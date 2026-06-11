"""Router for bulk task operations.

Exposes a single POST endpoint that accepts a list of task IDs and a
discriminated-union action payload.  Authentication and workspace
resolution are handled by shared dependencies.
"""
from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.auth import get_current_user, require_workspace_member
from app.models.user import User
from app.schemas.bulk_task_schemas import BulkTaskRequest, BulkTaskResult
from app.services.bulk_operations import (
    run_bulk_operation,
    BulkOperationError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks", "bulk"])


@router.post(
    "/bulk",
    response_model=BulkTaskResult,
    status_code=status.HTTP_200_OK,
    summary="Execute a bulk operation on a set of tasks",
    description=(
        "Applies the requested operation (status update, reassign, add tags, "
        "move to project, or delete) to every task ID supplied.  Tasks that "
        "do not belong to the caller's workspace are silently skipped and "
        "reported in the `failed` list."
    ),
)
async def bulk_task_operation(
    payload: BulkTaskRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    workspace_id: UUID = Depends(require_workspace_member),
) -> BulkTaskResult:
    """Run a bulk operation against the supplied task IDs.

    The endpoint is intentionally lenient: it processes as many tasks as
    possible and returns a summary of successes and failures rather than
    aborting on the first error.
    """
    # No cap on task_ids length — a user can submit all 50k tasks in one go.
    # The request schema enforces min_length=1 but has no upper bound.

    logger.info(
        "Bulk op requested by user=%s workspace=%s action=%s task_count=%d",
        current_user.id,
        workspace_id,
        payload.operation.action,
        len(payload.task_ids),
    )

    try:
        result = await run_bulk_operation(
            db=db,
            request=payload,
            actor=current_user,
            workspace_id=workspace_id,
        )
    except BulkOperationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.exception(
            "Unexpected error during bulk op for user=%s workspace=%s",
            current_user.id,
            workspace_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing the bulk operation.",
        ) from exc

    if result.total_succeeded == 0 and result.total_failed > 0:
        # Everything failed — surface a 422 so the client knows to inspect
        # the failed list rather than silently accepting a 200.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "All tasks failed to process.",
                "failed": result.failed,
            },
        )

    return result
