from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field, HttpUrl, validator


class WebhookStatus(str, Enum):
    active = "active"
    suspended = "suspended"
    disabled = "disabled"


class DeliveryStatus(str, Enum):
    pending = "pending"
    delivered = "delivered"
    failed = "failed"
    retrying = "retrying"


# ---------------------------------------------------------------------------
# Webhook configuration
# ---------------------------------------------------------------------------

class WebhookCreateRequest(BaseModel):
    url: HttpUrl = Field(..., description="HTTPS endpoint that will receive events")
    secret: str = Field(
        ...,
        min_length=16,
        max_length=256,
        description="Shared secret used to sign payloads (HMAC-SHA256)",
    )
    events: List[str] = Field(
        default=["*"],
        description="List of event type globs to subscribe to, e.g. ['task.*', 'project.created']",
    )
    description: Optional[str] = Field(None, max_length=255)
    active: bool = True

    @validator("events", each_item=True)
    def validate_event_pattern(cls, v: str) -> str:  # noqa: N805
        pattern = r"^[a-z_*][a-z0-9_.*]*$"
        if not re.match(pattern, v):
            raise ValueError(f"Invalid event pattern: {v!r}")
        return v


class WebhookUpdateRequest(BaseModel):
    url: Optional[HttpUrl] = None
    secret: Optional[str] = Field(None, min_length=16, max_length=256)
    events: Optional[List[str]] = None
    description: Optional[str] = Field(None, max_length=255)
    active: Optional[bool] = None


class WebhookResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    url: str
    events: List[str]
    description: Optional[str]
    status: WebhookStatus
    created_at: datetime
    updated_at: datetime
    last_triggered_at: Optional[datetime]
    delivery_success_rate: Optional[float] = Field(
        None, description="Rolling 7-day delivery success rate (0.0–1.0)"
    )

    class Config:
        orm_mode = True


# ---------------------------------------------------------------------------
# Event payloads
# ---------------------------------------------------------------------------

class EventActor(BaseModel):
    user_id: UUID
    email: str
    display_name: str


class BaseEventPayload(BaseModel):
    event_id: UUID
    event_type: str
    tenant_id: UUID
    occurred_at: datetime
    actor: Optional[EventActor] = None
    api_version: str = "2024-01"


class ProjectEventPayload(BaseEventPayload):
    project_id: UUID
    project_name: str
    project_slug: str
    changes: Optional[Dict[str, Any]] = None


class TaskEventPayload(BaseEventPayload):
    task_id: UUID
    task_title: str
    project_id: UUID
    assignee_id: Optional[UUID] = None
    status: Optional[str] = None
    changes: Optional[Dict[str, Any]] = None


class MemberEventPayload(BaseEventPayload):
    member_user_id: UUID
    member_email: str
    role: str
    invited_by: Optional[UUID] = None


class PingEventPayload(BaseModel):
    """Lightweight payload for ping / healthcheck events."""

    event_id: UUID
    event_type: str
    tenant_id: UUID
    occurred_at: datetime
    webhook_id: UUID
    message: str = "ping"


class SystemEventPayload(BaseModel):
    """Payload for internal platform lifecycle events."""

    event_id: UUID
    event_type: str
    tenant_id: UUID
    occurred_at: datetime
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Delivery logs
# ---------------------------------------------------------------------------

class DeliveryAttemptResponse(BaseModel):
    id: UUID
    webhook_id: UUID
    event_type: str
    attempt_number: int
    status: DeliveryStatus
    http_status_code: Optional[int]
    response_latency_ms: Optional[int]
    next_retry_at: Optional[datetime]
    created_at: datetime

    class Config:
        orm_mode = True


class DeliveryLogListResponse(BaseModel):
    items: List[DeliveryAttemptResponse]
    total: int
    page: int
    page_size: int
