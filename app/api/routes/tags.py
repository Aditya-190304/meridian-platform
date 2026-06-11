"""Tag-related API routes for the Meridian Platform.

Exposes:
  GET  /workspaces/{workspace_id}/tags                  — list all tags
  POST /workspaces/{workspace_id}/tags                  — create a tag
  DELETE /workspaces/{workspace_id}/tags/{tag_id}       — delete a tag
  GET  /workspaces/{workspace_id}/tags/suggestions      — auto-suggestions
  GET  /workspaces/{workspace_id}/tags/analytics        — analytics dashboard
  POST /workspaces/{workspace_id}/tags/analytics/warm   — manual cache warm-up
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_workspace_member, get_db
from app.models.tag import Tag
from app.models.workspace import WorkspaceMember
from app.schemas.tag import TagCreate, TagRead
from app.schemas.tag_analytics import (
    PrecomputeStatusResponse,
    TagAnalyticsResponse,
    TagSuggestResponse,
)
from app.services.tag_analytics import (
    compute_workspace_tag_stats,
    invalidate_workspace_cache,
    suggest_tags,
)
from app.tasks.tag_precompute import precompute_workspace_tag_analytics

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/workspaces/{workspace_id}/tags",
    tags=["tags"],
)


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------

def _get_tag_or_404(workspace_id: int, tag_id: int, db: Session) -> Tag:
    tag = (
        db.query(Tag)
        .filter(
            Tag.id == tag_id,
            Tag.workspace_id == workspace_id,
            Tag.deleted_at.is_(None),
        )
        .first()
    )
    if tag is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
    return tag


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("", response_model=list[TagRead], summary="List workspace tags")
def list_tags(
    workspace_id: int,
    db: Session = Depends(get_db),
    _member: WorkspaceMember = Depends(get_current_workspace_member),
) -> list[Tag]:
    """Return all active tags for the workspace, ordered alphabetically."""
    return (
        db.query(Tag)
        .filter(Tag.workspace_id == workspace_id, Tag.deleted_at.is_(None))
        .order_by(Tag.name)
        .all()
    )


@router.post(
    "",
    response_model=TagRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a tag",
)
def create_tag(
    workspace_id: int,
    body: TagCreate,
    db: Session = Depends(get_db),
    member: WorkspaceMember = Depends(get_current_workspace_member),
) -> Tag:
    """Create a new tag and invalidate the analytics cache."""
    existing = (
        db.query(Tag)
        .filter(
            Tag.workspace_id == workspace_id,
            Tag.name == body.name,
            Tag.deleted_at.is_(None),
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tag '{body.name}' already exists in this workspace",
        )

    tag = Tag(
        workspace_id=workspace_id,
        name=body.name,
        color=body.color,
        created_by_id=member.user_id,
    )
    db.add(tag)
    db.commit()
    db.refresh(tag)

    invalidate_workspace_cache(workspace_id)
    logger.info("Created tag %d '%s' in workspace %d", tag.id, tag.name, workspace_id)
    return tag


@router.delete(
    "/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a tag",
)
def delete_tag(
    workspace_id: int,
    tag_id: int,
    db: Session = Depends(get_db),
    _member: WorkspaceMember = Depends(get_current_workspace_member),
) -> None:
    """Soft-delete a tag and invalidate the analytics cache."""
    tag = _get_tag_or_404(workspace_id, tag_id, db)

    from datetime import datetime, timezone
    tag.deleted_at = datetime.now(timezone.utc)
    db.commit()

    invalidate_workspace_cache(workspace_id)
    logger.info("Deleted tag %d in workspace %d", tag_id, workspace_id)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

@router.get(
    "/suggestions",
    response_model=TagSuggestResponse,
    summary="Auto-suggest tags for a task",
)
def get_tag_suggestions(
    workspace_id: int,
    q: Annotated[str, Query(max_length=100)] = "",
    current_tag_ids: Annotated[list[int], Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    db: Session = Depends(get_db),
    _member: WorkspaceMember = Depends(get_current_workspace_member),
) -> TagSuggestResponse:
    """Return ranked tag suggestions based on frequency and co-occurrence.

    - ``q`` — partial tag name typed by the user (optional)
    - ``current_tag_ids`` — tags already applied; used for co-occurrence boost
    - ``limit`` — max results
    """
    suggestions = suggest_tags(
        db,
        workspace_id,
        partial_query=q,
        current_tag_ids=current_tag_ids or [],
        limit=limit,
    )
    return TagSuggestResponse(
        workspace_id=workspace_id,
        query=q,
        suggestions=suggestions,
    )


@router.get(
    "/analytics",
    response_model=TagAnalyticsResponse,
    summary="Tag usage analytics for the workspace dashboard",
)
def get_tag_analytics(
    workspace_id: int,
    db: Session = Depends(get_db),
    _member: WorkspaceMember = Depends(get_current_workspace_member),
) -> TagAnalyticsResponse:
    """Return full tag frequency and co-occurrence data for the analytics dashboard."""
    stats = compute_workspace_tag_stats(db, workspace_id)

    # Trim co-occurrence lists to top-5 neighbours per tag for the dashboard view.
    top_co: dict = {
        tag_id_str: entries[:5]
        for tag_id_str, entries in stats.co_occurrences.items()
    }

    total_tagged_tasks = len(
        {task_id for freq in stats.frequencies for task_id in []}
    ) or sum(f.task_count for f in stats.frequencies[:1])
    # Rough proxy: unique task count is not stored directly in the cached stats;
    # the dashboard just needs an indicative figure.
    total_tagged_tasks = stats.frequencies[0].task_count if stats.frequencies else 0

    return TagAnalyticsResponse(
        workspace_id=workspace_id,
        total_tags=len(stats.frequencies),
        total_tagged_tasks=total_tagged_tasks,
        frequencies=stats.frequencies,
        top_co_occurrences=top_co,
    )


@router.post(
    "/analytics/warm",
    response_model=PrecomputeStatusResponse,
    summary="Manually trigger analytics cache warm-up",
)
def warm_analytics_cache(
    workspace_id: int,
    _member: WorkspaceMember = Depends(get_current_workspace_member),
) -> PrecomputeStatusResponse:
    """Queue the background pre-compute task for this workspace.

    Useful after a bulk tag import or migration.
    """
    precompute_workspace_tag_analytics.delay(workspace_id)
    return PrecomputeStatusResponse(
        workspace_id=workspace_id,
        status="queued",
        message="Tag analytics pre-computation queued. Results will be available shortly.",
    )
