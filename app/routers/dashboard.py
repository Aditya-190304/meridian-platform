"""FastAPI router for the Reporting & KPI Dashboard.

Exposes endpoints consumed by the front-end chart widgets:
  - GET /summary            — full dashboard payload
  - GET /metrics/health     — per-project health scores
  - GET /metrics/velocity   — team velocity (rolling weeks)
  - GET /metrics/completion — completion rate by assignee
  - GET /metrics/overdue    — overdue item counts
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.metrics import (
    DashboardSummaryResponse,
    HealthScoreResponse,
    VelocityResponse,
    CompletionRateResponse,
    OverdueCountResponse,
    SnapshotResponse,
)
from app.services.kpi_service import (
    build_dashboard_summary,
    compute_project_health,
    compute_team_velocity,
    compute_completion_rate_by_assignee,
    compute_overdue_counts,
    save_kpi_snapshot,
    get_latest_snapshot,
)

router = APIRouter(
    prefix="/api/v1/dashboard",
    tags=["dashboard"],
)


# ---------------------------------------------------------------------------
# Dependency: resolve & validate tenant
# ---------------------------------------------------------------------------

def get_tenant_id(
    tenant_id: Annotated[int, Query(description="Tenant identifier", ge=1)],
) -> int:
    return tenant_id


TenantDep = Annotated[int, Depends(get_tenant_id)]
DbDep = Annotated[Session, Depends(get_db)]


# ---------------------------------------------------------------------------
# Summary endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/summary",
    response_model=DashboardSummaryResponse,
    summary="Full KPI dashboard payload",
    description=(
        "Returns the complete dashboard summary: avg project health, open task "
        "count, velocity, and overdue items. Expensive — consider the cached "
        "/snapshot endpoint for high-traffic UIs."
    ),
)
def get_dashboard_summary(
    tenant_id: TenantDep,
    db: DbDep,
    project_ids: Annotated[
        list[int] | None,
        Query(description="Limit results to these project IDs"),
    ] = None,
    save_snapshot: Annotated[
        bool,
        Query(description="Persist this result as a KPI snapshot"),
    ] = False,
) -> DashboardSummaryResponse:
    summary = build_dashboard_summary(db, tenant_id, project_ids)
    if save_snapshot:
        save_kpi_snapshot(db, tenant_id, summary)
    return DashboardSummaryResponse(**summary)


# ---------------------------------------------------------------------------
# Health score
# ---------------------------------------------------------------------------

@router.get(
    "/metrics/health",
    response_model=HealthScoreResponse,
    summary="Project health score",
)
def get_project_health(
    tenant_id: TenantDep,
    db: DbDep,
    project_id: Annotated[int, Query(description="Project to score", ge=1)],
) -> HealthScoreResponse:
    result = compute_project_health(db, tenant_id, project_id)
    return HealthScoreResponse(**result)


# ---------------------------------------------------------------------------
# Team velocity
# ---------------------------------------------------------------------------

@router.get(
    "/metrics/velocity",
    response_model=VelocityResponse,
    summary="Team velocity over rolling weeks",
)
def get_team_velocity(
    tenant_id: TenantDep,
    db: DbDep,
    team_id: Annotated[int, Query(description="Team to analyse", ge=1)],
    weeks: Annotated[
        int,
        Query(description="Number of weeks to look back", ge=1, le=52),
    ] = 4,
) -> VelocityResponse:
    weekly_data = compute_team_velocity(db, tenant_id, team_id, weeks)
    return VelocityResponse(
        team_id=team_id,
        weeks=weeks,
        weekly_breakdown=weekly_data,
    )


# ---------------------------------------------------------------------------
# Completion rate by assignee
# ---------------------------------------------------------------------------

@router.get(
    "/metrics/completion",
    response_model=CompletionRateResponse,
    summary="Task completion rate per assignee",
)
def get_completion_rate(
    tenant_id: TenantDep,
    db: DbDep,
    project_id: Annotated[int, Query(description="Project to analyse", ge=1)],
) -> CompletionRateResponse:
    breakdown = compute_completion_rate_by_assignee(db, tenant_id, project_id)
    return CompletionRateResponse(
        project_id=project_id,
        assignee_breakdown=breakdown,
    )


# ---------------------------------------------------------------------------
# Overdue counts
# ---------------------------------------------------------------------------

@router.get(
    "/metrics/overdue",
    response_model=OverdueCountResponse,
    summary="Overdue task counts per project",
)
def get_overdue_counts(
    tenant_id: TenantDep,
    db: DbDep,
    project_ids: Annotated[
        list[int] | None,
        Query(description="Limit to these project IDs"),
    ] = None,
) -> OverdueCountResponse:
    overdue = compute_overdue_counts(db, tenant_id, project_ids)
    total = sum(o["overdue_count"] for o in overdue)
    return OverdueCountResponse(
        total_overdue=total,
        by_project=overdue,
    )


# ---------------------------------------------------------------------------
# Snapshot endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/snapshot",
    response_model=SnapshotResponse,
    summary="Most recent cached KPI snapshot",
    description="Returns the last snapshot saved via ?save_snapshot=true on /summary.",
)
def get_snapshot(
    tenant_id: TenantDep,
    db: DbDep,
) -> SnapshotResponse:
    snapshot = get_latest_snapshot(db, tenant_id)
    if not snapshot:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No KPI snapshot found for this tenant. Call /summary?save_snapshot=true first.",
        )
    return SnapshotResponse(
        snapshot_id=snapshot.id,
        tenant_id=snapshot.tenant_id,
        created_at=snapshot.created_at.isoformat(),
        data=snapshot.snapshot_data,
    )


@router.post(
    "/snapshot",
    response_model=SnapshotResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Trigger a fresh KPI snapshot",
)
def create_snapshot(
    tenant_id: TenantDep,
    db: DbDep,
    project_ids: Annotated[
        list[int] | None,
        Query(description="Limit snapshot to these project IDs"),
    ] = None,
) -> SnapshotResponse:
    summary = build_dashboard_summary(db, tenant_id, project_ids)
    snapshot = save_kpi_snapshot(db, tenant_id, summary)
    return SnapshotResponse(
        snapshot_id=snapshot.id,
        tenant_id=snapshot.tenant_id,
        created_at=snapshot.created_at.isoformat(),
        data=snapshot.snapshot_data,
    )
