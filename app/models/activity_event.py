from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class EventType(str, enum.Enum):
    task_created = "task_created"
    task_updated = "task_updated"
    task_completed = "task_completed"
    task_deleted = "task_deleted"
    comment_added = "comment_added"
    comment_edited = "comment_edited"
    comment_deleted = "comment_deleted"
    file_attached = "file_attached"
    file_removed = "file_removed"
    member_added = "member_added"
    member_removed = "member_removed"
    member_role_changed = "member_role_changed"
    project_updated = "project_updated"


class TargetEntityType(str, enum.Enum):
    task = "task"
    comment = "comment"
    file = "file"
    member = "member"
    project = "project"


class ActivityEvent(Base):
    """
    Normalised activity log entry for a project.

    Each row captures a single user action. The ``meta`` column carries
    event-specific detail (e.g. previous/next field values for an update)
    without requiring a separate table per event type.
    """

    __tablename__ = "activity_events"

    id: uuid.UUID = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        nullable=False,
    )
    tenant_id: uuid.UUID = Column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: uuid.UUID = Column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: uuid.UUID = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,  # system-generated events have no actor
        index=True,
    )
    event_type: EventType = Column(
        Enum(EventType, name="activity_event_type"),
        nullable=False,
    )
    target_entity_type: TargetEntityType = Column(
        Enum(TargetEntityType, name="activity_target_entity_type"),
        nullable=False,
    )
    target_entity_id: uuid.UUID = Column(
        UUID(as_uuid=True),
        nullable=True,
    )
    # Human-readable summary pre-rendered at write time so the read path
    # does not need to reconstruct it from raw field diffs.
    summary: str = Column(Text, nullable=False, default="")
    meta: Dict[str, Any] = Column(JSONB, nullable=False, server_default="{}")
    created_at: datetime = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    # Relationships — loaded lazily by default; callers that need them
    # should use explicit eager-load options on the query.
    actor = relationship(
        "User",
        foreign_keys=[actor_id],
        lazy="select",
    )
    project = relationship(
        "Project",
        foreign_keys=[project_id],
        lazy="select",
    )

    __table_args__ = (
        # Primary access pattern: newest events for a project, scoped to tenant.
        Index(
            "ix_activity_events_project_created_at",
            "project_id",
            "created_at",
            postgresql_ops={"created_at": "DESC"},
        ),
        # Secondary pattern: all events by a specific actor within a project.
        Index(
            "ix_activity_events_actor_project",
            "actor_id",
            "project_id",
        ),
        # Tenant-scoped sweep used by admin tooling.
        Index(
            "ix_activity_events_tenant_created_at",
            "tenant_id",
            "created_at",
        ),
        {"schema": None},
    )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<ActivityEvent id={self.id} type={self.event_type} "
            f"project={self.project_id}>"
        )
