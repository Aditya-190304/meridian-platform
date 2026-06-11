from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, validator


class ExportFormat(str, Enum):
    pdf = "pdf"
    csv = "csv"


class ExportStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class ExportRequest(BaseModel):
    format: ExportFormat = ExportFormat.pdf
    report_title: Optional[str] = Field(
        default=None,
        description="Custom title for the exported report. Defaults to the project name.",
        max_length=128,
    )
    template_name: Optional[str] = Field(
        default="default",
        description="Name of the report template to use (must exist in templates/reports/).",
        max_length=64,
    )
    include_closed_tasks: bool = Field(
        default=False,
        description="Whether to include completed/closed tasks in the export.",
    )
    date_range_start: Optional[datetime] = None
    date_range_end: Optional[datetime] = None

    @validator("template_name")
    def validate_template_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return "default"
        # Allow alphanumeric, hyphens, underscores
        if not re.match(r"^[a-zA-Z0-9_\-]+$", v):
            raise ValueError("template_name may only contain letters, numbers, hyphens, and underscores")
        return v

    class Config:
        use_enum_values = True


class ExportJobResponse(BaseModel):
    job_id: str
    project_id: str
    status: ExportStatus
    format: ExportFormat
    created_at: datetime
    message: str = "Export job queued successfully"

    class Config:
        use_enum_values = True


class ExportStatusResponse(BaseModel):
    job_id: str
    project_id: str
    status: ExportStatus
    format: ExportFormat
    progress: int = Field(default=0, ge=0, le=100, description="Completion percentage")
    created_at: datetime
    updated_at: datetime
    error_message: Optional[str] = None
    download_url: Optional[str] = Field(
        default=None,
        description="Pre-signed S3 URL, populated when status=completed",
    )
    expires_at: Optional[datetime] = Field(
        default=None,
        description="Expiry timestamp for the download URL",
    )

    class Config:
        use_enum_values = True


class ExportListItem(BaseModel):
    job_id: str
    format: ExportFormat
    status: ExportStatus
    report_title: str
    created_at: datetime
    file_size_bytes: Optional[int] = None

    class Config:
        use_enum_values = True
