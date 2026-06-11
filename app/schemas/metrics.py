"""Pydantic schemas for the Reporting & KPI Dashboard endpoints.

Defines request/response models for health scores, velocity, completion
rates, overdue counts, and aggregate dashboard summaries.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Shared / primitive models
# ---------------------------------------------------------------------------

class ProjectHealthDetail(BaseModel):
    """Health breakdown for a single project."""

    project_id: int
    health_score: int = Field(..., ge=0, le=100, description="Score from 0 (critical) to 100 (healthy)")
    total_tasks: int = Field(..., ge=0)
    completed_tasks: int = Field(default=0, ge=0)
    overdue_tasks: int = Field(default=0, ge=0)
    completion_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class WeeklyVelocityEntry(BaseModel):
    """Task completion data for a single calendar week."""

    week_start: str = Field(..., description="ISO date of Monday")
    week_end: str = Field(..., description="ISO date of the following Monday")
    tasks_completed: int = Field(..., ge=0)
    story_points: int = Field(..., ge=0)


class AssigneeCompletionEntry(BaseModel):
    """Per-assignee completion rate for a project."""

    assignee_id: int
    assignee_name: str
    total_tasks: int = Field(..., ge=0)
    completed_tasks: int = Field(..., ge=0)
    completion_rate: float = Field(..., ge=0.0, le=1.0)


class ProjectOverdueEntry(BaseModel):
    """Overdue task count for a single project."""

    project_id: int
    project_name: str
    overdue_count: int = Field(..., ge=0)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class HealthScoreResponse(BaseModel):
    """Response for GET /metrics/health."""

    project_id: int
    health_score: int = Field(..., ge=0, le=100)
    total_tasks: int
    completed_tasks: int = 0
    overdue_tasks: int = 0
    completion_rate: float = 0.0

    model_config = {"from_attributes": True}


class VelocityResponse(BaseModel):
    """Response for GET /metrics/velocity."""

    team_id: int
    weeks: int = Field(..., ge=1, le=52)
    weekly_breakdown: list[WeeklyVelocityEntry]

    @field_validator("weekly_breakdown")
    @classmethod
    def must_not_be_empty(cls, v: list) -> list:
        # Allow empty — team may have no completed tasks in the window
        return v

    model_config = {"from_attributes": True}


class CompletionRateResponse(BaseModel):
    """Response for GET /metrics/completion."""

    project_id: int
    assignee_breakdown: list[AssigneeCompletionEntry]

    model_config = {"from_attributes": True}


class OverdueCountResponse(BaseModel):
    """Response for GET /metrics/overdue."""

    total_overdue: int = Field(..., ge=0)
    by_project: list[ProjectOverdueEntry]

    model_config = {"from_attributes": True}


class DashboardSummaryResponse(BaseModel):
    """Full dashboard payload returned by GET /summary."""

    tenant_id: int
    generated_at: str = Field(..., description="UTC ISO timestamp of when metrics were computed")
    avg_project_health: float = Field(..., ge=0.0, le=100.0)
    total_open_tasks: int = Field(..., ge=0)
    completed_this_week: int = Field(..., ge=0)
    total_overdue_tasks: int = Field(..., ge=0)
    project_health_scores: list[ProjectHealthDetail]
    overdue_by_project: list[ProjectOverdueEntry]

    model_config = {"from_attributes": True}


class SnapshotResponse(BaseModel):
    """Response for snapshot read/create endpoints."""

    snapshot_id: int
    tenant_id: int
    created_at: str
    data: dict[str, Any]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Request filter models
# ---------------------------------------------------------------------------

class DashboardFilterRequest(BaseModel):
    """Optional filters for narrowing dashboard scope."""

    project_ids: list[int] | None = Field(
        default=None,
        description="Restrict metrics to these project IDs",
    )
    team_ids: list[int] | None = Field(
        default=None,
        description="Restrict velocity metrics to these team IDs",
    )
    date_from: str | None = Field(
        default=None,
        description="ISO date — ignore tasks created before this date",
    )
    date_to: str | None = Field(
        default=None,
        description="ISO date — ignore tasks created after this date",
    )

    @field_validator("project_ids", "team_ids")
    @classmethod
    def non_empty_list(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and len(v) == 0:
            raise ValueError("If provided, the list must contain at least one ID.")
        return v
