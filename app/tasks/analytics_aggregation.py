from __future__ import annotations

"""Celery tasks for pre-computing analytics snapshots.

Scheduled via Celery Beat to run hourly. Snapshots are written to the
`analytics_snapshots` table and picked up by the read endpoints when a
cache miss occurs and a fresh DB query would be too expensive.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

from celery import shared_task
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.core.celery_app import celery_app
from app.core.database import SessionLocal
from app.models.activity import ActivityEvent
from app.models.analytics_snapshot import AnalyticsSnapshot
from app.models.project import Project
from app.models.sprint import Sprint
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.time_entry import TimeEntry
from app.models.user import User

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

@celery_app.task(name="analytics.aggregate_hourly_snapshots", bind=True, max_retries=3)
def aggregate_hourly_snapshots(self) -> Dict[str, Any]:
    """
    Fan-out task: iterates over all active tenants and dispatches
    per-tenant aggregation subtasks in parallel.
    """
    db: Session = SessionLocal()
    try:
        tenants = db.query(Tenant).filter(Tenant.is_active == True).all()  # noqa: E712
        logger.info("Dispatching aggregation for %d tenants", len(tenants))
        for tenant in tenants:
            aggregate_tenant_snapshots.delay(tenant_id=tenant.id)
        return {"dispatched": len(tenants), "at": datetime.utcnow().isoformat()}
    except Exception as exc:
        logger.exception("Failed to dispatch tenant aggregation tasks")
        raise self.retry(exc=exc, countdown=60)
    finally:
        db.close()


@celery_app.task(
    name="analytics.aggregate_tenant_snapshots",
    bind=True,
    max_retries=3,
    soft_time_limit=300,
)
def aggregate_tenant_snapshots(self, tenant_id: int) -> Dict[str, Any]:
    """
    Compute and upsert analytics snapshots for a single tenant covering
    the rolling 24-hour window ending now.
    """
    db: Session = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
        if not tenant:
            logger.warning("Tenant %d not found, skipping", tenant_id)
            return {"skipped": True}

        window_end = datetime.utcnow()
        window_start = window_end - timedelta(hours=24)

        activity_stats = _compute_activity_stats(db, tenant, window_start, window_end)
        velocity_stats = _compute_velocity_stats(db, tenant, window_start, window_end)
        time_stats = _compute_time_stats(db, tenant, window_start, window_end)

        _upsert_snapshot(
            db,
            tenant_id=tenant_id,
            snapshot_type="activity",
            window_start=window_start,
            window_end=window_end,
            payload=activity_stats,
        )
        _upsert_snapshot(
            db,
            tenant_id=tenant_id,
            snapshot_type="velocity",
            window_start=window_start,
            window_end=window_end,
            payload=velocity_stats,
        )
        _upsert_snapshot(
            db,
            tenant_id=tenant_id,
            snapshot_type="time_tracking",
            window_start=window_start,
            window_end=window_end,
            payload=time_stats,
        )

        db.commit()
        logger.info(
            "Snapshots committed for tenant %d (window %s – %s)",
            tenant_id,
            window_start.isoformat(),
            window_end.isoformat(),
        )
        return {
            "tenant_id": tenant_id,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "activity_events": activity_stats.get("total_events"),
            "velocity_sprints": velocity_stats.get("sprint_count"),
            "time_hours": time_stats.get("total_hours"),
        }
    except Exception as exc:
        db.rollback()
        logger.exception("Aggregation failed for tenant %d", tenant_id)
        raise self.retry(exc=exc, countdown=120)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Computation helpers
# ---------------------------------------------------------------------------

def _compute_activity_stats(
    db: Session,
    tenant: Tenant,
    start: datetime,
    end: datetime,
) -> Dict[str, Any]:
    rows = (
        db.query(
            ActivityEvent.event_type,
            func.count(ActivityEvent.id).label("count"),
        )
        .filter(
            ActivityEvent.tenant_id == tenant.id,
            ActivityEvent.occurred_at >= start,
            ActivityEvent.occurred_at <= end,
        )
        .group_by(ActivityEvent.event_type)
        .all()
    )

    breakdown: Dict[str, int] = {r.event_type: r.count for r in rows}
    total = sum(breakdown.values())

    unique_users = (
        db.query(func.count(func.distinct(ActivityEvent.user_id)))
        .filter(
            ActivityEvent.tenant_id == tenant.id,
            ActivityEvent.occurred_at >= start,
            ActivityEvent.occurred_at <= end,
        )
        .scalar()
        or 0
    )

    return {
        "total_events": total,
        "unique_active_users": unique_users,
        "breakdown_by_type": breakdown,
    }


def _compute_velocity_stats(
    db: Session,
    tenant: Tenant,
    start: datetime,
    end: datetime,
) -> Dict[str, Any]:
    sprints = (
        db.query(Sprint)
        .filter(
            Sprint.tenant_id == tenant.id,
            Sprint.end_date >= start,
            Sprint.end_date <= end,
        )
        .all()
    )

    if not sprints:
        return {"sprint_count": 0, "avg_completion_rate": 0, "total_points_completed": 0}

    total_completed = 0
    completion_rates: List[float] = []

    for sprint in sprints:
        tasks = (
            db.query(Task)
            .filter(Task.sprint_id == sprint.id, Task.tenant_id == tenant.id)
            .all()
        )
        planned = sum(t.story_points or 0 for t in tasks)
        completed = sum(
            t.story_points or 0 for t in tasks if t.status == "done"
        )
        total_completed += completed
        if planned:
            completion_rates.append(completed / planned * 100)

    avg_rate = sum(completion_rates) / len(completion_rates) if completion_rates else 0

    return {
        "sprint_count": len(sprints),
        "avg_completion_rate": round(avg_rate, 2),
        "total_points_completed": total_completed,
    }


def _compute_time_stats(
    db: Session,
    tenant: Tenant,
    start: datetime,
    end: datetime,
) -> Dict[str, Any]:
    entries = (
        db.query(TimeEntry)
        .filter(
            TimeEntry.tenant_id == tenant.id,
            TimeEntry.started_at >= start,
            TimeEntry.started_at <= end,
        )
        .all()
    )

    total_seconds = sum(
        (e.ended_at - e.started_at).total_seconds()
        for e in entries
        if e.ended_at
    )
    billable_seconds = sum(
        (e.ended_at - e.started_at).total_seconds()
        for e in entries
        if e.ended_at and e.billable
    )

    active_user_ids = {e.user_id for e in entries}

    return {
        "total_hours": round(total_seconds / 3600, 2),
        "billable_hours": round(billable_seconds / 3600, 2),
        "non_billable_hours": round((total_seconds - billable_seconds) / 3600, 2),
        "unique_users_tracked": len(active_user_ids),
        "entry_count": len(entries),
    }


# ---------------------------------------------------------------------------
# Upsert helper
# ---------------------------------------------------------------------------

def _upsert_snapshot(
    db: Session,
    *,
    tenant_id: int,
    snapshot_type: str,
    window_start: datetime,
    window_end: datetime,
    payload: Dict[str, Any],
) -> None:
    """
    Insert or update an AnalyticsSnapshot row. Uniqueness is keyed on
    (tenant_id, snapshot_type, window_start) so re-runs are idempotent.
    """
    existing = (
        db.query(AnalyticsSnapshot)
        .filter(
            AnalyticsSnapshot.tenant_id == tenant_id,
            AnalyticsSnapshot.snapshot_type == snapshot_type,
            AnalyticsSnapshot.window_start == window_start,
        )
        .with_for_update()
        .first()
    )

    if existing:
        existing.window_end = window_end
        existing.payload = payload
        existing.updated_at = datetime.utcnow()
    else:
        snapshot = AnalyticsSnapshot(
            tenant_id=tenant_id,
            snapshot_type=snapshot_type,
            window_start=window_start,
            window_end=window_end,
            payload=payload,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(snapshot)
