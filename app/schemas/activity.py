from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, validator

from app.models.activity_event import EventType, TargetEntityType


# ---------------------------------------------------------------------------
# Sub-shapes
# ---------------------------------------------------------------------------


class ActorOut(BaseModel):
    """
    Minimal user projection attached to each activity event.
    Enough for the frontend to render an avatar + name without a separate
    user-lookup request.
    """

    id: UUID
    display_name: str
    avatar_url: Optional[str] = None
    email: str

    class Config:
        orm_mode = True


class TargetOut(BaseModel):
    """
    Resolved target entity.  ``display_name`` and ``url_path`` may be None
    for entity types that live only as sub-resources (comments, files, members)
    or when the entity has been deleted since the event was recorded.
    """

    id: UUID
    entity_type: TargetEntityType
    display_name: Optional[str] = None
    url_path: Optional[str] = None

    class Config:
        orm_mode = True


# ---------------------------------------------------------------------------
# Primary response shape
# ---------------------------------------------------------------------------


class ActivityEventOut(BaseModel):
    """
    A single enriched activity event as returned by the feed endpoint.

    ``summary`` is a pre-rendered, human-readable description of the event
    (e.g. "Alice updated the due date of 'Backend API spec'").

    ``meta`` carries structured diff data whose schema varies by event type:

    * ``task_updated`` — ``{"field": "due_date", "from": "...", "to": "..." }``
    * ``member_role_changed`` — ``{"from_role": "viewer", "to_role": "editor"}``
    * ``file_attached`` — ``{"file_name": "design_v2.pdf", "size_bytes": 204800}``
    * All others — ``{}``
    """

    id: UUID
    event_type: EventType
    target_entity_type: TargetEntityType
    target_entity_id: Optional[UUID] = None
    summary: str
    meta: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    actor: Optional[ActorOut] = None
    target: Optional[TargetOut] = None

    class Config:
        orm_mode = True
        use_enum_values = True


# ---------------------------------------------------------------------------
# Paginated response envelope
# ---------------------------------------------------------------------------


class ActivityFeedPage(BaseModel):
    """
    Paginated response envelope for the activity feed.

    Pass ``next_cursor`` as the ``cursor`` query parameter to retrieve the
    following page.  ``next_cursor`` is ``null`` when the end of the feed has
    been reached for the current filter combination.
    """

    items: List[ActivityEventOut]
    next_cursor: Optional[str] = Field(
        None,
        description=(
            "Opaque string to pass as ?cursor= to retrieve the next page. "
            "Null when no further results exist."
        ),
    )
    total_returned: int = Field(
        ...,
        description="Number of items in this response (may be less than the requested limit).",
    )

    class Config:
        orm_mode = True


# ---------------------------------------------------------------------------
# Query-parameter model (used for documentation / validation)
# ---------------------------------------------------------------------------


class ActivityFeedParams(BaseModel):
    """
    Validated query parameters for GET /projects/{project_id}/activity.
    Kept as a standalone model so it can be reused by tests and client SDKs.
    """

    limit: int = Field(25, ge=1, le=50)
    cursor: Optional[str] = None
    event_types: Optional[List[EventType]] = None
    actor_id: Optional[UUID] = None
    after: Optional[datetime] = None
    before: Optional[datetime] = None

    @validator("before")
    def before_must_be_after_after(  # noqa: N805
        cls, v: Optional[datetime], values: Dict[str, Any]
    ) -> Optional[datetime]:
        after_val = values.get("after")
        if v is not None and after_val is not None and v <= after_val:
            raise ValueError("'before' must be strictly later than 'after'")
        return v
