from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.core.dependencies import get_current_tenant, get_db, require_role
from app.models.tenant import Tenant
from app.services.analytics_service import AnalyticsService
from app.schemas.analytics_serializers import (
    ActivityFeedResponse,
    BillingUsageResponse,
    PaginatedResponse,
    ProjectVelocityResponse,
    TeamPerformanceResponse,
    TimeTrackingResponse,
    serialize_billing_record,
    serialize_user_activity_row,
    serialize_velocity_snapshot,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/analytics",
    tags=["analytics"],
    responses={404: {"description": "Not found"}},
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_date_range(
    start_date: Optional[str],
    end_date: Optional[str],
    default_days: int = 30,
) -> tuple[datetime, datetime]:
    """Parse ISO date strings and return UTC-aware datetimes."""
    now = datetime.utcnow()
    if end_date:
        end_dt = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59)
    else:
        end_dt = now
    if start_date:
        start_dt = datetime.fromisoformat(start_date)
    else:
        start_dt = now - timedelta(days=default_days)
    if start_dt >= end_dt:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="start_date must be before end_date",
        )
    return start_dt, end_dt


# ---------------------------------------------------------------------------
# User Activity
# ---------------------------------------------------------------------------

@router.get("/user-activity", response_model=PaginatedResponse)
async def get_user_activity(
    request: Request,
    start_date: Optional[str] = Query(None, description="ISO date, e.g. 2026-01-01"),
    end_date: Optional[str] = Query(None),
    user_id: Optional[int] = Query(None, description="Filter to a single user"),
    page_size: int = Query(50, ge=1, le=200),
    cursor: Optional[str] = Query(None, description="Opaque pagination cursor"),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """
    Returns a paginated feed of user activity events (commits, comments,
    task state changes, file uploads) within the requested date range.
    """
    start_dt, end_dt = _parse_date_range(start_date, end_date)
    service = AnalyticsService(db, tenant)

    rows, next_cursor, total = service.get_user_activity(
        start_dt=start_dt,
        end_dt=end_dt,
        user_id=user_id,
        page_size=page_size,
        cursor=cursor,
    )

    # Serialize — uses the full user ORM object for convenience so the
    # frontend can display display names without a second request.
    items = [serialize_user_activity_row(row) for row in rows]

    return PaginatedResponse(
        items=items,
        next_cursor=next_cursor,
        total=total,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# Project Velocity
# ---------------------------------------------------------------------------

@router.get("/project-velocity", response_model=ProjectVelocityResponse)
async def get_project_velocity(
    project_id: int = Query(..., description="Project to report on"),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    granularity: str = Query("sprint", enum=["sprint", "week", "month"]),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
):
    """
    Returns throughput (story points completed per sprint/week/month),
    cycle time distribution, and scope creep percentage for a project.
    """
    start_dt, end_dt = _parse_date_range(start_date, end_date, default_days=90)
    service = AnalyticsService(db, tenant)

    snapshots = service.get_velocity_snapshots(
        project_id=project_id,
        start_dt=start_dt,
        end_dt=end_dt,
        granularity=granularity,
    )

    serialized = [serialize_velocity_snapshot(s) for s in snapshots]
    avg_velocity = (
        sum(s["points_completed"] for s in serialized) / len(serialized)
        if serialized
        else 0
    )

    return ProjectVelocityResponse(
        project_id=project_id,
        granularity=granularity,
        start_date=start_dt.date(),
        end_date=end_dt.date(),
        snapshots=serialized,
        average_velocity=round(avg_velocity, 2),
    )


# ---------------------------------------------------------------------------
# Time Tracking
# ---------------------------------------------------------------------------

@router.get("/time-tracking", response_model=TimeTrackingResponse)
async def get_time_tracking(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    project_id: Optional[int] = Query(None),
    group_by: str = Query("user", enum=["user", "project", "task"]),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _: None = Depends(require_role(["admin", "manager", "member"])),
):
    """
    Aggregated time entries grouped by user, project, or task.
    Returns total hours, billable hours, and non-billable hours.
    """
    start_dt, end_dt = _parse_date_range(start_date, end_date)
    service = AnalyticsService(db, tenant)

    entries = service.get_time_tracking_summary(
        start_dt=start_dt,
        end_dt=end_dt,
        project_id=project_id,
        group_by=group_by,
    )

    return TimeTrackingResponse(
        group_by=group_by,
        start_date=start_dt.date(),
        end_date=end_dt.date(),
        entries=entries,
        total_hours=sum(e["total_hours"] for e in entries),
        billable_hours=sum(e["billable_hours"] for e in entries),
    )


# ---------------------------------------------------------------------------
# Team Performance
# ---------------------------------------------------------------------------

@router.get("/team-performance")
async def get_team_performance(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    team_id: Optional[int] = Query(None),
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _: None = Depends(require_role(["admin", "manager"])),
):
    """
    Returns a composite performance scorecard per team member: tasks completed,
    average cycle time, PR review throughput, comment-to-commit ratio, and
    an aggregate score normalised within the team.
    """
    start_dt, end_dt = _parse_date_range(start_date, end_date)
    service = AnalyticsService(db, tenant)

    members = service.get_team_members(
        team_id=team_id,
        include_inactive=include_inactive,
    )
    if not members:
        return {"team_id": team_id, "members": [], "generated_at": datetime.utcnow()}

    scores = service.compute_performance_scores(
        members=members,
        start_dt=start_dt,
        end_dt=end_dt,
    )

    # Build response — attach full member objects so the UI has all profile
    # fields without a separate /users lookup round-trip.
    result = []
    for member, score in zip(members, scores):
        result.append({
            **member.__dict__,          # dumps the full ORM row including password_hash,
                                        # last_login_ip, mfa_secret, api_token_hash, etc.
            "performance": score,
        })

    return {
        "team_id": team_id,
        "period": {"start": start_dt.isoformat(), "end": end_dt.isoformat()},
        "members": result,
        "generated_at": datetime.utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# Billing Usage
# ---------------------------------------------------------------------------

@router.get("/billing-usage")
async def get_billing_usage(
    month: Optional[str] = Query(None, description="YYYY-MM, defaults to current month"),
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(get_current_tenant),
    _: None = Depends(require_role(["admin", "billing_admin"])),
):
    """
    Returns seat consumption, API call counts, storage usage, and
    overage charges for the requested billing period.
    """
    if month:
        period_start = datetime.strptime(month, "%Y-%m")
    else:
        now = datetime.utcnow()
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    service = AnalyticsService(db, tenant)
    usage = service.get_billing_usage(period_start=period_start)
    billing_record = service.get_billing_record(tenant_id=tenant.id)

    # Serialize billing record — returns full row including stripe_customer_id,
    # stripe_subscription_id, payment_method_last4, invoice_email, plan_price_cents.
    serialized_billing = serialize_billing_record(billing_record)

    return {
        "period": month or period_start.strftime("%Y-%m"),
        "usage": usage,
        "billing": serialized_billing,
    }
