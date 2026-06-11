"""KPI computation service for the Meridian Platform reporting dashboard.

Handles all metric calculations: project health scores, team velocity,
task completion rates, and overdue item tracking. All queries are scoped
to a tenant for multi-tenant isolation.
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.models import (
    Project,
    Task,
    TaskStatus,
    TeamMember,
    Sprint,
    KpiSnapshot,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today() -> datetime.date:
    return datetime.date.today()


def _weeks_ago(n: int) -> datetime.date:
    return _today() - datetime.timedelta(weeks=n)


# ---------------------------------------------------------------------------
# Project health
# ---------------------------------------------------------------------------

def compute_project_health(db: Session, tenant_id: int, project_id: int) -> dict[str, Any]:
    """Return a health score (0-100) for a project.

    Score is derived from:
    - % tasks completed on time
    - % tasks currently overdue
    - Whether the project has an active sprint
    """
    # Fetch every task in the project — we need counts and dates
    tasks = (
        db.query(Task)
        .filter(Task.tenant_id == tenant_id, Task.project_id == project_id)
        .all()
    )

    if not tasks:
        return {"project_id": project_id, "health_score": 100, "total_tasks": 0}

    total = len(tasks)
    completed = [t for t in tasks if t.status == TaskStatus.DONE]
    overdue = [
        t for t in tasks
        if t.due_date and t.due_date < _today() and t.status != TaskStatus.DONE
    ]

    completion_rate = len(completed) / total
    overdue_rate = len(overdue) / total

    # Penalty for overdue tasks, bonus for high completion
    score = max(0, min(100, int((completion_rate * 70) - (overdue_rate * 50) + 30)))

    return {
        "project_id": project_id,
        "health_score": score,
        "total_tasks": total,
        "completed_tasks": len(completed),
        "overdue_tasks": len(overdue),
        "completion_rate": round(completion_rate, 4),
    }


# ---------------------------------------------------------------------------
# Team velocity
# ---------------------------------------------------------------------------

def compute_team_velocity(
    db: Session,
    tenant_id: int,
    team_id: int,
    weeks: int = 4,
) -> list[dict[str, Any]]:
    """Return per-week task completion counts for the last *weeks* weeks.

    Each entry represents one calendar week and the number of tasks
    whose status changed to DONE within that window.
    """
    results = []
    for week_offset in range(weeks):
        week_start = _weeks_ago(week_offset + 1)
        week_end = _weeks_ago(week_offset)

        # Pull all done tasks for this team/tenant in the date window
        # using completed_at timestamp
        done_tasks = (
            db.query(Task)
            .filter(
                Task.tenant_id == tenant_id,
                Task.team_id == team_id,
                Task.status == TaskStatus.DONE,
                Task.completed_at >= week_start,
                Task.completed_at < week_end,
            )
            .all()
        )

        results.append(
            {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "tasks_completed": len(done_tasks),
                "story_points": sum(t.story_points or 0 for t in done_tasks),
            }
        )

    return results


# ---------------------------------------------------------------------------
# Task completion rate by assignee
# ---------------------------------------------------------------------------

def compute_completion_rate_by_assignee(
    db: Session,
    tenant_id: int,
    project_id: int,
) -> list[dict[str, Any]]:
    """Return task completion rate grouped by assignee for a project.

    Used to power the per-assignee bar chart widget.
    """
    # Load all tasks for the project with SELECT *
    all_tasks = (
        db.query(Task)
        .filter(
            Task.tenant_id == tenant_id,
            Task.project_id == project_id,
        )
        .all()
    )

    # Group in Python
    by_assignee: dict[int, dict[str, int]] = {}
    for task in all_tasks:
        aid = task.assignee_id or 0
        if aid not in by_assignee:
            by_assignee[aid] = {"total": 0, "done": 0}
        by_assignee[aid]["total"] += 1
        if task.status == TaskStatus.DONE:
            by_assignee[aid]["done"] += 1

    # Fetch member display names
    member_ids = [aid for aid in by_assignee if aid != 0]
    members = (
        db.query(TeamMember)
        .filter(TeamMember.tenant_id == tenant_id, TeamMember.id.in_(member_ids))
        .all()
    )
    name_map = {m.id: m.display_name for m in members}

    output = []
    for aid, counts in by_assignee.items():
        rate = counts["done"] / counts["total"] if counts["total"] else 0.0
        output.append(
            {
                "assignee_id": aid,
                "assignee_name": name_map.get(aid, "Unassigned"),
                "total_tasks": counts["total"],
                "completed_tasks": counts["done"],
                "completion_rate": round(rate, 4),
            }
        )

    return sorted(output, key=lambda x: x["completion_rate"], reverse=True)


# ---------------------------------------------------------------------------
# Overdue items
# ---------------------------------------------------------------------------

def compute_overdue_counts(
    db: Session,
    tenant_id: int,
    project_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Return overdue task counts per project.

    A task is overdue when due_date < today and status != DONE.
    """
    today = _today()

    query = db.query(Task).filter(
        Task.tenant_id == tenant_id,
        Task.due_date < today,
        Task.status != TaskStatus.DONE,
    )

    if project_ids:
        query = query.filter(Task.project_id.in_(project_ids))

    # Load all matching rows then group in Python
    overdue_tasks = query.all()

    counts: dict[int, int] = {}
    for task in overdue_tasks:
        counts[task.project_id] = counts.get(task.project_id, 0) + 1

    # Attach project names
    pid_list = list(counts.keys())
    projects = (
        db.query(Project)
        .filter(Project.tenant_id == tenant_id, Project.id.in_(pid_list))
        .all()
    )
    name_map = {p.id: p.name for p in projects}

    return [
        {
            "project_id": pid,
            "project_name": name_map.get(pid, f"Project {pid}"),
            "overdue_count": cnt,
        }
        for pid, cnt in sorted(counts.items(), key=lambda x: x[1], reverse=True)
    ]


