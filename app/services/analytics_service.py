from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from typing import Any, List, Optional, Tuple

from sqlalchemy import func, and_, or_, text
from sqlalchemy.orm import Session

from app.models.activity import ActivityEvent
from app.models.billing import BillingRecord
from app.models.project import Project
from app.models.sprint import Sprint
from app.models.task import Task
from app.models.tenant import Tenant
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.core.cache import redis_client

logger = logging.getLogger(__name__)

CACHE_TTL_ACTIVITY = 30          # seconds
CACHE_TTL_VELOCITY = 120
CACHE_TTL_TIME_TRACKING = 60
CACHE_TTL_PERFORMANCE = 300
CACHE_TTL_BILLING = 60


class AnalyticsService:
    def __init__(self, db: Session, tenant: Tenant) -> None:
        self.db = db
        self.tenant = tenant
        self._cache_ns = f"analytics:{tenant.id}"

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_key(self, *parts: Any) -> str:
        raw = ":".join(str(p) for p in parts)
        digest = hashlib.md5(raw.encode()).hexdigest()[:12]
        return f"{self._cache_ns}:{digest}"

    def _get_cached(self, key: str) -> Optional[Any]:
        try:
            val = redis_client.get(key)
            return json.loads(val) if val else None
        except Exception as exc:
            logger.warning("Cache read failed: %s", exc)
            return None

    def _set_cached(self, key: str, value: Any, ttl: int) -> None:
        try:
            redis_client.setex(key, ttl, json.dumps(value, default=str))
        except Exception as exc:
            logger.warning("Cache write failed: %s", exc)

    # ------------------------------------------------------------------
    # User Activity
    # ------------------------------------------------------------------

    def get_user_activity(
        self,
        start_dt: datetime,
        end_dt: datetime,
        user_id: Optional[int],
        page_size: int,
        cursor: Optional[str],
    ) -> Tuple[List[ActivityEvent], Optional[str], int]:
        cache_key = self._cache_key(
            "activity", start_dt, end_dt, user_id, page_size, cursor
        )
        cached = self._get_cached(cache_key)
        if cached:
            return cached["rows"], cached["next_cursor"], cached["total"]

        q = (
            self.db.query(ActivityEvent)
            .filter(
                ActivityEvent.tenant_id == self.tenant.id,
                ActivityEvent.occurred_at >= start_dt,
                ActivityEvent.occurred_at <= end_dt,
            )
            .order_by(ActivityEvent.occurred_at.desc())
        )

        if user_id:
            q = q.filter(ActivityEvent.user_id == user_id)

        if cursor:
            # cursor is an ISO timestamp; fetch rows older than it
            cursor_dt = datetime.fromisoformat(cursor)
            q = q.filter(ActivityEvent.occurred_at < cursor_dt)

        total = q.count()
        rows = q.limit(page_size + 1).all()

        next_cursor = None
        if len(rows) > page_size:
            rows = rows[:page_size]
            next_cursor = rows[-1].occurred_at.isoformat()

        self._set_cached(
            cache_key,
            {
                "rows": [r.id for r in rows],   # only cache IDs; serializer re-fetches
                "next_cursor": next_cursor,
                "total": total,
            },
            CACHE_TTL_ACTIVITY,
        )

        return rows, next_cursor, total

    # ------------------------------------------------------------------
    # Project Velocity
    # ------------------------------------------------------------------

    def get_velocity_snapshots(
        self,
        project_id: int,
        start_dt: datetime,
        end_dt: datetime,
        granularity: str,
    ) -> List[dict]:
        cache_key = self._cache_key("velocity", project_id, start_dt, end_dt, granularity)
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        sprints = (
            self.db.query(Sprint)
            .filter(
                Sprint.project_id == project_id,
                Sprint.tenant_id == self.tenant.id,
                Sprint.start_date >= start_dt,
                Sprint.end_date <= end_dt,
            )
            .order_by(Sprint.start_date)
            .all()
        )

        snapshots = []
        for sprint in sprints:
            tasks = (
                self.db.query(Task)
                .filter(
                    Task.sprint_id == sprint.id,
                    Task.tenant_id == self.tenant.id,
                )
                .all()
            )
            total_points = sum(t.story_points or 0 for t in tasks)
            completed = sum(
                t.story_points or 0 for t in tasks if t.status == "done"
            )
            added_mid_sprint = sum(
                t.story_points or 0
                for t in tasks
                if t.added_at and t.added_at > sprint.start_date
            )
            cycle_times = [
                (t.completed_at - t.started_at).total_seconds() / 3600
                for t in tasks
                if t.completed_at and t.started_at and t.status == "done"
            ]
            avg_cycle_time = (
                sum(cycle_times) / len(cycle_times) if cycle_times else 0
            )
            scope_creep_pct = (
                (added_mid_sprint / total_points * 100) if total_points else 0
            )

            snapshots.append(
                {
                    "sprint_id": sprint.id,
                    "sprint_name": sprint.name,
                    "start_date": sprint.start_date.isoformat(),
                    "end_date": sprint.end_date.isoformat(),
                    "points_planned": total_points,
                    "points_completed": completed,
                    "completion_rate": round(completed / total_points * 100, 1)
                    if total_points
                    else 0,
                    "avg_cycle_time_hours": round(avg_cycle_time, 2),
                    "scope_creep_pct": round(scope_creep_pct, 2),
                }
            )

        self._set_cached(cache_key, snapshots, CACHE_TTL_VELOCITY)
        return snapshots

    # ------------------------------------------------------------------
    # Time Tracking
    # ------------------------------------------------------------------

    def get_time_tracking_summary(
        self,
        start_dt: datetime,
        end_dt: datetime,
        project_id: Optional[int],
        group_by: str,
    ) -> List[dict]:
        cache_key = self._cache_key(
            "time", start_dt, end_dt, project_id, group_by
        )
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        q = self.db.query(TimeEntry).filter(
            TimeEntry.tenant_id == self.tenant.id,
            TimeEntry.started_at >= start_dt,
            TimeEntry.started_at <= end_dt,
        )
        if project_id:
            q = q.filter(TimeEntry.project_id == project_id)

        entries = q.all()

        # Group in Python to avoid complex SQL across DB dialects
        groups: dict[Any, dict] = {}
        for entry in entries:
            if group_by == "user":
                key = entry.user_id
                label = str(entry.user_id)  # resolved to name by serializer
            elif group_by == "project":
                key = entry.project_id
                label = str(entry.project_id)
            else:  # task
                key = entry.task_id
                label = str(entry.task_id)

            duration_h = (
                (entry.ended_at - entry.started_at).total_seconds() / 3600
                if entry.ended_at
                else 0
            )
            if key not in groups:
                groups[key] = {
                    "group_id": key,
                    "label": label,
                    "total_hours": 0.0,
                    "billable_hours": 0.0,
                    "non_billable_hours": 0.0,
                    "entry_count": 0,
                }
            groups[key]["total_hours"] += duration_h
            if entry.billable:
                groups[key]["billable_hours"] += duration_h
            else:
                groups[key]["non_billable_hours"] += duration_h
            groups[key]["entry_count"] += 1

        result = [
            {**v, "total_hours": round(v["total_hours"], 2),
             "billable_hours": round(v["billable_hours"], 2),
             "non_billable_hours": round(v["non_billable_hours"], 2)}
            for v in groups.values()
        ]
        result.sort(key=lambda x: x["total_hours"], reverse=True)

        self._set_cached(cache_key, result, CACHE_TTL_TIME_TRACKING)
        return result

    # ------------------------------------------------------------------
    # Team Performance
    # ------------------------------------------------------------------

    def get_team_members(
        self,
        team_id: Optional[int],
        include_inactive: bool,
    ) -> List[User]:
        q = self.db.query(User).filter(User.tenant_id == self.tenant.id)
        if team_id:
            q = q.filter(User.team_id == team_id)
        if not include_inactive:
            q = q.filter(User.is_active == True)  # noqa: E712
        return q.order_by(User.display_name).all()

    def compute_performance_scores(
        self,
        members: List[User],
        start_dt: datetime,
        end_dt: datetime,
    ) -> List[dict]:
        cache_key = self._cache_key(
            "perf",
            "-".join(str(m.id) for m in members),
            start_dt,
            end_dt,
        )
        cached = self._get_cached(cache_key)
        if cached:
            return cached

        scores = []
        for member in members:
            tasks_done = (
                self.db.query(func.count(Task.id))
                .filter(
                    Task.assignee_id == member.id,
                    Task.tenant_id == self.tenant.id,
                    Task.status == "done",
                    Task.completed_at >= start_dt,
                    Task.completed_at <= end_dt,
                )
                .scalar()
                or 0
            )

            time_entries = (
                self.db.query(TimeEntry)
                .filter(
                    TimeEntry.user_id == member.id,
                    TimeEntry.tenant_id == self.tenant.id,
                    TimeEntry.started_at >= start_dt,
                    TimeEntry.started_at <= end_dt,
                )
                .all()
            )
            total_hours = sum(
                (e.ended_at - e.started_at).total_seconds() / 3600
                for e in time_entries
                if e.ended_at
            )

            activity_count = (
                self.db.query(func.count(ActivityEvent.id))
                .filter(
                    ActivityEvent.user_id == member.id,
                    ActivityEvent.tenant_id == self.tenant.id,
                    ActivityEvent.occurred_at >= start_dt,
                    ActivityEvent.occurred_at <= end_dt,
                )
                .scalar()
                or 0
            )

            # Normalised composite score (will be rescaled relative to team after
            # collecting all members)
            raw_score = (
                tasks_done * 10
                + activity_count * 0.5
                + min(total_hours, 160) * 0.25  # cap at full-time equivalent
            )

            scores.append(
                {
                    "tasks_completed": tasks_done,
                    "total_logged_hours": round(total_hours, 2),
                    "activity_events": activity_count,
                    "raw_score": raw_score,
                    "normalised_score": 0,  # filled in below
                }
            )

        # Normalise scores to 0–100 within the team
        max_raw = max((s["raw_score"] for s in scores), default=1) or 1
        for s in scores:
            s["normalised_score"] = round(s["raw_score"] / max_raw * 100, 1)

        self._set_cached(cache_key, scores, CACHE_TTL_PERFORMANCE)
        return scores

    # ------------------------------------------------------------------
    # Billing
    # ------------------------------------------------------------------

    def get_billing_usage(self, period_start: datetime) -> dict:
        period_end = (period_start + timedelta(days=32)).replace(day=1)

        seat_count = (
            self.db.query(func.count(User.id))
            .filter(
                User.tenant_id == self.tenant.id,
                User.is_active == True,  # noqa: E712
            )
            .scalar()
            or 0
        )

        api_calls = (
            self.db.query(func.count(ActivityEvent.id))
            .filter(
                ActivityEvent.tenant_id == self.tenant.id,
                ActivityEvent.event_type == "api_call",
                ActivityEvent.occurred_at >= period_start,
                ActivityEvent.occurred_at < period_end,
            )
            .scalar()
            or 0
        )

        storage_bytes = (
            self.db.query(func.sum(text("file_size_bytes")))
            .select_from(text("file_attachments"))
            .filter(text(f"tenant_id = {self.tenant.id}"))
            .scalar()
            or 0
        )

        plan = self.tenant.plan  # e.g. {"seats": 25, "api_calls": 50000, ...}
        plan_seats = plan.get("seats", 5) if isinstance(plan, dict) else 5
        plan_api_calls = plan.get("api_calls", 10000) if isinstance(plan, dict) else 10000

        return {
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "seats_used": seat_count,
            "seats_included": plan_seats,
            "seats_overage": max(0, seat_count - plan_seats),
            "api_calls_used": api_calls,
            "api_calls_included": plan_api_calls,
            "api_calls_overage": max(0, api_calls - plan_api_calls),
            "storage_bytes": storage_bytes,
        }

    def get_billing_record(self, tenant_id: int) -> BillingRecord:
        record = (
            self.db.query(BillingRecord)
            .filter(BillingRecord.tenant_id == tenant_id)
            .first()
        )
        if not record:
            raise ValueError(f"No billing record found for tenant {tenant_id}")
        return record
