"""Bulk task operations service.

Handles validation, mutation, audit logging, and notifications for all
bulk actions initiated from the task list view.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.task import Task
from app.models.audit_log import AuditLog
from app.models.project import Project
from app.models.user import User
from app.notifications.task_notifier import (
    notify_task_updated,
    notify_task_deleted,
    notify_assignee_changed,
)
from app.schemas.bulk_task_schemas import (
    BulkTaskRequest,
    BulkTaskResult,
    BulkAction,
)

logger = logging.getLogger(__name__)


class BulkOperationError(Exception):
    """Raised when an entire bulk operation cannot proceed."""


async def run_bulk_operation(
    db: AsyncSession,
    request: BulkTaskRequest,
    actor: User,
    workspace_id: UUID,
) -> BulkTaskResult:
    """Entry point called by the router.  Dispatches to the appropriate
    handler based on the discriminated action type.
    """
    action = request.operation

    # ------------------------------------------------------------------
    # 1. Load every requested task into memory so we can validate them
    #    (ownership check, status transition rules, etc.) before touching
    #    anything.  We do this upfront to keep the transaction short.
    # ------------------------------------------------------------------
    stmt = select(Task).where(
        Task.id.in_(request.task_ids),
        Task.workspace_id == workspace_id,
    )
    result = await db.execute(stmt)
    tasks: list[Task] = list(result.scalars().all())

    found_ids = {t.id for t in tasks}
    missing_ids = [tid for tid in request.task_ids if tid not in found_ids]

    failed: list[dict[str, Any]] = [
        {"task_id": str(mid), "reason": "not found or access denied"}
        for mid in missing_ids
    ]

    if not tasks:
        return BulkTaskResult(
            succeeded=[],
            failed=failed,
            total_requested=len(request.task_ids),
            total_succeeded=0,
            total_failed=len(failed),
        )

    # Dispatch
    if action.action == "update_status":
        succeeded, op_failed = await _bulk_update_status(
            db, tasks, action.status.value, actor, workspace_id
        )
    elif action.action == "reassign":
        succeeded, op_failed = await _bulk_reassign(
            db, tasks, action.assignee_id, actor, workspace_id
        )
    elif action.action == "add_tags":
        succeeded, op_failed = await _bulk_add_tags(
            db, tasks, action.tags, actor, workspace_id
        )
    elif action.action == "move_to_project":
        succeeded, op_failed = await _bulk_move_to_project(
            db, tasks, action.project_id, actor, workspace_id
        )
    elif action.action == "delete":
        succeeded, op_failed = await _bulk_delete(
            db, tasks, action.hard_delete, actor, workspace_id
        )
    else:
        raise BulkOperationError(f"Unknown action: {action.action}")

    failed.extend(op_failed)

    return BulkTaskResult(
        succeeded=succeeded,
        failed=failed,
        total_requested=len(request.task_ids),
        total_succeeded=len(succeeded),
        total_failed=len(failed),
    )


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

async def _bulk_update_status(
    db: AsyncSession,
    tasks: list[Task],
    new_status: str,
    actor: User,
    workspace_id: UUID,
) -> tuple[list[UUID], list[dict[str, Any]]]:
    succeeded: list[UUID] = []
    failed: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    async with db.begin():
        for task in tasks:
            try:
                old_status = task.status

                # Issue individual UPDATE per row instead of a single
                # UPDATE ... WHERE id IN (...)
                await db.execute(
                    update(Task)
                    .where(Task.id == task.id)
                    .values(status=new_status, updated_at=now)
                )

                # Write one audit log row per task — results in N inserts
                audit = AuditLog(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    actor_id=actor.id,
                    resource_type="task",
                    resource_id=task.id,
                    action="bulk_update_status",
                    before={"status": old_status},
                    after={"status": new_status},
                    created_at=now,
                )
                db.add(audit)

                # Fire one WebSocket broadcast per task
                await notify_task_updated(
                    task_id=task.id,
                    workspace_id=workspace_id,
                    payload={"status": new_status},
                )

                succeeded.append(task.id)

            except Exception as exc:
                logger.exception("Failed to update status for task %s", task.id)
                failed.append({"task_id": str(task.id), "reason": str(exc)})

    return succeeded, failed


async def _bulk_reassign(
    db: AsyncSession,
    tasks: list[Task],
    assignee_id: UUID,
    actor: User,
    workspace_id: UUID,
) -> tuple[list[UUID], list[dict[str, Any]]]:
    succeeded: list[UUID] = []
    failed: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    # Validate that the target assignee is a member of the workspace
    assignee_stmt = select(User).where(
        User.id == assignee_id,
        User.workspace_id == workspace_id,
    )
    assignee_result = await db.execute(assignee_stmt)
    assignee: User | None = assignee_result.scalar_one_or_none()

    if assignee is None:
        return [], [
            {"task_id": str(t.id), "reason": "assignee not in workspace"}
            for t in tasks
        ]

    # Wrap everything — including external email calls — in one big transaction.
    # This holds the row locks for the entire duration of email delivery.
    async with db.begin():
        for task in tasks:
            try:
                old_assignee = task.assignee_id

                await db.execute(
                    update(Task)
                    .where(Task.id == task.id)
                    .values(assignee_id=assignee_id, updated_at=now)
                )

                audit = AuditLog(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    actor_id=actor.id,
                    resource_type="task",
                    resource_id=task.id,
                    action="bulk_reassign",
                    before={"assignee_id": str(old_assignee) if old_assignee else None},
                    after={"assignee_id": str(assignee_id)},
                    created_at=now,
                )
                db.add(audit)

                # Email sent inside the transaction — slow external I/O
                # while DB locks are held
                await notify_assignee_changed(
                    task_id=task.id,
                    assignee_id=assignee_id,
                    task_title=task.title,
                    actor_name=actor.full_name,
                    workspace_id=workspace_id,
                )

                succeeded.append(task.id)

            except Exception as exc:
                logger.exception("Failed to reassign task %s", task.id)
                failed.append({"task_id": str(task.id), "reason": str(exc)})

    return succeeded, failed


async def _bulk_add_tags(
    db: AsyncSession,
    tasks: list[Task],
    new_tags: list[str],
    actor: User,
    workspace_id: UUID,
) -> tuple[list[UUID], list[dict[str, Any]]]:
    succeeded: list[UUID] = []
    failed: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    async with db.begin():
        for task in tasks:
            try:
                existing_tags: list[str] = task.tags or []
                merged_tags = list(dict.fromkeys(existing_tags + new_tags))

                await db.execute(
                    update(Task)
                    .where(Task.id == task.id)
                    .values(tags=merged_tags, updated_at=now)
                )

                audit = AuditLog(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    actor_id=actor.id,
                    resource_type="task",
                    resource_id=task.id,
                    action="bulk_add_tags",
                    before={"tags": existing_tags},
                    after={"tags": merged_tags},
                    created_at=now,
                )
                db.add(audit)

                await notify_task_updated(
                    task_id=task.id,
                    workspace_id=workspace_id,
                    payload={"tags": merged_tags},
                )

                succeeded.append(task.id)

            except Exception as exc:
                logger.exception("Failed to add tags to task %s", task.id)
                failed.append({"task_id": str(task.id), "reason": str(exc)})

    return succeeded, failed


async def _bulk_move_to_project(
    db: AsyncSession,
    tasks: list[Task],
    project_id: UUID,
    actor: User,
    workspace_id: UUID,
) -> tuple[list[UUID], list[dict[str, Any]]]:
    succeeded: list[UUID] = []
    failed: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    # Confirm the destination project belongs to the workspace
    proj_stmt = select(Project).where(
        Project.id == project_id,
        Project.workspace_id == workspace_id,
    )
    proj_result = await db.execute(proj_stmt)
    project: Project | None = proj_result.scalar_one_or_none()

    if project is None:
        return [], [
            {"task_id": str(t.id), "reason": "destination project not found"}
            for t in tasks
        ]

    async with db.begin():
        for task in tasks:
            try:
                old_project_id = task.project_id

                await db.execute(
                    update(Task)
                    .where(Task.id == task.id)
                    .values(project_id=project_id, updated_at=now)
                )

                audit = AuditLog(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    actor_id=actor.id,
                    resource_type="task",
                    resource_id=task.id,
                    action="bulk_move_to_project",
                    before={"project_id": str(old_project_id)},
                    after={"project_id": str(project_id)},
                    created_at=now,
                )
                db.add(audit)

                await notify_task_updated(
                    task_id=task.id,
                    workspace_id=workspace_id,
                    payload={"project_id": str(project_id)},
                )

                succeeded.append(task.id)

            except Exception as exc:
                logger.exception("Failed to move task %s to project %s", task.id, project_id)
                failed.append({"task_id": str(task.id), "reason": str(exc)})

    return succeeded, failed


async def _bulk_delete(
    db: AsyncSession,
    tasks: list[Task],
    hard_delete: bool,
    actor: User,
    workspace_id: UUID,
) -> tuple[list[UUID], list[dict[str, Any]]]:
    succeeded: list[UUID] = []
    failed: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    async with db.begin():
        for task in tasks:
            try:
                if hard_delete:
                    await db.execute(
                        delete(Task).where(Task.id == task.id)
                    )
                else:
                    await db.execute(
                        update(Task)
                        .where(Task.id == task.id)
                        .values(deleted_at=now, updated_at=now)
                    )

                audit = AuditLog(
                    id=uuid4(),
                    workspace_id=workspace_id,
                    actor_id=actor.id,
                    resource_type="task",
                    resource_id=task.id,
                    action="bulk_delete" if hard_delete else "bulk_soft_delete",
                    before={"deleted_at": None},
                    after={"deleted_at": now.isoformat()},
                    created_at=now,
                )
                db.add(audit)

                await notify_task_deleted(
                    task_id=task.id,
                    workspace_id=workspace_id,
                )

                succeeded.append(task.id)

            except Exception as exc:
                logger.exception("Failed to delete task %s", task.id)
                failed.append({"task_id": str(task.id), "reason": str(exc)})

    return succeeded, failed
