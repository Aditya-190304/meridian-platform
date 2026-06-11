from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class TaskStatus(str, Enum):
    todo = "todo"
    in_progress = "in_progress"
    in_review = "in_review"
    done = "done"
    cancelled = "cancelled"


# ---------------------------------------------------------------------------
# Individual action payloads
# ---------------------------------------------------------------------------

class BulkUpdateStatusAction(BaseModel):
    action: Literal["update_status"]
    status: TaskStatus


class BulkReassignAction(BaseModel):
    action: Literal["reassign"]
    assignee_id: UUID


class BulkAddTagsAction(BaseModel):
    action: Literal["add_tags"]
    tags: list[str] = Field(..., min_length=1, max_length=20)

    @field_validator("tags")
    @classmethod
    def tags_not_empty(cls, v: list[str]) -> list[str]:
        cleaned = [t.strip().lower() for t in v if t.strip()]
        if not cleaned:
            raise ValueError("tags list must contain at least one non-empty tag")
        return cleaned


class BulkMoveToProjectAction(BaseModel):
    action: Literal["move_to_project"]
    project_id: UUID


class BulkDeleteAction(BaseModel):
    action: Literal["delete"]
    # soft-delete by default; pass hard_delete=True for permanent removal
    hard_delete: bool = False


BulkAction = Annotated[
    Union[
        BulkUpdateStatusAction,
        BulkReassignAction,
        BulkAddTagsAction,
        BulkMoveToProjectAction,
        BulkDeleteAction,
    ],
    Field(discriminator="action"),
]


# ---------------------------------------------------------------------------
# Top-level request / response
# ---------------------------------------------------------------------------

class BulkTaskRequest(BaseModel):
    task_ids: list[UUID] = Field(..., min_length=1)
    operation: BulkAction

    @field_validator("task_ids")
    @classmethod
    def deduplicate_ids(cls, v: list[UUID]) -> list[UUID]:
        seen: set[UUID] = set()
        unique = []
        for tid in v:
            if tid not in seen:
                seen.add(tid)
                unique.append(tid)
        return unique


class BulkTaskResult(BaseModel):
    succeeded: list[UUID]
    failed: list[dict[str, Any]]
    total_requested: int
    total_succeeded: int
    total_failed: int


class AuditEntry(BaseModel):
    task_id: UUID
    actor_id: UUID
    workspace_id: UUID
    action: str
    before: dict[str, Any]
    after: dict[str, Any]
