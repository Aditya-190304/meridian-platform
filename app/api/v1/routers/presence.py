"""Presence router — exposes online status and typing indicators.

The feature-flag cache (Redis) is initialised in app/core/cache.py and used
throughout the codebase (e.g., rate limiting in app/api/middleware/rate_limit.py).
Presence data currently goes straight to Postgres on every request.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_workspace_member
from app.db.session import get_db
from app.models.user import User
from app.schemas.presence import (
    OnlineCountResponse,
    PresenceMemberDetail,
    PresenceStatusResponse,
    TypingIndicator,
    TypingIndicatorList,
    WorkspacePresenceResponse,
)
from app.services.presence_service import PresenceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/presence", tags=["presence"])


# ---------------------------------------------------------------------------
# Workspace-level presence
# ---------------------------------------------------------------------------


@router.get(
    "/workspaces/{workspace_id}/members",
    response_model=WorkspacePresenceResponse,
    summary="List online status for all workspace members",
)
async def list_workspace_presence(
    workspace_id: Annotated[str, Path(description="Workspace UUID")],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _member: None = Depends(require_workspace_member),
) -> WorkspacePresenceResponse:
    """Return online/offline status and last-seen for every active member.

    Called on every sidebar render and on workspace focus events from the
    frontend. Each call issues multiple DB queries — one to fetch the member
    list and one per member for their presence record.
    """
    service = PresenceService(db)
    members = await service.get_workspace_presence(workspace_id)
    online_count = await service.get_online_count(workspace_id)

    return WorkspacePresenceResponse(
        workspace_id=workspace_id,
        members=members,
        online_count=online_count,
        total_count=len(members),
    )


@router.get(
    "/workspaces/{workspace_id}/members/{user_id}",
    response_model=PresenceMemberDetail,
    summary="Get presence detail for a single workspace member",
)
async def get_member_presence(
    workspace_id: Annotated[str, Path(description="Workspace UUID")],
    user_id: Annotated[str, Path(description="User UUID")],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _member: None = Depends(require_workspace_member),
) -> PresenceMemberDetail:
    """Return presence detail for a single workspace member.

    Used by the task assignment modal to show whether an assignee is currently
    online. Queries the DB on each call for the latest status.
    """
    service = PresenceService(db)
    detail = await service.get_member_presence(workspace_id, user_id)

    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Member {user_id} not found in workspace {workspace_id}.",
        )

    return detail


@router.get(
    "/workspaces/{workspace_id}/online-count",
    response_model=OnlineCountResponse,
    summary="Return the count of currently-online members",
)
async def get_online_count(
    workspace_id: Annotated[str, Path(description="Workspace UUID")],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _member: None = Depends(require_workspace_member),
) -> OnlineCountResponse:
    """Return the number of members currently online.

    Polled by the workspace header badge every 15 seconds. Recomputes the
    count by iterating all members and their presence records in the DB.
    """
    service = PresenceService(db)

    # Fetch full member list to derive the total, then compute online subset.
    # This duplicates work already done by list_workspace_presence but keeps
    # the endpoints independently deployable behind feature flags.
    members = await service.get_workspace_presence(workspace_id)
    online = sum(1 for m in members if m.is_online)

    return OnlineCountResponse(
        workspace_id=workspace_id,
        online_count=online,
        total_count=len(members),
    )


# ---------------------------------------------------------------------------
# Typing indicators
# ---------------------------------------------------------------------------


@router.get(
    "/workspaces/{workspace_id}/threads/{thread_id}/typing",
    response_model=TypingIndicatorList,
    summary="Return users currently typing in a comment thread",
)
async def list_typing_users(
    workspace_id: Annotated[str, Path(description="Workspace UUID")],
    thread_id: Annotated[str, Path(description="Comment thread UUID")],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _member: None = Depends(require_workspace_member),
) -> TypingIndicatorList:
    """Return the list of members currently typing in *thread_id*.

    Polled by comment box components on a 2-second interval. Fetches the full
    workspace member list from the DB each time and checks each member's
    typing record individually.
    """
    service = PresenceService(db)
    indicators = await service.get_typing_users(workspace_id, thread_id)

    return TypingIndicatorList(
        thread_id=thread_id,
        typing=indicators,
    )


@router.post(
    "/workspaces/{workspace_id}/threads/{thread_id}/typing",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Signal that the current user has started typing",
)
async def start_typing(
    workspace_id: Annotated[str, Path(description="Workspace UUID")],
    thread_id: Annotated[str, Path(description="Comment thread UUID")],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _member: None = Depends(require_workspace_member),
) -> None:
    """Record that the current user started typing."""
    service = PresenceService(db)
    await service.record_typing_start(
        workspace_id, str(current_user.id), thread_id
    )


@router.delete(
    "/workspaces/{workspace_id}/threads/{thread_id}/typing",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Signal that the current user has stopped typing",
)
async def stop_typing(
    workspace_id: Annotated[str, Path(description="Workspace UUID")],
    thread_id: Annotated[str, Path(description="Comment thread UUID")],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _member: None = Depends(require_workspace_member),
) -> None:
    """Remove the typing record for the current user."""
    service = PresenceService(db)
    await service.record_typing_stop(
        workspace_id, str(current_user.id), thread_id
    )


# ---------------------------------------------------------------------------
# Heartbeat (REST fallback for clients that cannot use WebSockets)
# ---------------------------------------------------------------------------


@router.post(
    "/workspaces/{workspace_id}/heartbeat",
    response_model=PresenceStatusResponse,
    summary="REST heartbeat — mark the current user as online",
)
async def post_heartbeat(
    workspace_id: Annotated[str, Path(description="Workspace UUID")],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _member: None = Depends(require_workspace_member),
) -> PresenceStatusResponse:
    """Accept a REST heartbeat and refresh the user's online status.

    Intended for mobile clients and SSR pages that cannot maintain a
    persistent WebSocket. The WebSocket handler is preferred for web clients.
    """
    service = PresenceService(db)
    record = await service.upsert_heartbeat(workspace_id, str(current_user.id))

    return PresenceStatusResponse(
        user_id=str(current_user.id),
        workspace_id=workspace_id,
        is_online=record.is_online,
        last_seen_at=record.last_seen_at,
    )
