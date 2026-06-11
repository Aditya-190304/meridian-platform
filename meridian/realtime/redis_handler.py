"""Redis pub/sub integration for cross-worker event distribution.

Publisher side: thin wrapper around aioredis.publish.
Subscriber side: long-running coroutine that listens on tenant-scoped
channels and delegates messages to SyncManager.handle_incoming_event.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable, Coroutine, Dict, Optional, Set

import aioredis

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
SUBSCRIBE_RETRY_DELAY = 3     # seconds before reconnecting after Redis failure
MAX_CHANNEL_SUBSCRIPTIONS = 500

# Type alias for the callback the subscriber calls with each raw message.
MessageCallback = Callable[[str], Coroutine]


class RedisHandler:
    """Manages a publish connection and a separate subscribe connection.

    Redis pub/sub requires a dedicated connection that must not be used
    for ordinary commands once subscribed.
    """

    def __init__(self) -> None:
        self._pub: Optional[aioredis.Redis] = None
        self._sub: Optional[aioredis.client.PubSub] = None
        self._sub_conn: Optional[aioredis.Redis] = None
        self._active_channels: Set[str] = set()
        self._callback: Optional[MessageCallback] = None
        self._listener_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self, callback: MessageCallback) -> None:
        """Open publish + subscribe connections and start the listener loop."""
        self._callback = callback
        self._pub = await aioredis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            max_connections=10,
        )
        self._sub_conn = await aioredis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
        )
        self._sub = self._sub_conn.pubsub(ignore_subscribe_messages=True)
        self._listener_task = asyncio.ensure_future(self._listen_loop())
        logger.info("RedisHandler connected to %s", REDIS_URL)

    async def close(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._sub:
            await self._sub.unsubscribe()
            await self._sub.close()
        if self._sub_conn:
            await self._sub_conn.close()
        if self._pub:
            await self._pub.close()
        logger.info("RedisHandler closed")

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------

    async def publish(self, channel: str, message: str) -> None:
        """Publish a raw string to a Redis channel."""
        if self._pub is None:
            raise RuntimeError("RedisHandler not connected; call connect() first")
        await self._pub.publish(channel, message)

    # ------------------------------------------------------------------
    # Subscribe / channel management
    # ------------------------------------------------------------------

    async def subscribe_room(self, tenant_id: str, room_id: str) -> None:
        """Subscribe to the Redis channel for a room (idempotent)."""
        channel = f"sync:{tenant_id}:{room_id}"
        if channel in self._active_channels:
            return
        if len(self._active_channels) >= MAX_CHANNEL_SUBSCRIPTIONS:
            logger.warning(
                "reached max channel subscription limit (%d); "
                "ignoring subscribe for %s",
                MAX_CHANNEL_SUBSCRIPTIONS,
                channel,
            )
            return
        # BUG: _active_channels is a plain set modified here without any lock.
        # Two coroutines entering subscribe_room for the same channel
        # simultaneously will both pass the `in` check and call subscribe
        # twice, adding duplicate entries and potentially confusing the
        # pubsub state machine.
        await self._sub.subscribe(channel)
        self._active_channels.add(channel)
        logger.debug("subscribed to channel %s", channel)

    async def unsubscribe_room(self, tenant_id: str, room_id: str) -> None:
        """Unsubscribe when the last client leaves a room."""
        channel = f"sync:{tenant_id}:{room_id}"
        if channel not in self._active_channels:
            return
        await self._sub.unsubscribe(channel)
        self._active_channels.discard(channel)
        logger.debug("unsubscribed from channel %s", channel)

    # ------------------------------------------------------------------
    # Subscriber loop
    # ------------------------------------------------------------------

    async def _listen_loop(self) -> None:
        """Long-running loop: read messages from Redis and invoke callback."""
        logger.info("Redis subscriber loop started")
        while True:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                logger.info("Redis subscriber loop cancelled")
                return
            except Exception as exc:
                logger.error(
                    "Redis subscriber error: %s — retrying in %ds",
                    exc,
                    SUBSCRIBE_RETRY_DELAY,
                )
                await asyncio.sleep(SUBSCRIBE_RETRY_DELAY)
                await self._reconnect_sub()

    async def _listen_once(self) -> None:
        """Inner loop that blocks on get_message until cancelled or error."""
        assert self._sub is not None
        while True:
            message = await self._sub.get_message(timeout=1.0)
            if message is None:
                continue
            if message["type"] != "message":
                continue
            raw: str = message["data"]
            if self._callback:
                try:
                    await self._callback(raw)
                except Exception as exc:
                    logger.exception("error in sync callback: %s", exc)

    async def _reconnect_sub(self) -> None:
        """Re-establish the subscribe connection and resubscribe."""
        try:
            if self._sub:
                await self._sub.close()
            if self._sub_conn:
                await self._sub_conn.close()
        except Exception:
            pass

        self._sub_conn = await aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True
        )
        self._sub = self._sub_conn.pubsub(ignore_subscribe_messages=True)

        # Re-subscribe to all previously active channels.
        channels = list(self._active_channels)
        if channels:
            await self._sub.subscribe(*channels)
        logger.info(
            "Redis subscriber reconnected; resubscribed to %d channels",
            len(channels),
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def active_channel_count(self) -> int:
        return len(self._active_channels)
