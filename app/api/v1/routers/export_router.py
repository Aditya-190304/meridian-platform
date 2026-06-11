"""REST endpoints for workspace data export.

Flow:
  1. POST  /workspaces/{workspace_id}/export          — create & enqueue
  2. GET   /workspaces/{workspace_id}/export/{id}/status  — poll progress
  3. GET   /workspaces/{workspace_id}/export/{id}/download — fetch ZIP
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, get_db, require_workspace_admin
from app.models.export_record import ExportRecord, ExportStatus
from app.models.user import User
from app.schemas.export_schemas import (
    ExportCreateResponse,
    ExportStatusResponse,
)
from app.storage import download_export_artifact
from app.tasks.export_tasks import run_workspace_export

router = APIRouter(prefix="/workspaces/{workspace_id}/export", tags=["export"])

# How long a completed export artifact is kept in object storage (seconds).
_EXPORT_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


@router.post(
    "",
    response_model=ExportCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger a full workspace export",
)
def create_export(
    workspace_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _admin: None = Depends(require_workspace_admin),
) -> ExportCreateResponse:
    """Enqueue a background job that assembles and zips all workspace data.

    Only workspace admins may trigger an export.  Returns immediately with an
    export ID; use the status endpoint to poll for completion.
    """
    # Prevent duplicate in-flight exports for the same workspace.
    existing = (
        db.query(ExportRecord)
        .filter(
            ExportRecord.workspace_id == workspace_id,
            ExportRecord.status.in_(
                [ExportStatus.PENDING, ExportStatus.QUEUED, ExportStatus.IN_PROGRESS]
            ),
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"An export is already in progress (id={existing.id}). "
                "Wait for it to complete before starting a new one."
            ),
        )

    record = ExportRecord(
        id=uuid4(),
        workspace_id=workspace_id,
        requested_by=current_user.id,
        status=ExportStatus.QUEUED,
        created_at=datetime.now(timezone.utc),
        progress_pct=0,
        progress_message="Queued",
    )
    db.add(record)
    db.commit()
    db.refresh(record)

    run_workspace_export.delay(str(record.id))

    return ExportCreateResponse(
        export_id=record.id,
        status=record.status,
        message="Export queued. Poll the status endpoint for progress.",
    )


@router.get(
    "/{export_id}/status",
    response_model=ExportStatusResponse,
    summary="Poll export progress",
)
def get_export_status(
    workspace_id: UUID,
    export_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _admin: None = Depends(require_workspace_admin),
) -> ExportStatusResponse:
    """Return the current status and progress percentage of an export job."""
    record = _get_export_or_404(db, workspace_id, export_id)

    return ExportStatusResponse(
        export_id=record.id,
        status=record.status,
        progress_pct=record.progress_pct,
        progress_message=record.progress_message,
        created_at=record.created_at,
        started_at=record.started_at,
        completed_at=record.completed_at,
        file_size_bytes=record.file_size_bytes,
        error_message=record.error_message,
    )


@router.get(
    "/{export_id}/download",
    summary="Download the completed export ZIP",
    responses={
        200: {"content": {"application/zip": {}}},
        409: {"description": "Export not yet complete"},
    },
)
def download_export(
    workspace_id: UUID,
    export_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _admin: None = Depends(require_workspace_admin),
) -> Response:
    """Return the ZIP archive for a completed export.

    The entire file is loaded into memory and returned in a single response.
    For very large workspaces the ZIP can be several hundred MB; consider
    downloading from the pre-signed URL instead (see storage_key in status).
    """
    record = _get_export_or_404(db, workspace_id, export_id)

    if record.status != ExportStatus.COMPLETE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Export is not ready yet (status={record.status}).",
        )

    if record.storage_key is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Export record is complete but has no associated artifact.",
        )

    # Fetch the full artifact from object storage into memory, then return it.
    zip_bytes = download_export_artifact(record.storage_key)

    filename = (
        f"meridian_export_{workspace_id}_{record.created_at.strftime('%Y%m%d')}.zip"
    )
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete(
    "/{export_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an export record and its artifact",
)
def delete_export(
    workspace_id: UUID,
    export_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _admin: None = Depends(require_workspace_admin),
) -> None:
    """Remove an export record.  In-progress exports cannot be deleted."""
    record = _get_export_or_404(db, workspace_id, export_id)

    if record.status == ExportStatus.IN_PROGRESS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete an export that is currently in progress.",
        )

    db.delete(record)
    db.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_export_or_404(
    db: Session, workspace_id: UUID, export_id: UUID
) -> ExportRecord:
    record = (
        db.query(ExportRecord)
        .filter(
            ExportRecord.id == export_id,
            ExportRecord.workspace_id == workspace_id,
        )
        .one_or_none()
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Export {export_id} not found for workspace {workspace_id}.",
        )
    return record
