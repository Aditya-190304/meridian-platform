"""Pydantic schemas and SQLAlchemy models for the Notifications service."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, EmailStr, Field, validator
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ChannelType(str, enum.Enum):
    EMAIL = "email"
    SMS = "sms"
    SLACK = "slack"


class DeliveryStatus(str, enum.Enum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    DELIVERED = "delivered"
    RETRYING = "retrying"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# SQLAlchemy ORM Models
# ---------------------------------------------------------------------------


class NotificationRecord(Base):
    """Persisted delivery record for a single notification attempt."""

    __tablename__ = "notifications"

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    channel = Column(String(16), nullable=False)
    recipient = Column(String(512), nullable=False)
    template_id = Column(String(36), nullable=True)
    payload_hash = Column(String(16), nullable=False)
    status = Column(String(16), nullable=False, default=DeliveryStatus.QUEUED)
    attempt_count = Column(Integer, nullable=False, default=0)
    provider_message_id = Column(String(256), nullable=True)
    last_error = Column(Text, nullable=True)
    next_attempt_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    dispatched_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class TemplateRecord(Base):
    """Jinja2 notification template scoped to a tenant."""

    __tablename__ = "notification_templates"

    id = Column(String(36), primary_key=True)
    tenant_id = Column(String(36), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    channel = Column(String(16), nullable=False)
    subject = Column(String(256), nullable=True)  # email only
    body = Column(Text, nullable=False)
    active = Column(Boolean, nullable=False, default=True)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Pydantic request / response schemas
# ---------------------------------------------------------------------------


class RetryPolicy(BaseModel):
    max_attempts: int = Field(4, ge=1, le=10)
    base_delay_seconds: int = Field(30, ge=5, le=300)
    backoff_multiplier: float = Field(2.5, ge=1.0, le=10.0)
    max_delay_seconds: int = Field(600, ge=60, le=3600)


class NotificationCreate(BaseModel):
    tenant_id: str = Field(..., description="Owning tenant UUID")
    channel: ChannelType
    recipient: str = Field(
        ...,
        description="Email address, E.164 phone number, or Slack webhook URL",
    )
    template_id: Optional[str] = Field(
        None,
        description="Template ID to render. Mutually exclusive with body/subject.",
    )
    subject: Optional[str] = Field(None, description="Email subject (raw, no template)")
    body: Optional[str] = Field(None, description="Message body (raw, no template)")
    context: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Template variable context dict",
    )
    retry_policy: Optional[RetryPolicy] = None

    @validator("recipient")
    def recipient_not_empty(cls, v: str) -> str:  # noqa: N805
        if not v.strip():
            raise ValueError("recipient must not be blank")
        return v.strip()

    @validator("body", always=True)
    def body_or_template_required(cls, v: Optional[str], values: Dict[str, Any]) -> Optional[str]:  # noqa: N805
        if not v and not values.get("template_id"):
            raise ValueError("Either body or template_id must be provided")
        return v


class NotificationResponse(BaseModel):
    notification_id: str
    status: DeliveryStatus
    channel: ChannelType
    recipient: str
    attempt_count: int
    created_at: datetime
    dispatched_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
    last_error: Optional[str] = None

    class Config:
        from_attributes = True


class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    channel: ChannelType
    subject: Optional[str] = Field(None, max_length=256)
    body: str = Field(..., min_length=1)

    @validator("subject", always=True)
    def subject_required_for_email(cls, v: Optional[str], values: Dict[str, Any]) -> Optional[str]:  # noqa: N805
        if values.get("channel") == ChannelType.EMAIL and not v:
            raise ValueError("subject is required for email templates")
        return v


class TemplateResponse(BaseModel):
    id: str
    tenant_id: str
    name: str
    channel: ChannelType
    subject: Optional[str] = None
    version: int
    active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SlackEventCallback(BaseModel):
    """Inbound payload from Slack Events API."""

    token: Optional[str] = None
    team_id: Optional[str] = None
    type: str
    event: Optional[Dict[str, Any]] = None
    challenge: Optional[str] = None  # URL verification handshake
    event_id: Optional[str] = None
    event_time: Optional[int] = None


class DeliveryStatusResponse(BaseModel):
    notification_id: str
    status: DeliveryStatus
    attempt_count: int
    provider_message_id: Optional[str] = None
    last_error: Optional[str] = None
    next_attempt_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None
