"""Pydantic schemas for the workspace export API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ExportStatusEnum(str, Enum):
    """Mirrors the ExportStatus SQLAlchemy enum for API responses."""

    PENDING = "pending"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ExportCreateRequest(BaseModel):
    """Optional parameters when triggering an export.

    Currently all sections are always exported; this schema is here for
    forward-compatibility when per-section filtering is added.
    """

    include_comments: bool = Field(
        default=True,
        description="Include task comments in the export.",
    )
    include_attachment_metadata: bool = Field(
        default=True,
        description=(
            "Include attachment metadata (filename, size, storage key). "
            "Attachment file contents are never included."
        ),
    )
    notify_on_complete: bool = Field(
        default=False,
        description="Send an in-app notification to the requester when done.",
    )


# ---------------------------------------------------------------------------
# Response bodies
# ---------------------------------------------------------------------------


class ExportCreateResponse(BaseModel):
    """Returned immediately after a new export is enqueued."""

    export_id: UUID = Field(description="Opaque ID — use to poll status and download.")
    status: ExportStatusEnum = Field(description="Always 'queued' on creation.")
    message: str = Field(description="Human-readable summary of next steps.")

    model_config = {"from_attributes": True}


class ExportStatusResponse(BaseModel):
    """Full status snapshot returned by the polling endpoint."""

    export_id: UUID
    status: ExportStatusEnum
    progress_pct: int = Field(
        ge=0,
        le=100,
        description="Approximate completion percentage (0-100).",
    )
    progress_message: Optional[str] = Field(
        default=None,
        description="Human-readable description of the current step.",
    )
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    file_size_bytes: Optional[int] = Field(
        default=None,
        description="Size of the ZIP artifact once the export is complete.",
    )
    error_message: Optional[str] = Field(
        default=None,
        description="Present only when status='failed'.",
    )
    download_url_hint: Optional[str] = Field(
        default=None,
        description=(
            "Convenience path to the download endpoint, populated when "
            "status='complete'."
        ),
    )

    model_config = {"from_attributes": True}


class ExportListItem(BaseModel):
    """Compact representation used in list views."""

    export_id: UUID
    status: ExportStatusEnum
    requested_by: UUID
    created_at: datetime
    completed_at: Optional[datetime] = None
    file_size_bytes: Optional[int] = None

    model_config = {"from_attributes": True}


class ExportListResponse(BaseModel):
    """Paginated list of exports for a workspace."""

    items: list[ExportListItem]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Internal / Celery result schema
# ---------------------------------------------------------------------------


class ExportTaskResult(BaseModel):
    """Shape of the dict returned by the Celery task for logging purposes."""

    status: str
    export_id: str
    file_size_bytes: Optional[int] = None
    error: Optional[str] = None
