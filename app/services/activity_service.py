from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import and_, desc
from sqlalchemy.orm import Session

from app.models.activity_event import ActivityEvent, EventType, TargetEntityType
from app.models.project import Project
from app.models.task import Task
from app.models.user import User
from app.schemas.activity import (
    ActivityEventOut,
    ActivityFeedPage,
    ActorOut,
    TargetOut,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache helpers — thin wrappers so the Redis client can be injected later
# without touching service logic.
# ---------------------------------------------------------------------------

_CACHE_TTL_FIRST_PAGE = 60  # seconds
_cache: Dict[str, Any] = {}  # placeholder; replace with Redis client


def _cache_key(project_id: UUID, params_hash: str) -> str:
    return f"meridian:activity:{project_id}:{params_hash}"


def _cache_get(key: str) -> Optional[Dict]:
    """Return cached value or None. No-op until Redis is wired."""
    return _cache.get(key)


def _cache_set(key: str, value: Dict, ttl: int) -> None:
    """Store value. No-op until Redis is wired."""
    _cache[key] = value


def _params_hash(event_types, actor_id, after, before) -> str:
    payload = json.dumps(
        {
            "event_types": sorted(t.value for t in event_types) if event_types else [],
            "actor_id": str(actor_id) if actor_id else None,
            "after": after.isoformat() if after else None,
            "before": before.isoformat() if before else None,
        },
        sort_keys=True,
    )
    return base64.urlsafe_b64encode(payload.encode()).decode()


# ---------------------------------------------------------------------------
# Cursor encoding
# ---------------------------------------------------------------------------

def _encode_cursor(created_at: datetime, event_id: UUID) -> str:
    raw = json.dumps(
        {"ts": created_at.isoformat(), "id": str(event_id)},
    )
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> Tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        data = json.loads(raw)
        ts = datetime.fromisoformat(data["ts"])
        uid = UUID(data["id"])
        return ts, uid
    except Exception as exc:
        raise ValueError(f"Invalid pagination cursor: {exc}") from exc


# ---------------------------------------------------------------------------
# Activity record writer
# ---------------------------------------------------------------------------

def record_activity(
    db: Session,
    *,
    tenant_id: UUID,
    project_id: UUID,
    actor_id: Optional[UUID],
    event_type: EventType,
    target_entity_type: TargetEntityType,
    target_entity_id: Optional[UUID] = None,
    summary: str = "",
    meta: Optional[Dict[str, Any]] = None,
) -> ActivityEvent:
    """
    Persist a single activity event.  Call this from domain services
    (task service, comment service, etc.) immediately after the
    underlying mutation is committed.
    """
    event = ActivityEvent(
        tenant_id=tenant_id,
        project_id=project_id,
        actor_id=actor_id,
        event_type=event_type,
        target_entity_type=target_entity_type,
        target_entity_id=target_entity_id,
        summary=summary,
        meta=meta or {},
    )
    db.add(event)
    db.flush()  # get generated id without closing the outer transaction
    return event


# ---------------------------------------------------------------------------
# Activity feed reader
# ---------------------------------------------------------------------------

class ActivityService:
    """
    Assembles paginated, enriched activity feeds for the project dashboard.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_project_feed(
        self,
        *,
        project_id: UUID,
        tenant_id: UUID,
        limit: int = 25,
        cursor: Optional[str] = None,
        event_types: Optional[List[EventType]] = None,
        actor_id: Optional[UUID] = None,
        after: Optional[datetime] = None,
        before: Optional[datetime] = None,
    ) -> ActivityFeedPage:
        """
        Return one page of activity events for *project_id*.

        Pagination is cursor-based.  Pass the ``next_cursor`` from the
        previous response to advance to the next page.  Absence of
        ``next_cursor`` in a response signals the end of the feed.
        """
        is_first_page = cursor is None
        ph = _params_hash(event_types, actor_id, after, before)

        if is_first_page:
            cache_key = _cache_key(project_id, ph)
            cached = _cache_get(cache_key)
            if cached is not None:
                log.debug("activity feed cache hit project=%s", project_id)
                return ActivityFeedPage(**cached)

        raw_events = self._query_events(
            project_id=project_id,
            tenant_id=tenant_id,
            limit=limit + 1,  # fetch one extra to detect next page
            cursor=cursor,
            event_types=event_types,
            actor_id=actor_id,
            after=after,
            before=before,
        )

        has_more = len(raw_events) > limit
        page_events = raw_events[:limit]

        enriched = self._enrich_events(page_events)

        next_cursor: Optional[str] = None
        if has_more and page_events:
            last = page_events[-1]
            next_cursor = _encode_cursor(last.created_at, last.id)

        result = ActivityFeedPage(
            items=enriched,
            next_cursor=next_cursor,
            total_returned=len(enriched),
        )

        if is_first_page:
            _cache_set(cache_key, result.dict(), ttl=_CACHE_TTL_FIRST_PAGE)

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _query_events(
        self,
        *,
        project_id: UUID,
        tenant_id: UUID,
        limit: int,
        cursor: Optional[str],
        event_types: Optional[List[EventType]],
        actor_id: Optional[UUID],
        after: Optional[datetime],
        before: Optional[datetime],
    ) -> List[ActivityEvent]:
        """
        Execute the base feed query with all active filters applied.

        The query is intentionally kept narrow (only the activity_events
        table) so that the index on (project_id, created_at) is used for
        all code paths.  Enrichment happens in a separate pass.
        """
        filters = [
            ActivityEvent.project_id == project_id,
            ActivityEvent.tenant_id == tenant_id,
        ]

        if event_types:
            filters.append(ActivityEvent.event_type.in_(event_types))

        if actor_id is not None:
            filters.append(ActivityEvent.actor_id == actor_id)

        if after is not None:
            filters.append(ActivityEvent.created_at > after)

        if before is not None:
            filters.append(ActivityEvent.created_at < before)

        if cursor is not None:
            cursor_ts, cursor_id = _decode_cursor(cursor)
            # Keyset pagination: events strictly older than the cursor position,
            # with tie-breaking on id to guarantee stable ordering.
            filters.append(
                and_(
                    ActivityEvent.created_at <= cursor_ts,
                    ActivityEvent.id != cursor_id,
                )
            )

        return (
            self.db.query(ActivityEvent)
            .filter(and_(*filters))
            .order_by(desc(ActivityEvent.created_at), desc(ActivityEvent.id))
            .limit(limit)
            .all()
        )

    def _enrich_events(
        self, events: List[ActivityEvent]
    ) -> List[ActivityEventOut]:
        """
        Attach human-readable actor and target metadata to each raw event.

        Actor resolution is straightforward: most events carry an actor_id
        that maps to a user row.  Target resolution depends on the entity
        type — tasks and projects need their current display name so the
        frontend can render a meaningful link even when the summary is stale.
        """
        result: List[ActivityEventOut] = []

        for event in events:
            actor_out: Optional[ActorOut] = None
            if event.actor_id is not None:
                # Look up the actor for display purposes.  Using a direct
                # filter on primary key here because actor records are small
                # and the user table has an index on id.
                actor_row = (
                    self.db.query(User)
                    .filter(User.id == event.actor_id)
                    .first()
                )
                if actor_row is not None:
                    actor_out = ActorOut(
                        id=actor_row.id,
                        display_name=actor_row.display_name,
                        avatar_url=actor_row.avatar_url,
                        email=actor_row.email,
                    )

            target_out: Optional[TargetOut] = None
            if event.target_entity_id is not None:
                target_out = self._resolve_target(
                    entity_type=event.target_entity_type,
                    entity_id=event.target_entity_id,
                )

            result.append(
                ActivityEventOut(
                    id=event.id,
                    event_type=event.event_type,
                    target_entity_type=event.target_entity_type,
                    target_entity_id=event.target_entity_id,
                    summary=event.summary,
                    meta=event.meta,
                    created_at=event.created_at,
                    actor=actor_out,
                    target=target_out,
                )
            )

        return result

    def _resolve_target(
        self,
        *,
        entity_type: TargetEntityType,
        entity_id: UUID,
    ) -> Optional[TargetOut]:
        """
        Fetch the current display label for a target entity so the feed
        row can show "[actor] updated task '[title]'" rather than a raw id.

        Returns None if the entity has been hard-deleted.
        """
        if entity_type == TargetEntityType.task:
            row = self.db.query(Task).filter(Task.id == entity_id).first()
            if row is not None:
                return TargetOut(
                    id=row.id,
                    entity_type=entity_type,
                    display_name=row.title,
                    url_path=f"/tasks/{row.id}",
                )

        elif entity_type == TargetEntityType.project:
            row = (
                self.db.query(Project)
                .filter(Project.id == entity_id)
                .first()
            )
            if row is not None:
                return TargetOut(
                    id=row.id,
                    entity_type=entity_type,
                    display_name=row.name,
                    url_path=f"/projects/{row.id}",
                )

        elif entity_type in (
            TargetEntityType.comment,
            TargetEntityType.file,
            TargetEntityType.member,
        ):
            # These entity types do not have a standalone detail page;
            # the display name is captured in event.summary at write time.
            return TargetOut(
                id=entity_id,
                entity_type=entity_type,
                display_name=None,
                url_path=None,
            )

        return None
