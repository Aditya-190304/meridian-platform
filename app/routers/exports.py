from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.responses import JSONResponse

from app.core.auth import get_current_user, require_project_member
from app.core.redis_client import get_redis
from app.core.rate_limit import RateLimiter
from app.schemas.export_schemas import (
    ExportFormat,
    ExportJobResponse,
    ExportListItem,
    ExportRequest,
    ExportStatus,
    ExportStatusResponse,
)
from app.services.export_service import ExportService
from app.tasks.export_tasks import export_report_task

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1",
    tags=["exports"],
)

_rate_limiter = RateLimiter(max_calls=10, period_seconds=60)


# ---------------------------------------------------------------------------
# POST /api/v1/projects/{project_id}/exports
# ---------------------------------------------------------------------------

@router.post(
    "/projects/{project_id}/exports",
    response_model=ExportJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger a new report export",
    description=(
        "Enqueues an async job to generate a PDF or CSV report for the given project. "
        "Poll the returned job_id at GET /exports/{job_id}/status for progress."
    ),
)
async def create_export(
    project_id: str = Path(..., description="UUID of the project to export"),
    body: ExportRequest = ...,
    current_user=Depends(get_current_user),
    _member=Depends(require_project_member),
) -> ExportJobResponse:
    _rate_limiter.check(user_id=current_user.id)

    job_id = str(uuid.uuid4())
    created_at = datetime.utcnow()

    # Fall back to a sensible default title if the caller didn't provide one
    report_title = body.report_title or f"Project Report — {created_at.strftime('%Y-%m-%d')}"

    # Seed the job record in Redis before enqueueing so polls don't 404
    redis = get_redis()
    redis.hset(
        f"meridian:export:job:{job_id}",
        mapping={
            "job_id": job_id,
            "project_id": project_id,
            "tenant_id": current_user.tenant_id,
            "format": body.format,
            "report_title": report_title,
            "status": ExportStatus.pending,
            "progress": "0",
            "created_at": created_at.isoformat(),
            "updated_at": created_at.isoformat(),
        },
    )
    redis.expire(f"meridian:export:job:{job_id}", 60 * 60 * 24)

    export_report_task.apply_async(
        kwargs={
            "job_id": job_id,
            "project_id": project_id,
            "tenant_id": current_user.tenant_id,
            "export_format": body.format,
            "report_title": report_title,
            "template_name": body.template_name or "default",
            "include_closed_tasks": body.include_closed_tasks,
            "date_range_start": body.date_range_start.isoformat() if body.date_range_start else None,
            "date_range_end": body.date_range_end.isoformat() if body.date_range_end else None,
            "requested_by_user_id": current_user.id,
        },
        task_id=job_id,
        queue="exports",
    )

    logger.info(
        "Export job %s enqueued for project=%s format=%s user=%s",
        job_id, project_id, body.format, current_user.id,
    )

    return ExportJobResponse(
        job_id=job_id,
        project_id=project_id,
        status=ExportStatus.pending,
        format=body.format,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/exports/{job_id}/status
# ---------------------------------------------------------------------------

@router.get(
    "/exports/{job_id}/status",
    response_model=ExportStatusResponse,
    summary="Poll export job status",
)
async def get_export_status(
    job_id: str = Path(..., description="Job ID returned by the export creation endpoint"),
    current_user=Depends(get_current_user),
) -> ExportStatusResponse:
    redis = get_redis()
    data = redis.hgetall(f"meridian:export:job:{job_id}")

    if not data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Export job '{job_id}' not found or has expired.",
        )

    # Ensure this job belongs to the requesting tenant
    if data.get("tenant_id") != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this export job.",
        )

    job_status = data.get("status", ExportStatus.pending)
    download_url: Optional[str] = None
    expires_at: Optional[datetime] = None

    if job_status == ExportStatus.completed and "s3_key" in data:
        try:
            svc = ExportService()
            download_url, expires_at = svc.get_presigned_download_url(data["s3_key"])
        except Exception:
            logger.exception("Failed to generate download URL for job %s", job_id)
            # Non-fatal — return the status without a URL; client can retry

    return ExportStatusResponse(
        job_id=job_id,
        project_id=data.get("project_id", ""),
        status=job_status,
        format=data.get("format", ExportFormat.pdf),
        progress=int(data.get("progress", 0)),
        created_at=datetime.fromisoformat(data["created_at"]),
        updated_at=datetime.fromisoformat(data["updated_at"]),
        error_message=data.get("error_message"),
        download_url=download_url,
        expires_at=expires_at,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/projects/{project_id}/exports
# ---------------------------------------------------------------------------

@router.get(
    "/projects/{project_id}/exports",
    response_model=List[ExportListItem],
    summary="List recent export jobs for a project",
)
async def list_exports(
    project_id: str = Path(..., description="UUID of the project"),
    limit: int = Query(default=20, ge=1, le=100),
    current_user=Depends(get_current_user),
    _member=Depends(require_project_member),
) -> List[ExportListItem]:
    """
    Returns the most recent export jobs for the project (Redis scan).
    Jobs older than 24 hours are automatically expired by Redis TTL.
    """
    redis = get_redis()

    # Scan for all export job keys belonging to this tenant/project.
    # In production this would be backed by a DB table for proper pagination;
    # Redis scan is acceptable for the expected volume (~handful per project/day).
    pattern = "meridian:export:job:*"
    cursor = 0
    results: List[ExportListItem] = []

    while True:
        cursor, keys = redis.scan(cursor=cursor, match=pattern, count=200)
        for key in keys:
            data = redis.hgetall(key)
            if not data:
                continue
            if data.get("project_id") != project_id:
                continue
            if data.get("tenant_id") != current_user.tenant_id:
                continue

            results.append(
                ExportListItem(
                    job_id=data.get("job_id", ""),
                    format=data.get("format", ExportFormat.pdf),
                    status=data.get("status", ExportStatus.pending),
                    report_title=data.get("report_title", ""),
                    created_at=datetime.fromisoformat(data["created_at"]),
                    file_size_bytes=int(data["file_size_bytes"]) if "file_size_bytes" in data else None,
                )
            )

        if cursor == 0:
            break

    results.sort(key=lambda r: r.created_at, reverse=True)
    return results[:limit]


# ---------------------------------------------------------------------------
# DELETE /api/v1/exports/{job_id}
# ---------------------------------------------------------------------------

@router.delete(
    "/exports/{job_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an export job record",
)
async def delete_export(
    job_id: str = Path(..., description="Job ID to delete"),
    current_user=Depends(get_current_user),
) -> None:
    redis = get_redis()
    data = redis.hgetall(f"meridian:export:job:{job_id}")

    if not data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Export job '{job_id}' not found.",
        )

    if data.get("tenant_id") != current_user.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this export job.",
        )

    redis.delete(f"meridian:export:job:{job_id}")
    logger.info("Export job %s deleted by user %s", job_id, current_user.id)
