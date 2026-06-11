"""Pydantic schemas for tag analytics and auto-suggestion responses."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Internal / storage models
# ---------------------------------------------------------------------------

class TagFrequency(BaseModel):
    """How many tasks a single tag appears on in the workspace."""

    tag_id: int
    tag_name: str
    task_count: int = Field(..., ge=0)


class CoOccurrenceEntry(BaseModel):
    """A single (tag_b, count) pair in the co-occurrence list for tag_a."""

    tag_id: int
    tag_name: str
    co_occurrence_count: int = Field(..., ge=0)


class WorkspaceTagStats(BaseModel):
    """Full analytics payload cached in Redis and returned by the analytics endpoint."""

    workspace_id: int
    frequencies: list[TagFrequency]
    # Keyed by tag_id (as string, because JSON object keys must be strings).
    co_occurrences: dict[str, list[CoOccurrenceEntry]]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TagSuggestRequest(BaseModel):
    """Query parameters for the suggestion endpoint."""

    q: str = Field(
        default="",
        description="Partial tag name typed by the user. Empty string returns top tags.",
        max_length=100,
    )
    current_tag_ids: list[int] = Field(
        default_factory=list,
        description="IDs of tags already applied to the task being edited.",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of suggestions to return.",
    )


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class TagSuggestion(BaseModel):
    """A single ranked tag suggestion."""

    tag_id: int
    tag_name: str
    score: float = Field(..., ge=0.0, description="Composite suggestion score (higher = more relevant).")
    reason: Literal["frequency", "co_occurrence"] = Field(
        ...,
        description="Primary signal driving this suggestion.",
    )


class TagSuggestResponse(BaseModel):
    workspace_id: int
    query: str
    suggestions: list[TagSuggestion]


class TagAnalyticsResponse(BaseModel):
    """Full analytics payload exposed to the frontend for the tag management dashboard."""

    workspace_id: int
    total_tags: int
    total_tagged_tasks: int
    frequencies: list[TagFrequency]
    # Only the top-5 co-occurring neighbours are included per tag to keep the
    # payload size reasonable for the dashboard view.
    top_co_occurrences: dict[str, list[CoOccurrenceEntry]]


class PrecomputeStatusResponse(BaseModel):
    """Response from the manual cache warm-up trigger."""

    workspace_id: int
    status: Literal["queued", "skipped"]
    message: str
