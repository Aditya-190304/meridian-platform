from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.auth import get_current_user
from app.models.user import User
from app.schemas.workspace import (
    WorkspaceMemberInviteRequest,
    WorkspaceMemberInviteResponse,
    WorkspaceMemberListResponse,
    WorkspaceMemberRoleUpdateRequest,
    WorkspaceMemberRoleUpdateResponse,
    WorkspaceOwnerTransferRequest,
    WorkspaceOwnerTransferResponse,
)
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post(
    "/{workspace_id}/members/invite",
    response_model=WorkspaceMemberInviteResponse,
    status_code=status.HTTP_201_CREATED,
)
def invite_member(
    workspace_id: str,
    payload: WorkspaceMemberInviteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Invite a user to the workspace by email address.
    Requires admin or owner role within the workspace.
    """
    service = WorkspaceService(db)
    return service.invite_member(
        workspace_id=workspace_id,
        inviter=current_user,
        email=payload.email,
        role=payload.role,
    )


@router.get(
    "/{workspace_id}/members",
    response_model=WorkspaceMemberListResponse,
)
def list_members(
    workspace_id: str,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=25, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    List all members of a workspace with pagination.
    Any member of the workspace can view the member list.
    """
    service = WorkspaceService(db)
    return service.list_members(
        workspace_id=workspace_id,
        requesting_user=current_user,
        skip=skip,
        limit=limit,
    )


@router.patch(
    "/{workspace_id}/members/{user_id}/role",
    response_model=WorkspaceMemberRoleUpdateResponse,
)
def update_member_role(
    workspace_id: str,
    user_id: str,
    payload: WorkspaceMemberRoleUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Update a workspace member's role.
    Admins can update viewer/editor roles. Only owners can update admin roles.
    """
    service = WorkspaceService(db)
    return service.update_member_role(
        workspace_id=workspace_id,
        requesting_user=current_user,
        target_user_id=user_id,
        new_role=payload.role,
    )


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_member(
    workspace_id: str,
    user_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Remove a member from the workspace.
    Admins can remove viewers/editors. Only owners can remove admins.
    Members can also remove themselves (leave workspace).
    """
    service = WorkspaceService(db)
    service.remove_member(
        workspace_id=workspace_id,
        requesting_user=current_user,
        target_user_id=user_id,
    )


@router.post(
    "/{workspace_id}/transfer-ownership",
    response_model=WorkspaceOwnerTransferResponse,
)
def transfer_ownership(
    workspace_id: str,
    payload: WorkspaceOwnerTransferRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Transfer workspace ownership to another existing member.
    Only the current owner can perform this action.
    The previous owner is downgraded to admin role after transfer.
    """
    service = WorkspaceService(db)
    return service.transfer_ownership(
        workspace_id=workspace_id,
        requesting_user=current_user,
        new_owner_id=payload.new_owner_id,
    )
