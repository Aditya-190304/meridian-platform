from __future__ import annotations

import logging
from typing import Optional

from databases import Database
from fastapi import APIRouter, Depends, HTTPException, Query, status

from meridian.api.dependencies import get_current_tenant_id, get_db
from meridian.schemas.search import (
    PaginatedResponse,
    ProjectResult,
    ProjectSearchParams,
    SortDirection,
    TaskResult,
    TaskSearchParams,
    TaskStatus,
    ProjectStatus,
)
from meridian.services.search_service import SearchService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])


def _get_search_service(db: Database = Depends(get_db)) -> SearchService:
    return SearchService(db)


@router.get(
    "/tasks",
    response_model=PaginatedResponse[TaskResult],
    summary="Advanced task search",
    description=(
        "Search tasks within the current tenant workspace using multiple optional filters. "
        "Supports sorting and cursor-based pagination."
    ),
)
async def search_tasks(
    q: Optional[str] = Query(None, description="Free-text search on task name/description"),
    assignee_ids: Optional[str] = Query(
        None, description="Comma-separated assignee UUIDs"
    ),
    status: Optional[TaskStatus] = Query(None),
    priority: Optional[int] = Query(None, ge=1, le=5),
    project_id: Optional[str] = Query(None),
    created_after: Optional[str] = Query(None, description="ISO date, e.g. 2024-01-01"),
    created_before: Optional[str] = Query(None, description="ISO date"),
    due_after: Optional[str] = Query(None, description="ISO date"),
    due_before: Optional[str] = Query(None, description="ISO date"),
    tags: Optional[str] = Query(
        None, description="Comma-separated tag names; task must have ALL tags"
    ),
    sort_by: str = Query("created_at"),
    sort_dir: SortDirection = Query(SortDirection.desc),
    page_size: int = Query(20, ge=1, le=100),
    page_token: Optional[str] = Query(None),
    tenant_id: str = Depends(get_current_tenant_id),
    svc: SearchService = Depends(_get_search_service),
) -> PaginatedResponse[TaskResult]:
    from datetime import date as _date

    def _parse_date(val: Optional[str], field: str) -> Optional[_date]:
        if val is None:
            return None
        try:
            return _date.fromisoformat(val)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field} must be a valid ISO date (YYYY-MM-DD)",
            )

    try:
        params = TaskSearchParams(
            q=q,
            assignee_ids=assignee_ids,
            status=status,
            priority=priority,
            project_id=project_id,
            created_after=_parse_date(created_after, "created_after"),
            created_before=_parse_date(created_before, "created_before"),
            due_after=_parse_date(due_after, "due_after"),
            due_before=_parse_date(due_before, "due_before"),
            tags=tags,
            sort_by=sort_by,
            sort_dir=sort_dir,
            page_size=page_size,
            page_token=page_token,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    try:
        items, total_count, next_token = await svc.search_tasks(tenant_id, params)
    except Exception:
        logger.exception("Unexpected error during task search")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing your search.",
        )

    return PaginatedResponse(
        items=items,
        total_count=total_count,
        page_size=page_size,
        next_page_token=next_token,
        has_more=next_token is not None,
    )


@router.get(
    "/projects",
    response_model=PaginatedResponse[ProjectResult],
    summary="Advanced project search",
    description="Search projects within the current tenant workspace.",
)
async def search_projects(
    q: Optional[str] = Query(None),
    status: Optional[ProjectStatus] = Query(None),
    owner_id: Optional[str] = Query(None),
    start_after: Optional[str] = Query(None, description="ISO date"),
    end_before: Optional[str] = Query(None, description="ISO date"),
    sort_by: str = Query("created_at"),
    sort_dir: SortDirection = Query(SortDirection.desc),
    page_size: int = Query(20, ge=1, le=100),
    page_token: Optional[str] = Query(None),
    tenant_id: str = Depends(get_current_tenant_id),
    svc: SearchService = Depends(_get_search_service),
) -> PaginatedResponse[ProjectResult]:
    from datetime import date as _date

    def _parse_date(val: Optional[str], field: str) -> Optional[_date]:
        if val is None:
            return None
        try:
            return _date.fromisoformat(val)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"{field} must be a valid ISO date (YYYY-MM-DD)",
            )

    try:
        params = ProjectSearchParams(
            q=q,
            status=status,
            owner_id=owner_id,
            start_after=_parse_date(start_after, "start_after"),
            end_before=_parse_date(end_before, "end_before"),
            sort_by=sort_by,
            sort_dir=sort_dir,
            page_size=page_size,
            page_token=page_token,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    try:
        items, total_count, next_token = await svc.search_projects(tenant_id, params)
    except Exception:
        logger.exception("Unexpected error during project search")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing your search.",
        )

    return PaginatedResponse(
        items=items,
        total_count=total_count,
        page_size=page_size,
        next_page_token=next_token,
        has_more=next_token is not None,
    )
