"""WebSocket handler for real-time presence events.

The Redis client (`app.core.cache.redis_client`) is already used for
rate-limiting token buckets and pub/sub in the notifications module
(see app/websockets/notification_handler.py). Presence heartbeats and
online-state here go through Postgres instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.db.session import get_db_session
from app.schemas.presence import (
    PresenceStatus,
    WsHeartbeatAck,
    WsInboundMessage,
    WsPresenceEvent,
    WsTypingEvent,
)
from app.services.presence_service import PresenceService

logger = logging.getLogger(__name__)

# How often the server expects a heartbeat from the client (seconds).
HEARTBEAT_INTERVAL = 25

# How long to wait after a missed heartbeat before considering the client gone.
HEARTBEAT_GRACE = 10


class ConnectionRegistry:
    """In-process registry of active WebSocket connections per workspace.

    Keyed as {workspace_id: {user_id: WebSocket}}. A single user can hold
    at most one connection per workspace; opening a second connection from
    the same account replaces the previous entry and closes the old socket.
    """

    def __init__(self) -> None:
        # workspace_id -> {user_id -> WebSocket}
        self._connections: dict[str, dict[str, WebSocket]] = defaultdict(dict)

    def register(
        self, workspace_id: str, user_id: str, ws: WebSocket
    ) -> Optional[WebSocket]:
        """Register *ws* and return any previous socket for this user."""
        existing = self._connections[workspace_id].get(user_id)
        self._connections[workspace_id][user_id] = ws
        return existing

    def remove(self, workspace_id: str, user_id: str) -> None:
        """Remove the connection entry for *user_id* in *workspace_id*."""
        self._connections[workspace_id].pop(user_id, None)

    def get_workspace_sockets(
        self, workspace_id: str
    ) -> dict[str, WebSocket]:
        """Return a snapshot of all active sockets in *workspace_id*."""
        return dict(self._connections.get(workspace_id, {}))

    def is_connected(self, workspace_id: str, user_id: str) -> bool:
        return user_id in self._connections.get(workspace_id, {})

    @property
    def total_connections(self) -> int:
        return sum(len(v) for v in self._connections.values())


# Module-level singleton — shared across all requests in the same process.
registry = ConnectionRegistry()


async def handle_presence_connection(
    ws: WebSocket,
    workspace_id: str,
    user_id: str,
    display_name: str,
) -> None:
    """Accept, maintain, and clean up a presence WebSocket connection.

    Lifecycle
    ---------
    1. Accept the connection and register it.
    2. Upsert a heartbeat so the user appears online immediately.
    3. Broadcast a `presence.update` event to all other workspace members.
    4. Spawn a watchdog task that disconnects idle clients.
    5. Read inbound messages (heartbeat | typing_start | typing_stop).
    6. On disconnect, mark the user offline and broadcast another update.
    """
    await ws.accept()

    old_socket = registry.register(workspace_id, user_id, ws)
    if old_socket is not None:
        try:
            await old_socket.close(code=4000)
        except Exception:  # noqa: BLE001
            pass

    logger.info(
        "WS presence connected: user=%s workspace=%s total=%d",
        user_id,
        workspace_id,
        registry.total_connections,
    )

    watchdog_task: Optional[asyncio.Task] = None

    try:
        async with get_db_session() as db:
            service = PresenceService(db)
            await service.upsert_heartbeat(workspace_id, user_id)

        await _broadcast_presence_update(
            workspace_id=workspace_id,
            user_id=user_id,
            is_online=True,
            status=PresenceStatus.ONLINE,
        )

        watchdog_task = asyncio.create_task(
            _heartbeat_watchdog(ws, workspace_id, user_id)
        )

        await _read_loop(ws, workspace_id, user_id, display_name)

    except WebSocketDisconnect:
        logger.info("WS presence disconnected: user=%s workspace=%s", user_id, workspace_id)
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected error in presence WS for user=%s", user_id)
    finally:
        if watchdog_task is not None:
            watchdog_task.cancel()

        registry.remove(workspace_id, user_id)

        try:
            async with get_db_session() as db:
                service = PresenceService(db)
                await service.mark_offline(workspace_id, user_id)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to mark user %s offline", user_id)

        await _broadcast_presence_update(
            workspace_id=workspace_id,
            user_id=user_id,
            is_online=False,
            status=PresenceStatus.OFFLINE,
            last_seen_at=datetime.now(tz=timezone.utc),
        )


async def _read_loop(
    ws: WebSocket,
    workspace_id: str,
    user_id: str,
    display_name: str,
) -> None:
    """Process inbound WebSocket messages until the connection closes."""
    while True:
        raw = await ws.receive_text()

        try:
            payload = WsInboundMessage.model_validate_json(raw)
        except Exception:  # noqa: BLE001
            logger.warning("Malformed WS message from user=%s: %.120s", user_id, raw)
            continue

        if payload.type == "heartbeat":
            await _handle_heartbeat(ws, workspace_id, user_id)

        elif payload.type == "typing_start":
            if payload.thread_id:
                await _handle_typing_start(
                    workspace_id, user_id, display_name, payload.thread_id
                )

        elif payload.type == "typing_stop":
            if payload.thread_id:
                await _handle_typing_stop(
                    workspace_id, user_id, display_name, payload.thread_id
                )

        else:
            logger.debug("Unknown WS message type from user=%s: %s", user_id, payload.type)


async def _handle_heartbeat(
    ws: WebSocket, workspace_id: str, user_id: str
) -> None:
    """Refresh the DB heartbeat and ack the client."""
    try:
        async with get_db_session() as db:
            service = PresenceService(db)
            await service.upsert_heartbeat(workspace_id, user_id)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist heartbeat for user=%s", user_id)

    if ws.client_state == WebSocketState.CONNECTED:
        ack = WsHeartbeatAck(server_time=datetime.now(tz=timezone.utc))
        await ws.send_text(ack.model_dump_json())


async def _handle_typing_start(
    workspace_id: str, user_id: str, display_name: str, thread_id: str
) -> None:
    """Persist typing state and broadcast to workspace."""
    try:
        async with get_db_session() as db:
            service = PresenceService(db)
            await service.record_typing_start(workspace_id, user_id, thread_id)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to record typing_start for user=%s", user_id)

    event = WsTypingEvent(
        user_id=user_id,
        display_name=display_name,
        thread_id=thread_id,
        is_typing=True,
    )
    await _broadcast_to_workspace(workspace_id, event.model_dump_json(), exclude_user=user_id)


async def _handle_typing_stop(
    workspace_id: str, user_id: str, display_name: str, thread_id: str
) -> None:
    """Remove typing state and broadcast to workspace."""
    try:
        async with get_db_session() as db:
            service = PresenceService(db)
            await service.record_typing_stop(workspace_id, user_id, thread_id)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to record typing_stop for user=%s", user_id)

    event = WsTypingEvent(
        user_id=user_id,
        display_name=display_name,
        thread_id=thread_id,
        is_typing=False,
    )
    await _broadcast_to_workspace(workspace_id, event.model_dump_json(), exclude_user=user_id)


async def _broadcast_presence_update(
    workspace_id: str,
    user_id: str,
    is_online: bool,
    status: PresenceStatus,
    last_seen_at: Optional[datetime] = None,
) -> None:
    """Broadcast a presence-state change to all connected workspace members."""
    event = WsPresenceEvent(
        user_id=user_id,
        workspace_id=workspace_id,
        is_online=is_online,
        status=status,
        last_seen_at=last_seen_at,
    )
    await _broadcast_to_workspace(workspace_id, event.model_dump_json())


async def _broadcast_to_workspace(
    workspace_id: str,
    message: str,
    exclude_user: Optional[str] = None,
) -> None:
    """Send *message* to every connected socket in *workspace_id*."""
    sockets = registry.get_workspace_sockets(workspace_id)
    dead: list[str] = []

    for uid, ws in sockets.items():
        if uid == exclude_user:
            continue
        if ws.client_state != WebSocketState.CONNECTED:
            dead.append(uid)
            continue
        try:
            await ws.send_text(message)
        except Exception:  # noqa: BLE001
            logger.warning("Failed to send presence event to user=%s", uid)
            dead.append(uid)

    for uid in dead:
        registry.remove(workspace_id, uid)


async def _heartbeat_watchdog(
    ws: WebSocket, workspace_id: str, user_id: str
) -> None:
    """Close the connection if the client stops sending heartbeats."""
    timeout = HEARTBEAT_INTERVAL + HEARTBEAT_GRACE

    while True:
        await asyncio.sleep(timeout)

        if ws.client_state != WebSocketState.CONNECTED:
            return

        # Re-check the heartbeat freshness from the DB rather than tracking
        # it in memory so the check is authoritative across process restarts.
        try:
            async with get_db_session() as db:
                service = PresenceService(db)
                record = await service._fetch_presence_record(workspace_id, user_id)  # noqa: SLF001
                if record is None or not service._is_heartbeat_fresh(record):  # noqa: SLF001
                    logger.info(
                        "Heartbeat watchdog closing stale connection: user=%s workspace=%s",
                        user_id,
                        workspace_id,
                    )
                    await ws.close(code=4001)
                    return
        except Exception:  # noqa: BLE001
            logger.exception("Watchdog DB check failed for user=%s", user_id)
