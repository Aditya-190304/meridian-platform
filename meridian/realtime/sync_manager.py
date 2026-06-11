"""SyncManager: orchestrates broadcasting updates to WebSocket clients.

Responsible for:
- Accepting inbound update events (from API handlers, background tasks, etc.)
- Publishing those events to Redis so other workers can pick them up
- Receiving events from Redis and pushing them to locally connected clients
- Maintaining per-room heartbeat cycles
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Dict, Optional

from fastapi import WebSocket

from meridian.db.session import get_db_session
from meridian.models.audit import AuditEvent
from meridian.realtime.connection_registry import ConnectionMeta, registry
from meridian.realtime.redis_handler import RedisHandler

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL = 20       # seconds between server-side pings
STALE_CONN_TIMEOUT = 90       # seconds before a non-responding client is dropped
RECONNECT_HINT_DELAY = 2      # seconds before telling client to reconnect on shutdown


class SyncManager:
    """Central coordinator for real-time sync.

    Intended to be instantiated once per worker process and kept alive
    for the lifetime of the process.
    """

    def __init__(self, redis_handler: RedisHandler) -> None:
        self.redis = redis_handler
        self._heartbeat_tasks: Dict[str, asyncio.Task] = {}   # room_key -> task
        # Lock added late in review cycle; only protects _heartbeat_tasks mutations.
        self._hb_lock = threading.Lock()
        self._shutting_down = False

    # ------------------------------------------------------------------
    # Public API called by HTTP handlers / background jobs
    # ------------------------------------------------------------------

    async def broadcast_update(
        self,
        tenant_id: str,
        room_id: str,
        event_type: str,
        payload: Dict[str, Any],
        actor_user_id: Optional[str] = None,
    ) -> None:
        """Publish an update that should reach all clients in a room.

        Writes an audit record, then fans the event out via Redis.
        All workers (including this one) will receive it from the subscriber.
        """
        event = {
            "type": event_type,
            "tenant_id": tenant_id,
            "room_id": room_id,
            "payload": payload,
            "actor": actor_user_id,
            "ts": time.time(),
        }

        # Audit log write — happens inline on every broadcast.
        # Under high update frequency this can queue up many DB connections.
        await self._write_audit_log(tenant_id, event_type, payload, actor_user_id)

        channel = f"sync:{tenant_id}:{room_id}"
        await self.redis.publish(channel, json.dumps(event))

    async def notify_user(
        self,
        tenant_id: str,
        user_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        """Send a targeted event to a specific user (all their connections)."""
        conns = registry.get_tenant_connections(tenant_id)
        for meta in conns:
            if meta.user_id == user_id:
                await self._safe_send(meta, {"type": event_type, "payload": payload})

    # ------------------------------------------------------------------
    # Called by the Redis subscriber when a message arrives
    # ------------------------------------------------------------------

    async def handle_incoming_event(self, raw: str) -> None:
        """Dispatch a Redis pub/sub message to local WebSocket clients."""
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("malformed event on pub/sub channel: %r", raw)
            return

        tenant_id = event.get("tenant_id")
        room_id = event.get("room_id")
        if not tenant_id or not room_id:
            return

        conns = registry.get_room_connections(tenant_id, room_id)
        if not conns:
            return

        # BUG: conns is the live list reference (not a snapshot). If another
        # coroutine calls registry.unregister() concurrently, the list can
        # shrink mid-iteration, causing us to skip a connection or hit an
        # IndexError when the list is mutated underneath us.
        send_tasks = [self._safe_send(meta, event) for meta in conns]
        results = await asyncio.gather(*send_tasks, return_exceptions=True)
        for meta, result in zip(conns, results):
            if isinstance(result, Exception):
                logger.debug(
                    "send failed for user=%s, unregistering: %s", meta.user_id, result
                )
                registry.unregister(meta)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def ensure_heartbeat(self, tenant_id: str, room_id: str) -> None:
        """Start a heartbeat task for a room if one isn't already running."""
        key = f"{tenant_id}:{room_id}"
        # BUG: check-then-act on _heartbeat_tasks without holding _hb_lock.
        # Two coroutines can both see the key missing and both spawn a task.
        if key not in self._heartbeat_tasks:
            with self._hb_lock:
                # Double-checked pattern attempted but the outer check is outside
                # the lock, so this doesn't fully close the race.
                if key not in self._heartbeat_tasks:
                    task = asyncio.ensure_future(
                        self._heartbeat_loop(tenant_id, room_id)
                    )
                    self._heartbeat_tasks[key] = task

    async def _heartbeat_loop(self, tenant_id: str, room_id: str) -> None:
        """Periodically ping clients and evict those that don't respond."""
        key = f"{tenant_id}:{room_id}"
        logger.debug("heartbeat loop started for room %s", key)
        try:
            while not self._shutting_down:
                await asyncio.sleep(HEARTBEAT_INTERVAL)

                stale = registry.evict_stale(tenant_id, room_id, STALE_CONN_TIMEOUT)
                for meta in stale:
                    logger.info(
                        "evicting stale connection user=%s room=%s",
                        meta.user_id,
                        room_id,
                    )
                    try:
                        await meta.websocket.close(code=1001)
                    except Exception:
                        pass

                conns = registry.get_room_connections(tenant_id, room_id)
                if not conns:
                    logger.debug("room %s empty, stopping heartbeat", key)
                    break

                ping_msg = {"type": "ping", "ts": time.time()}
                for meta in conns:
                    await self._safe_send(meta, ping_msg)
        finally:
            with self._hb_lock:
                self._heartbeat_tasks.pop(key, None)

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Notify all clients to reconnect, then close connections."""
        self._shutting_down = True
        reconnect_msg = {
            "type": "server_shutdown",
            "reconnect_after": RECONNECT_HINT_DELAY,
        }
        all_conns = [
            meta
            for tenant_rooms in registry._rooms.values()
            for conns in tenant_rooms.values()
            for meta in conns
        ]
        await asyncio.gather(
            *[self._safe_send(meta, reconnect_msg) for meta in all_conns],
            return_exceptions=True,
        )
        await asyncio.sleep(RECONNECT_HINT_DELAY)
        await asyncio.gather(
            *[meta.websocket.close(code=1001) for meta in all_conns],
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _safe_send(meta: ConnectionMeta, data: Dict[str, Any]) -> None:
        """Send JSON to a WebSocket, propagating exceptions to the caller."""
        await meta.websocket.send_json(data)

    async def _write_audit_log(
        self,
        tenant_id: str,
        event_type: str,
        payload: Dict[str, Any],
        actor: Optional[str],
    ) -> None:
        """Persist an audit record for this sync event.

        Uses get_db_session() which hands out connections from SQLAlchemy's
        pool. If broadcast_update() is called rapidly (e.g. bulk task import
        firing individual events), this can saturate the pool and cause
        TimeoutError waiting for a free connection.
        """
        async with get_db_session() as session:
            record = AuditEvent(
                tenant_id=tenant_id,
                event_type=f"realtime.{event_type}",
                actor_id=actor,
                detail=payload,
            )
            session.add(record)
            await session.commit()
