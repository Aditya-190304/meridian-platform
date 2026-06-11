"""Pydantic schemas for the presence feature."""

from __future__ import annotations

from datetime import datetime
from enum import str as str_enum
from enum import auto
from typing import Optional

from pydantic import BaseModel, Field


class PresenceStatus(str_enum):
    """Enumeration of user presence states."""

    ONLINE = "online"
    OFFLINE = "offline"
    AWAY = "away"  # reserved for future idle-detection


# ---------------------------------------------------------------------------
# Member presence
# ---------------------------------------------------------------------------


class PresenceMemberDetail(BaseModel):
    """Presence detail for a single workspace member."""

    user_id: str = Field(..., description="User UUID")
    display_name: str = Field(..., description="User's display name")
    avatar_url: Optional[str] = Field(None, description="Avatar image URL")
    email: str = Field(..., description="User email address")
    status: PresenceStatus = Field(..., description="Current presence status")
    is_online: bool = Field(..., description="True when the user is currently online")
    last_seen_at: Optional[datetime] = Field(
        None,
        description="UTC timestamp of the user's last recorded activity",
    )

    class Config:
        from_attributes = True


class WorkspacePresenceResponse(BaseModel):
    """Full presence snapshot for an entire workspace."""

    workspace_id: str = Field(..., description="Workspace UUID")
    members: list[PresenceMemberDetail] = Field(
        default_factory=list,
        description="Presence detail for every active member",
    )
    online_count: int = Field(..., description="Number of currently-online members")
    total_count: int = Field(..., description="Total number of active members")


class OnlineCountResponse(BaseModel):
    """Lightweight response for the online-count badge."""

    workspace_id: str
    online_count: int
    total_count: int


class PresenceStatusResponse(BaseModel):
    """Presence status for a single user in a single workspace."""

    user_id: str
    workspace_id: str
    is_online: bool
    last_seen_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Typing indicators
# ---------------------------------------------------------------------------


class TypingIndicator(BaseModel):
    """Represents a member who is actively typing in a thread."""

    user_id: str = Field(..., description="User UUID")
    display_name: str = Field(..., description="User's display name")
    avatar_url: Optional[str] = Field(None)
    thread_id: str = Field(..., description="Comment thread UUID")
    started_at: datetime = Field(
        ..., description="UTC timestamp when typing was last signalled"
    )


class TypingIndicatorList(BaseModel):
    """List of typing indicators for a comment thread."""

    thread_id: str
    typing: list[TypingIndicator] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# WebSocket event payloads
# ---------------------------------------------------------------------------


class WsPresenceEvent(BaseModel):
    """Outbound WebSocket message for a presence state change."""

    event: str = Field(
        "presence.update",
        description="Event type discriminator",
    )
    user_id: str
    workspace_id: str
    is_online: bool
    status: PresenceStatus
    last_seen_at: Optional[datetime] = None


class WsTypingEvent(BaseModel):
    """Outbound WebSocket message for a typing-indicator change."""

    event: str = Field("presence.typing")
    user_id: str
    display_name: str
    thread_id: str
    is_typing: bool


class WsHeartbeatAck(BaseModel):
    """Server acknowledgement of a client heartbeat message."""

    event: str = Field("presence.heartbeat_ack")
    server_time: datetime


# ---------------------------------------------------------------------------
# Inbound WebSocket message shapes
# ---------------------------------------------------------------------------


class WsInboundMessage(BaseModel):
    """Generic container for inbound WebSocket messages."""

    type: str = Field(
        ...,
        description="Message type: heartbeat | typing_start | typing_stop",
    )
    thread_id: Optional[str] = Field(
        None,
        description="Required for typing_start and typing_stop messages",
    )
