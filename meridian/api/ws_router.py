"""WebSocket router for real-time collaboration sync.

Clients connect to /ws/{tenant_id}/{project_id} after obtaining a
short-lived token via POST /auth/ws-token. The token is validated on
connect and carries the user identity so we don't need to accept
credentials over the socket itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel

from meridian.auth.tokens import decode_ws_token, WsTokenPayload
from meridian.core.exceptions import TenantNotFound
from meridian.realtime.connection_registry import ConnectionMeta, registry
from meridian.realtime.redis_handler import RedisHandler
from meridian.realtime.sync_manager import SyncManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ws", tags=["realtime"])

# Worker-scoped singletons, initialised in app lifespan.
_redis_handler: Optional[RedisHandler] = None
_sync_manager: Optional[SyncManager] = None


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------

def get_sync_manager() -> SyncManager:
    if _sync_manager is None:
        raise RuntimeError("SyncManager not initialised — check app lifespan setup")
    return _sync_manager


def get_redis_handler() -> RedisHandler:
    if _redis_handler is None:
        raise RuntimeError("RedisHandler not initialised — check app lifespan setup")
    return _redis_handler


# ---------------------------------------------------------------------------
# Lifespan helpers (called from app.py)
# ---------------------------------------------------------------------------

async def startup(redis_url: Optional[str] = None) -> None:
    """Initialise Redis and SyncManager. Call from FastAPI lifespan."""
    global _redis_handler, _sync_manager
    handler = RedisHandler()
    manager = SyncManager(handler)
    await handler.connect(callback=manager.handle_incoming_event)
    _redis_handler = handler
    _sync_manager = manager
    logger.info("realtime subsystem started")


async def shutdown() -> None:
    """Gracefully close all connections. Call from FastAPI lifespan."""
    global _redis_handler, _sync_manager
    if _sync_manager:
        await _sync_manager.shutdown()
    if _redis_handler:
        await _redis_handler.close()
    _redis_handler = None
    _sync_manager = None
    logger.info("realtime subsystem stopped")


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------

@router.websocket("/{tenant_id}/{project_id}")
async def ws_collaboration(
    websocket: WebSocket,
    tenant_id: str,
    project_id: str,
    token: str = Query(..., description="Short-lived WS auth token"),
) -> None:
    """WebSocket endpoint for real-time project collaboration.

    Protocol (client -> server messages):
        {"type": "pong"}                        — heartbeat reply
        {"type": "subscribe", "events": [...]}  — filter to specific event types
        {"type": "unsubscribe", "events": [...]} — remove event filters

    Protocol (server -> client messages):
        {"type": "ping", "ts": <float>}         — heartbeat probe
        {"type": "connected", ...}              — handshake ack
        {"type": "task.updated", "payload": {}} — example domain event
        {"type": "server_shutdown", "reconnect_after": 2} — graceful restart
    """
    manager = get_sync_manager()
    redis = get_redis_handler()
    room_id = f"project:{project_id}"

    # --- Auth ---
    try:
        token_payload: WsTokenPayload = decode_ws_token(token)
    except Exception as exc:
        logger.warning("ws auth failed for tenant=%s: %s", tenant_id, exc)
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    if token_payload.tenant_id != tenant_id:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()

    meta = ConnectionMeta(
        websocket=websocket,
        user_id=token_payload.user_id,
        tenant_id=tenant_id,
        room_id=room_id,
    )

    # Register and ensure Redis subscription for this room.
    # BUG: register() and subscribe_room() are two separate operations.
    # Between them, another coroutine handling a broadcast could call
    # get_room_connections() and see the new meta before the Redis channel
    # is subscribed, or conversely the channel can be subscribed before
    # the connection is in the registry — neither is catastrophic here but
    # the ordering is not atomic.
    registry.register(meta)
    await redis.subscribe_room(tenant_id, room_id)
    manager.ensure_heartbeat(tenant_id, room_id)

    await websocket.send_json({
        "type": "connected",
        "user_id": meta.user_id,
        "room": room_id,
        "ts": time.time(),
    })

    logger.info(
        "ws connected user=%s tenant=%s room=%s",
        meta.user_id, tenant_id, room_id,
    )

    try:
        await _client_loop(websocket, meta, manager)
    except WebSocketDisconnect as exc:
        logger.info(
            "ws disconnected user=%s code=%s", meta.user_id, exc.code
        )
    except Exception as exc:
        logger.exception("unexpected ws error user=%s: %s", meta.user_id, exc)
    finally:
        registry.unregister(meta)
        # Unsubscribe from Redis if this was the last connection in the room.
        if not registry.get_room_connections(tenant_id, room_id):
            await redis.unsubscribe_room(tenant_id, room_id)


async def _client_loop(
    websocket: WebSocket,
    meta: ConnectionMeta,
    manager: SyncManager,
) -> None:
    """Read and handle messages from the client until disconnection."""
    while True:
        raw = await websocket.receive_text()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("non-JSON message from user=%s, ignoring", meta.user_id)
            continue

        msg_type = msg.get("type")

        if msg_type == "pong":
            registry.mark_ping(meta)

        elif msg_type == "subscribe":
            events = msg.get("events", [])
            if isinstance(events, list):
                for ev in events:
                    if isinstance(ev, str):
                        meta.subscriptions.add(ev)

        elif msg_type == "unsubscribe":
            events = msg.get("events", [])
            if isinstance(events, list):
                meta.subscriptions.difference_update(
                    e for e in events if isinstance(e, str)
                )

        elif msg_type == "ping":
            # Client-initiated ping (some proxies require bidirectional activity).
            await websocket.send_json({"type": "pong", "ts": time.time()})

        else:
            logger.debug(
                "unknown message type %r from user=%s", msg_type, meta.user_id
            )


# ---------------------------------------------------------------------------
# Internal broadcast helper used by HTTP API handlers
# ---------------------------------------------------------------------------

async def emit_task_update(
    tenant_id: str,
    project_id: str,
    task_id: str,
    changes: dict,
    actor_user_id: Optional[str] = None,
) -> None:
    """Convenience wrapper called from task CRUD endpoints."""
    manager = get_sync_manager()
    await manager.broadcast_update(
        tenant_id=tenant_id,
        room_id=f"project:{project_id}",
        event_type="task.updated",
        payload={"task_id": task_id, "changes": changes},
        actor_user_id=actor_user_id,
    )


async def emit_project_update(
    tenant_id: str,
    project_id: str,
    changes: dict,
    actor_user_id: Optional[str] = None,
) -> None:
    """Convenience wrapper called from project CRUD endpoints."""
    manager = get_sync_manager()
    await manager.broadcast_update(
        tenant_id=tenant_id,
        room_id=f"project:{project_id}",
        event_type="project.updated",
        payload={"project_id": project_id, "changes": changes},
        actor_user_id=actor_user_id,
    )
