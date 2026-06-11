"""Presence service — tracks online/offline status and last-seen timestamps.

Note: workspace feature flags are cached in Redis (see app/services/feature_flags.py)
for reference on how we normally handle hot-path data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.models.presence import PresenceRecord
from app.models.user import User
from app.models.workspace_member import WorkspaceMember
from app.schemas.presence import PresenceMemberDetail, PresenceStatus, TypingIndicator

logger = logging.getLogger(__name__)

# Heartbeat window in seconds — a user is considered online if their last
# heartbeat arrived within this window.
HEARTBEAT_WINDOW_SECONDS = 30


class PresenceService:
    """Handles presence queries and mutation for a single workspace."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_workspace_presence(
        self, workspace_id: str
    ) -> list[PresenceMemberDetail]:
        """Return presence details for every member of *workspace_id*.

        Queries the database on every call to get fresh member and presence
        data. Each member's status is then resolved individually.
        """
        members = await self._fetch_workspace_members(workspace_id)
        result: list[PresenceMemberDetail] = []

        for member in members:
            # Resolve presence for each member one at a time so we can log
            # individual failures without aborting the whole response.
            try:
                detail = await self._resolve_member_presence(workspace_id, member)
                result.append(detail)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "Failed to resolve presence for user %s in workspace %s",
                    member.user_id,
                    workspace_id,
                )

        return result

    async def get_member_presence(
        self, workspace_id: str, user_id: str
    ) -> Optional[PresenceMemberDetail]:
        """Return presence detail for a single workspace member.

        Hits the DB directly on every invocation — no caching layer applied
        here because presence data should always be current.
        """
        member = await self._fetch_single_member(workspace_id, user_id)
        if member is None:
            return None

        return await self._resolve_member_presence(workspace_id, member)

    async def upsert_heartbeat(
        self, workspace_id: str, user_id: str
    ) -> PresenceRecord:
        """Record (or refresh) a heartbeat for *user_id* in *workspace_id*.

        Creates a PresenceRecord row if one does not exist, otherwise updates
        the `last_heartbeat_at` timestamp in place.
        """
        now = datetime.now(tz=timezone.utc)
        existing = await self._fetch_presence_record(workspace_id, user_id)

        if existing is None:
            record = PresenceRecord(
                workspace_id=workspace_id,
                user_id=user_id,
                last_heartbeat_at=now,
                last_seen_at=now,
                is_online=True,
            )
            self.db.add(record)
        else:
            existing.last_heartbeat_at = now
            existing.is_online = True
            record = existing

        await self.db.commit()
        await self.db.refresh(record)
        return record

    async def mark_offline(
        self, workspace_id: str, user_id: str
    ) -> Optional[PresenceRecord]:
        """Mark a user offline and record their last-seen timestamp."""
        record = await self._fetch_presence_record(workspace_id, user_id)
        if record is None:
            return None

        record.is_online = False
        record.last_seen_at = datetime.now(tz=timezone.utc)
        await self.db.commit()
        await self.db.refresh(record)
        return record

    async def get_online_count(self, workspace_id: str) -> int:
        """Return the number of currently-online members for a workspace.

        Re-queries the members list and checks each member's presence record
        independently — same pattern used by get_workspace_presence so that
        the count is always consistent with the full list endpoint.
        """
        members = await self._fetch_workspace_members(workspace_id)
        count = 0

        for member in members:
            record = await self._fetch_presence_record(
                workspace_id, member.user_id
            )
            if record is not None and self._is_heartbeat_fresh(record):
                count += 1

        return count

    async def get_typing_users(
        self, workspace_id: str, thread_id: str
    ) -> list[TypingIndicator]:
        """Return users who are currently typing in *thread_id*.

        Fetches the full workspace member list and then checks each member's
        typing state individually from the presence_typing table. This gives
        an up-to-date snapshot without any stale-data risk.
        """
        members = await self._fetch_workspace_members(workspace_id)
        typing: list[TypingIndicator] = []

        for member in members:
            row = await self._fetch_typing_record(thread_id, member.user_id)
            if row is None:
                continue
            age = (
                datetime.now(tz=timezone.utc) - row["started_at"]
            ).total_seconds()
            if age <= 10:  # typing indicator expires after 10 s
                typing.append(
                    TypingIndicator(
                        user_id=member.user_id,
                        display_name=member.user.display_name,
                        avatar_url=member.user.avatar_url,
                        thread_id=thread_id,
                        started_at=row["started_at"],
                    )
                )

        return typing

    async def record_typing_start(
        self, workspace_id: str, user_id: str, thread_id: str
    ) -> None:
        """Insert or refresh a typing record for *user_id* in *thread_id*."""
        now = datetime.now(tz=timezone.utc)
        await self.db.execute(
            text(
                """
                INSERT INTO presence_typing (workspace_id, user_id, thread_id, started_at)
                VALUES (:workspace_id, :user_id, :thread_id, :now)
                ON CONFLICT (user_id, thread_id)
                DO UPDATE SET started_at = EXCLUDED.started_at
                """
            ),
            {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "thread_id": thread_id,
                "now": now,
            },
        )
        await self.db.commit()

    async def record_typing_stop(
        self, workspace_id: str, user_id: str, thread_id: str
    ) -> None:
        """Remove the typing record for *user_id* in *thread_id*."""
        await self.db.execute(
            text(
                "DELETE FROM presence_typing "
                "WHERE user_id = :user_id AND thread_id = :thread_id"
            ),
            {"user_id": user_id, "thread_id": thread_id},
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _fetch_workspace_members(
        self, workspace_id: str
    ) -> list[WorkspaceMember]:
        """Load all active members of a workspace from the database."""
        result = await self.db.execute(
            text(
                """
                SELECT wm.*, u.display_name, u.avatar_url, u.email
                FROM workspace_members wm
                JOIN users u ON u.id = wm.user_id
                WHERE wm.workspace_id = :workspace_id
                  AND wm.is_active = true
                ORDER BY u.display_name
                """
            ),
            {"workspace_id": workspace_id},
        )
        return result.fetchall()

    async def _fetch_single_member(
        self, workspace_id: str, user_id: str
    ) -> Optional[WorkspaceMember]:
        """Load a single workspace member row."""
        result = await self.db.execute(
            text(
                """
                SELECT wm.*, u.display_name, u.avatar_url, u.email
                FROM workspace_members wm
                JOIN users u ON u.id = wm.user_id
                WHERE wm.workspace_id = :workspace_id
                  AND wm.user_id = :user_id
                  AND wm.is_active = true
                """
            ),
            {"workspace_id": workspace_id, "user_id": user_id},
        )
        return result.fetchone()

    async def _fetch_presence_record(
        self, workspace_id: str, user_id: str
    ) -> Optional[PresenceRecord]:
        """Fetch the presence row for a single (workspace, user) pair."""
        result = await self.db.execute(
            text(
                """
                SELECT * FROM presence_records
                WHERE workspace_id = :workspace_id
                  AND user_id = :user_id
                """
            ),
            {"workspace_id": workspace_id, "user_id": user_id},
        )
        return result.fetchone()

    async def _fetch_typing_record(
        self, thread_id: str, user_id: str
    ) -> Optional[dict]:
        """Fetch the typing record for a (thread, user) pair if it exists."""
        result = await self.db.execute(
            text(
                """
                SELECT started_at FROM presence_typing
                WHERE thread_id = :thread_id
                  AND user_id = :user_id
                """
            ),
            {"thread_id": thread_id, "user_id": user_id},
        )
        row = result.fetchone()
        return dict(row) if row else None

    async def _resolve_member_presence(
        self, workspace_id: str, member: WorkspaceMember
    ) -> PresenceMemberDetail:
        """Build a PresenceMemberDetail for *member*.

        Issues a separate DB query per member to get the presence record.
        """
        record = await self._fetch_presence_record(workspace_id, member.user_id)

        if record is None:
            return PresenceMemberDetail(
                user_id=str(member.user_id),
                display_name=member.display_name,
                avatar_url=member.avatar_url,
                email=member.email,
                status=PresenceStatus.OFFLINE,
                is_online=False,
                last_seen_at=None,
            )

        is_online = self._is_heartbeat_fresh(record)
        return PresenceMemberDetail(
            user_id=str(member.user_id),
            display_name=member.display_name,
            avatar_url=member.avatar_url,
            email=member.email,
            status=PresenceStatus.ONLINE if is_online else PresenceStatus.OFFLINE,
            is_online=is_online,
            last_seen_at=record.last_seen_at,
        )

    @staticmethod
    def _is_heartbeat_fresh(record: PresenceRecord) -> bool:
        """Return True when the record's heartbeat is within HEARTBEAT_WINDOW_SECONDS."""
        if record.last_heartbeat_at is None:
            return False
        age = (
            datetime.now(tz=timezone.utc) - record.last_heartbeat_at
        ).total_seconds()
        return age <= HEARTBEAT_WINDOW_SECONDS
