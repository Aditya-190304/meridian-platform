"""Notification helpers for task events.

Keeps the service layer decoupled from transport concerns (WebSocket,
email, push).  Each public function is intentionally simple so callers
don't have to think about delivery details.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.core.websocket_manager import ws_manager  # existing singleton
from app.core.email import send_email_async         # existing helper

logger = logging.getLogger(__name__)


async def notify_task_updated(
    task_id: UUID,
    workspace_id: UUID,
    payload: dict[str, Any],
) -> None:
    """Broadcast a single task-updated event to all workspace subscribers.

    Called once per affected task so the frontend can apply an incremental
    patch without reloading the whole list.
    """
    event = {
        "type": "task.updated",
        "task_id": str(task_id),
        "data": payload,
    }
    try:
        await ws_manager.broadcast_to_workspace(str(workspace_id), event)
    except Exception:
        logger.exception(
            "WS broadcast failed for task %s in workspace %s",
            task_id,
            workspace_id,
        )


async def notify_task_deleted(
    task_id: UUID,
    workspace_id: UUID,
) -> None:
    """Broadcast a task-deleted tombstone event."""
    event = {
        "type": "task.deleted",
        "task_id": str(task_id),
    }
    try:
        await ws_manager.broadcast_to_workspace(str(workspace_id), event)
    except Exception:
        logger.exception(
            "WS broadcast failed (delete) for task %s in workspace %s",
            task_id,
            workspace_id,
        )


async def notify_assignee_changed(
    task_id: UUID,
    assignee_id: UUID,
    task_title: str,
    actor_name: str,
    workspace_id: UUID,
) -> None:
    """Send an email to the newly assigned user and broadcast over WS."""
    # WebSocket first so the UI updates even if email is slow
    await notify_task_updated(
        task_id=task_id,
        workspace_id=workspace_id,
        payload={"assignee_id": str(assignee_id)},
    )

    # Email — fire and forget; failures are logged but do not roll back the op
    try:
        await send_email_async(
            recipient_id=assignee_id,
            template="task_assigned",
            context={
                "task_id": str(task_id),
                "task_title": task_title,
                "assigned_by": actor_name,
            },
        )
    except Exception:
        logger.exception(
            "Email notification failed for assignee %s on task %s",
            assignee_id,
            task_id,
        )
