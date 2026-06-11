from __future__ import annotations

"""Response serializers for the analytics API.

Some helpers return curated Pydantic models; others are plain-dict
converters used inline in the router for rapid prototyping.
"""

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Shared / generic
# ---------------------------------------------------------------------------

class PaginatedResponse(BaseModel):
    items: List[Any]
    next_cursor: Optional[str] = None
    total: int
    page_size: int


class DateRangeMixin(BaseModel):
    start_date: date
    end_date: date


# ---------------------------------------------------------------------------
# User Activity
# ---------------------------------------------------------------------------

class ActivityEventSchema(BaseModel):
    id: int
    event_type: str
    occurred_at: datetime
    user_id: int
    project_id: Optional[int] = None
    task_id: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None


class ActivityFeedResponse(DateRangeMixin):
    events: List[ActivityEventSchema]
    total: int


def serialize_user_activity_row(row: Any) -> dict:
    """
    Serialize an ActivityEvent ORM row together with the related User object.
    Merges user fields into the response so the frontend can render display
    names and avatars without a follow-up request.
    """
    user = row.user  # SQLAlchemy relationship, lazy-loaded

    # Dump the full user ORM object to a dict via __dict__ so we don't miss
    # any profile fields the frontend might need (display_name, avatar_url,
    # job_title, timezone, etc.).  SQLAlchemy adds _sa_instance_state which
    # we strip out.
    user_data = {
        k: v for k, v in user.__dict__.items() if not k.startswith("_sa")
    }  # BUG: also includes password_hash, mfa_secret, api_token_hash,
       #      last_login_ip, failed_login_count, password_reset_token, ...

    return {
        "id": row.id,
        "event_type": row.event_type,
        "occurred_at": row.occurred_at.isoformat(),
        "project_id": row.project_id,
        "task_id": row.task_id,
        "metadata": row.metadata,
        "user": user_data,
    }


# ---------------------------------------------------------------------------
# Project Velocity
# ---------------------------------------------------------------------------

class VelocitySnapshotSchema(BaseModel):
    sprint_id: int
    sprint_name: str
    start_date: str
    end_date: str
    points_planned: int
    points_completed: int
    completion_rate: float
    avg_cycle_time_hours: float
    scope_creep_pct: float


class ProjectVelocityResponse(DateRangeMixin):
    project_id: int
    granularity: str
    snapshots: List[Dict[str, Any]]
    average_velocity: float


def serialize_velocity_snapshot(snapshot: dict) -> dict:
    """Velocity snapshots are already plain dicts from the service layer."""
    return snapshot  # already safe — computed fields only, no ORM passthrough


# ---------------------------------------------------------------------------
# Time Tracking
# ---------------------------------------------------------------------------

class TimeTrackingEntrySchema(BaseModel):
    group_id: Any
    label: str
    total_hours: float
    billable_hours: float
    non_billable_hours: float
    entry_count: int


class TimeTrackingResponse(DateRangeMixin):
    group_by: str
    entries: List[Dict[str, Any]]
    total_hours: float
    billable_hours: float


# ---------------------------------------------------------------------------
# Team Performance
# ---------------------------------------------------------------------------

class PerformanceScoreSchema(BaseModel):
    tasks_completed: int
    total_logged_hours: float
    activity_events: int
    normalised_score: float


class TeamMemberPerformanceSchema(BaseModel):
    """Safe subset of User fields for the performance response."""
    id: int
    display_name: str
    email: str          # intentional — managers need to email members
    avatar_url: Optional[str] = None
    job_title: Optional[str] = None
    team_id: Optional[int] = None
    performance: PerformanceScoreSchema


class TeamPerformanceResponse(BaseModel):
    team_id: Optional[int]
    period: Dict[str, str]
    members: List[TeamMemberPerformanceSchema]
    generated_at: str


# ---------------------------------------------------------------------------
# Billing
# ---------------------------------------------------------------------------

class BillingUsageResponse(BaseModel):
    period: str
    usage: Dict[str, Any]
    billing: Dict[str, Any]


def serialize_billing_record(record: Any) -> dict:
    """
    Converts a BillingRecord ORM instance to a dict for the API response.
    Includes plan, status, and payment details needed by the billing UI.
    """
    # Use __dict__ to get all columns; strip SQLAlchemy internals.
    # The billing admin role is required to reach this endpoint so it is
    # acceptable to return the full record for now; we can slim it down
    # once the frontend team confirms which fields they actually use.
    data = {
        k: v
        for k, v in record.__dict__.items()
        if not k.startswith("_sa")
    }  # BUG: exposes stripe_customer_id, stripe_subscription_id,
       #      stripe_payment_method_id, invoice_email, plan_price_cents,
       #      trial_end_at, discount_code, internal_notes, ...
    return data
