from __future__ import annotations

import base64
from datetime import date
from enum import Enum
from typing import Any, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field, validator
from pydantic.generics import GenericModel


class SortDirection(str, Enum):
    asc = "asc"
    desc = "desc"


class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    in_review = "in_review"
    done = "done"
    cancelled = "cancelled"


class ProjectStatus(str, Enum):
    active = "active"
    on_hold = "on_hold"
    completed = "completed"
    archived = "archived"


ALLOWED_TASK_SORT_COLUMNS = {
    "created_at",
    "updated_at",
    "due_date",
    "name",
    "status",
    "priority",
}

ALLOWED_PROJECT_SORT_COLUMNS = {
    "created_at",
    "updated_at",
    "name",
    "status",
    "start_date",
    "end_date",
}


class TaskSearchParams(BaseModel):
    q: Optional[str] = Field(None, description="Free-text search against task name and description")
    assignee_ids: Optional[str] = Field(
        None,
        description="Comma-separated list of assignee UUIDs",
    )
    status: Optional[TaskStatus] = None
    priority: Optional[int] = Field(None, ge=1, le=5)
    project_id: Optional[str] = None
    created_after: Optional[date] = None
    created_before: Optional[date] = None
    due_after: Optional[date] = None
    due_before: Optional[date] = None
    tags: Optional[str] = Field(
        None,
        description="Comma-separated tag names to filter by (tasks must have ALL listed tags)",
    )
    sort_by: str = Field("created_at", description="Column to sort by")
    sort_dir: SortDirection = SortDirection.desc
    page_size: int = Field(20, ge=1, le=100)
    page_token: Optional[str] = None

    @validator("sort_by")
    def validate_sort_column(cls, v: str) -> str:  # noqa: N805
        if v not in ALLOWED_TASK_SORT_COLUMNS:
            raise ValueError(
                f"sort_by must be one of: {', '.join(sorted(ALLOWED_TASK_SORT_COLUMNS))}"
            )
        return v

    @validator("created_before")
    def created_before_after_created_after(  # noqa: N805
        cls, v: Optional[date], values: dict
    ) -> Optional[date]:
        if v and values.get("created_after") and v < values["created_after"]:
            raise ValueError("created_before must be after created_after")
        return v


class ProjectSearchParams(BaseModel):
    q: Optional[str] = Field(None, description="Free-text search against project name")
    status: Optional[ProjectStatus] = None
    owner_id: Optional[str] = None
    start_after: Optional[date] = None
    end_before: Optional[date] = None
    sort_by: str = Field("created_at", description="Column to sort by")
    sort_dir: SortDirection = SortDirection.desc
    page_size: int = Field(20, ge=1, le=100)
    page_token: Optional[str] = None

    @validator("sort_by")
    def validate_sort_column(cls, v: str) -> str:  # noqa: N805
        if v not in ALLOWED_PROJECT_SORT_COLUMNS:
            raise ValueError(
                f"sort_by must be one of: {', '.join(sorted(ALLOWED_PROJECT_SORT_COLUMNS))}"
            )
        return v


T = TypeVar("T")


class PaginatedResponse(GenericModel, Generic[T]):
    items: List[T]
    total_count: int
    page_size: int
    next_page_token: Optional[str] = None
    has_more: bool


class TaskResult(BaseModel):
    id: str
    name: str
    description: Optional[str]
    status: str
    priority: int
    assignee_id: Optional[str]
    project_id: str
    tags: List[str]
    created_at: str
    updated_at: str
    due_date: Optional[str]


class ProjectResult(BaseModel):
    id: str
    name: str
    description: Optional[str]
    status: str
    owner_id: str
    created_at: str
    updated_at: str
    start_date: Optional[str]
    end_date: Optional[str]
    task_count: int
