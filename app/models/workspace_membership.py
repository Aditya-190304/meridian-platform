import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import (
    Column,
    String,
    DateTime,
    ForeignKey,
    Enum,
    UniqueConstraint,
    Index,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.core.database import Base


class MemberRole(str, PyEnum):
    viewer = "viewer"
    editor = "editor"
    admin = "admin"
    owner = "owner"


class InviteStatus(str, PyEnum):
    pending = "pending"
    accepted = "accepted"
    declined = "declined"
    expired = "expired"
    revoked = "revoked"


class WorkspaceMembership(Base):
    """
    Tracks membership of users within a workspace.

    A single user can have at most one active membership per workspace.
    Pending invites are also stored here; they transition to 'accepted'
    when the invitee clicks the link and authenticates.

    Columns
    -------
    id                : PK, UUID
    workspace_id      : FK -> workspaces.id
    user_id           : FK -> users.id (null while invite is still pending)
    invited_by_id     : FK -> users.id (who sent the invite)
    invited_email     : email address the invite was sent to
    role              : viewer | editor | admin | owner
    status            : pending | accepted | declined | expired | revoked
    invite_token      : opaque token embedded in the invite link
    invite_expires_at : when the invite link becomes invalid
    joined_at         : when the member accepted the invite
    role_updated_at   : last time the role was changed
    created_at        : row creation time
    updated_at        : last row modification time
    """

    __tablename__ = "workspace_memberships"

    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("gen_random_uuid()"),
    )
    workspace_id = Column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,  # null until invite accepted
    )
    invited_by_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    invited_email = Column(String(254), nullable=False, index=True)
    role = Column(
        Enum(MemberRole, name="member_role_enum"),
        nullable=False,
        default=MemberRole.viewer,
    )
    status = Column(
        Enum(InviteStatus, name="invite_status_enum"),
        nullable=False,
        default=InviteStatus.pending,
        index=True,
    )
    invite_token = Column(String(64), nullable=True, unique=True, index=True)
    invite_expires_at = Column(DateTime(timezone=True), nullable=True)
    joined_at = Column(DateTime(timezone=True), nullable=True)
    role_updated_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Relationships
    workspace = relationship("Workspace", back_populates="memberships", lazy="select")
    user = relationship(
        "User",
        foreign_keys=[user_id],
        back_populates="workspace_memberships",
        lazy="select",
    )
    invited_by = relationship(
        "User",
        foreign_keys=[invited_by_id],
        lazy="select",
    )

    __table_args__ = (
        # One active membership per user per workspace
        UniqueConstraint(
            "workspace_id",
            "user_id",
            name="uq_workspace_membership_user",
            postgresql_where=text("status = 'accepted'"),
        ),
        # One pending invite per email per workspace
        UniqueConstraint(
            "workspace_id",
            "invited_email",
            name="uq_workspace_membership_email_pending",
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_workspace_memberships_workspace_status", "workspace_id", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<WorkspaceMembership id={self.id} "
            f"workspace={self.workspace_id} user={self.user_id} "
            f"role={self.role} status={self.status}>"
        )

    @property
    def is_active(self) -> bool:
        return self.status == InviteStatus.accepted

    @property
    def invite_is_expired(self) -> bool:
        if self.invite_expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.invite_expires_at
