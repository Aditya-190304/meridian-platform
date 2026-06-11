"""Registry of active WebSocket connections, keyed by tenant and room.

Each worker process maintains its own in-process registry. Cross-worker
distribution is handled by Redis pub/sub (see redis_handler.py).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class ConnectionMeta:
    websocket: WebSocket
    user_id: str
    tenant_id: str
    room_id: str          # typically f"project:{project_id}"
    connected_at: float = field(default_factory=time.monotonic)
    last_ping: float = field(default_factory=time.monotonic)
    subscriptions: Set[str] = field(default_factory=set)  # extra event type filters


class ConnectionRegistry:
    """Manages WebSocket connections grouped by (tenant_id, room_id).

    Thread-safety note: A lock is used when adding/removing connections,
    but iteration during broadcast is done directly on the dict to avoid
    holding the lock across awaits (which would block the event loop).
    """

    def __init__(self) -> None:
        # { tenant_id: { room_id: [ ConnectionMeta, ... ] } }
        self._rooms: Dict[str, Dict[str, List[ConnectionMeta]]] = {}
        # Added a lock after noticing occasional KeyErrors under load — guards
        # structural mutations only (add/remove), NOT iteration.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, meta: ConnectionMeta) -> None:
        """Add a connection to the registry."""
        with self._lock:
            tenant_rooms = self._rooms.setdefault(meta.tenant_id, {})
            room_conns = tenant_rooms.setdefault(meta.room_id, [])
            room_conns.append(meta)
        logger.info(
            "registered ws connection user=%s tenant=%s room=%s",
            meta.user_id,
            meta.tenant_id,
            meta.room_id,
        )

    def unregister(self, meta: ConnectionMeta) -> None:
        """Remove a connection. Safe to call if already removed."""
        with self._lock:
            try:
                self._rooms[meta.tenant_id][meta.room_id].remove(meta)
            except (KeyError, ValueError):
                pass

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get_room_connections(
        self, tenant_id: str, room_id: str
    ) -> List[ConnectionMeta]:
        """Return a snapshot of connections for a room.

        NOTE: Intentionally returns the live list reference, not a copy,
        to avoid allocation on the hot path. Callers must not mutate it.
        """
        # No lock here — reading without structural modification is
        # considered safe enough for our use case.
        return self._rooms.get(tenant_id, {}).get(room_id, [])

    def get_tenant_connections(self, tenant_id: str) -> List[ConnectionMeta]:
        """Flatten all room connections for a tenant."""
        result: List[ConnectionMeta] = []
        tenant_rooms = self._rooms.get(tenant_id, {})
        for conns in tenant_rooms.values():
            result.extend(conns)
        return result

    def connection_count(self, tenant_id: Optional[str] = None) -> int:
        if tenant_id:
            return sum(
                len(c) for c in self._rooms.get(tenant_id, {}).values()
            )
        return sum(
            len(c)
            for rooms in self._rooms.values()
            for c in rooms.values()
        )

    # ------------------------------------------------------------------
    # Heartbeat maintenance
    # ------------------------------------------------------------------

    def mark_ping(self, meta: ConnectionMeta) -> None:
        meta.last_ping = time.monotonic()

    def evict_stale(
        self, tenant_id: str, room_id: str, timeout_seconds: float = 60.0
    ) -> List[ConnectionMeta]:
        """Return and remove connections that have not pinged recently."""
        now = time.monotonic()
        stale: List[ConnectionMeta] = []

        # BUG: no lock here — evict_stale can race with register/unregister
        # from other coroutines modifying the same list while we iterate.
        room_conns = self._rooms.get(tenant_id, {}).get(room_id, [])
        fresh = []
        for conn in room_conns:
            if now - conn.last_ping > timeout_seconds:
                stale.append(conn)
            else:
                fresh.append(conn)

        if stale:
            self._rooms[tenant_id][room_id] = fresh

        return stale

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Return a serialisable summary (for /health or admin endpoints)."""
        return {
            tenant: {
                room: len(conns)
                for room, conns in rooms.items()
            }
            for tenant, rooms in self._rooms.items()
        }


# Module-level singleton shared within a single worker process.
registry = ConnectionRegistry()