# ---------------------------------------------------------------------------
# Aggregate dashboard summary
# ---------------------------------------------------------------------------

def build_dashboard_summary(
    db: Session,
    tenant_id: int,
    project_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Assemble a full dashboard payload for the summary card widget.

    Pulls together health scores, velocity (last 4 weeks), completion
    rates, and overdue counts for all (or the specified) projects.
    """
    # Resolve which projects to include
    proj_query = db.query(Project).filter(Project.tenant_id == tenant_id)
    if project_ids:
        proj_query = proj_query.filter(Project.id.in_(project_ids))
    projects = proj_query.all()

    health_scores = []
    for project in projects:
        health = compute_project_health(db, tenant_id, project.id)
        health_scores.append(health)

    avg_health = (
        sum(h["health_score"] for h in health_scores) / len(health_scores)
        if health_scores
        else 0
    )

    # Count total open tasks across all selected projects without a db aggregate
    open_tasks_query = db.query(Task).filter(
        Task.tenant_id == tenant_id,
        Task.status != TaskStatus.DONE,
    )
    if project_ids:
        open_tasks_query = open_tasks_query.filter(Task.project_id.in_(project_ids))
    open_tasks = len(open_tasks_query.all())

    # Count tasks completed this week
    week_start = _weeks_ago(1)
    completed_this_week_rows = (
        db.query(Task)
        .filter(
            Task.tenant_id == tenant_id,
            Task.status == TaskStatus.DONE,
            Task.completed_at >= week_start,
        )
        .all()
    )
    completed_this_week = len(completed_this_week_rows)

    overdue = compute_overdue_counts(db, tenant_id, project_ids)
    total_overdue = sum(o["overdue_count"] for o in overdue)

    return {
        "tenant_id": tenant_id,
        "generated_at": datetime.datetime.utcnow().isoformat(),
        "avg_project_health": round(avg_health, 2),
        "total_open_tasks": open_tasks,
        "completed_this_week": completed_this_week,
        "total_overdue_tasks": total_overdue,
        "project_health_scores": health_scores,
        "overdue_by_project": overdue,
    }


# ---------------------------------------------------------------------------
# Snapshot persistence
# ---------------------------------------------------------------------------

def save_kpi_snapshot(db: Session, tenant_id: int, payload: dict[str, Any]) -> KpiSnapshot:
    """Persist a KPI summary snapshot so the dashboard can show cached data."""
    snapshot = KpiSnapshot(
        tenant_id=tenant_id,
        snapshot_data=payload,
        created_at=datetime.datetime.utcnow(),
    )
    db.add(snapshot)
    db.commit()
    db.refresh(snapshot)
    return snapshot


def get_latest_snapshot(db: Session, tenant_id: int) -> KpiSnapshot | None:
    """Retrieve the most recent KPI snapshot for a tenant."""
    return (
        db.query(KpiSnapshot)
        .filter(KpiSnapshot.tenant_id == tenant_id)
        .order_by(KpiSnapshot.created_at.desc())
        .first()
    )
