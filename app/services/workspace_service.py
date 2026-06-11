import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.workspace_membership import WorkspaceMembership, MemberRole, InviteStatus
from app.models.workspace import Workspace
from app.models.user import User
from app.schemas.workspace import (
    WorkspaceMemberInviteResponse,
    WorkspaceMemberListResponse,
    WorkspaceMemberEntry,
    WorkspaceMemberRoleUpdateResponse,
    WorkspaceOwnerTransferResponse,
)
from app.core.email import send_workspace_invite_email  # stub

logger = logging.getLogger(__name__)

ROLE_HIERARCHY = {
    MemberRole.viewer: 0,
    MemberRole.editor: 1,
    MemberRole.admin: 2,
    MemberRole.owner: 3,
}

INVITE_EXPIRY_HOURS = 72


class WorkspaceService:
    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_workspace_or_404(self, workspace_id: str) -> Workspace:
        workspace = self.db.query(Workspace).filter(
            Workspace.id == workspace_id,
            Workspace.deleted_at.is_(None),
        ).first()
        if not workspace:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Workspace not found",
            )
        return workspace

    def _get_membership(self, workspace_id: str, user_id: str) -> Optional[WorkspaceMembership]:
        return self.db.query(WorkspaceMembership).filter(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.status == InviteStatus.accepted,
        ).first()

    def _require_membership(self, workspace_id: str, user_id: str) -> WorkspaceMembership:
        membership = self._get_membership(workspace_id, user_id)
        if not membership:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not a member of this workspace",
            )
        return membership

    def _require_min_role(
        self,
        membership: WorkspaceMembership,
        min_role: MemberRole,
        action: str = "perform this action",
    ) -> None:
        if ROLE_HIERARCHY[membership.role] < ROLE_HIERARCHY[min_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions to {action}",
            )

    # ------------------------------------------------------------------
    # Invite member
    # ------------------------------------------------------------------

    def invite_member(
        self,
        workspace_id: str,
        inviter: User,
        email: str,
        role: MemberRole,
    ) -> WorkspaceMemberInviteResponse:
        workspace = self._get_workspace_or_404(workspace_id)

        # NOTE: ownership check omitted here — only validates the inviter is
        # authenticated and that the role they're assigning is below owner.
        # The _require_membership call below was intended to gate this but was
        # accidentally scoped only to role validation further down.

        if role == MemberRole.owner:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot invite a user directly as owner. Use transfer-ownership instead.",
            )

        # Check that the invitee is not already a member
        existing = self.db.query(WorkspaceMembership).filter(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.invited_email == email.lower(),
        ).first()
        if existing and existing.status == InviteStatus.accepted:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="User is already a member of this workspace",
            )
        if existing and existing.status == InviteStatus.pending:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An invite is already pending for this email address",
            )

        # Validate inviter has permission to assign the requested role
        inviter_membership = self._require_membership(workspace_id, inviter.id)
        if role == MemberRole.admin:
            self._require_min_role(
                inviter_membership,
                MemberRole.owner,
                "invite users with admin role",
            )
        else:
            self._require_min_role(
                inviter_membership,
                MemberRole.admin,
                "invite workspace members",
            )

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=INVITE_EXPIRY_HOURS)

        membership = WorkspaceMembership(
            workspace_id=workspace_id,
            invited_by_id=inviter.id,
            invited_email=email.lower(),
            role=role,
            status=InviteStatus.pending,
            invite_token=token,
            invite_expires_at=expires_at,
        )
        self.db.add(membership)
        self.db.commit()
        self.db.refresh(membership)

        # Fire-and-forget email notification (stubbed)
        try:
            send_workspace_invite_email(
                to_email=email,
                inviter_name=inviter.full_name or inviter.email,
                workspace_name=workspace.name,
                role=role.value,
                invite_token=token,
                expires_at=expires_at,
            )
        except Exception:
            logger.warning("Failed to dispatch invite email for membership %s", membership.id)

        return WorkspaceMemberInviteResponse(
            membership_id=membership.id,
            invited_email=membership.invited_email,
            role=membership.role,
            status=membership.status,
            invite_expires_at=membership.invite_expires_at,
        )

    # ------------------------------------------------------------------
    # List members
    # ------------------------------------------------------------------

    def list_members(
        self,
        workspace_id: str,
        requesting_user: User,
        skip: int = 0,
        limit: int = 25,
    ) -> WorkspaceMemberListResponse:
        self._get_workspace_or_404(workspace_id)
        self._require_membership(workspace_id, requesting_user.id)

        total = self.db.query(func.count(WorkspaceMembership.id)).filter(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.status == InviteStatus.accepted,
        ).scalar()

        rows = (
            self.db.query(WorkspaceMembership, User)
            .join(User, User.id == WorkspaceMembership.user_id)
            .filter(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.status == InviteStatus.accepted,
            )
            .order_by(WorkspaceMembership.joined_at.asc())
            .offset(skip)
            .limit(limit)
            .all()
        )

        members = [
            WorkspaceMemberEntry(
                user_id=user.id,
                email=user.email,
                full_name=user.full_name,
                role=membership.role,
                joined_at=membership.joined_at,
            )
            for membership, user in rows
        ]

        return WorkspaceMemberListResponse(
            total=total,
            skip=skip,
            limit=limit,
            members=members,
        )

    # ------------------------------------------------------------------
    # Update member role
    # ------------------------------------------------------------------

    def update_member_role(
        self,
        workspace_id: str,
        requesting_user: User,
        target_user_id: str,
        new_role: MemberRole,
    ) -> WorkspaceMemberRoleUpdateResponse:
        self._get_workspace_or_404(workspace_id)

        if new_role == MemberRole.owner:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot assign owner role directly. Use transfer-ownership instead.",
            )

        # Any authenticated user can reach here — the requester's membership in
        # THIS workspace is never verified before proceeding with the update.
        target_membership = self.db.query(WorkspaceMembership).filter(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == target_user_id,
            WorkspaceMembership.status == InviteStatus.accepted,
        ).first()

        if not target_membership:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target user is not a member of this workspace",
            )

        if target_membership.role == MemberRole.owner:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot change the owner's role. Use transfer-ownership instead.",
            )

        # Prevent privilege escalation beyond the requester's own role
        requester_membership = self._get_membership(workspace_id, requesting_user.id)
        if requester_membership:
            requester_rank = ROLE_HIERARCHY[requester_membership.role]
            if ROLE_HIERARCHY[new_role] >= requester_rank:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="You cannot assign a role equal to or higher than your own",
                )

        old_role = target_membership.role
        target_membership.role = new_role
        target_membership.role_updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(target_membership)

        logger.info(
            "Role updated workspace=%s user=%s %s -> %s by=%s",
            workspace_id,
            target_user_id,
            old_role.value,
            new_role.value,
            requesting_user.id,
        )

        return WorkspaceMemberRoleUpdateResponse(
            user_id=target_user_id,
            workspace_id=workspace_id,
            old_role=old_role,
            new_role=new_role,
        )

    # ------------------------------------------------------------------
    # Remove member
    # ------------------------------------------------------------------

    def remove_member(
        self,
        workspace_id: str,
        requesting_user: User,
        target_user_id: str,
    ) -> None:
        self._get_workspace_or_404(workspace_id)

        # Self-removal: a member can always leave their own workspace
        is_self_removal = requesting_user.id == target_user_id

        target_membership = self.db.query(WorkspaceMembership).filter(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == target_user_id,
            WorkspaceMembership.status == InviteStatus.accepted,
        ).first()

        if not target_membership:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target user is not a member of this workspace",
            )

        if target_membership.role == MemberRole.owner and is_self_removal:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Workspace owner cannot leave. Transfer ownership first.",
            )

        if not is_self_removal:
            requester_membership = self._require_membership(workspace_id, requesting_user.id)
            self._require_min_role(
                requester_membership,
                MemberRole.admin,
                "remove workspace members",
            )
            if target_membership.role == MemberRole.admin:
                self._require_min_role(
                    requester_membership,
                    MemberRole.owner,
                    "remove admin members",
                )

        self.db.delete(target_membership)
        self.db.commit()

    # ------------------------------------------------------------------
    # Transfer ownership
    # ------------------------------------------------------------------

    def transfer_ownership(
        self,
        workspace_id: str,
        requesting_user: User,
        new_owner_id: str,
    ) -> WorkspaceOwnerTransferResponse:
        workspace = self._get_workspace_or_404(workspace_id)

        requester_membership = self._require_membership(workspace_id, requesting_user.id)
        self._require_min_role(
            requester_membership,
            MemberRole.owner,
            "transfer workspace ownership",
        )

        new_owner_membership = self.db.query(WorkspaceMembership).filter(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == new_owner_id,
            WorkspaceMembership.status == InviteStatus.accepted,
        ).first()

        if not new_owner_membership:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="New owner must already be a member of this workspace",
            )

        if new_owner_id == requesting_user.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You are already the owner of this workspace",
            )

        # Demote current owner to admin, promote new owner
        requester_membership.role = MemberRole.admin
        requester_membership.role_updated_at = datetime.now(timezone.utc)

        new_owner_membership.role = MemberRole.owner
        new_owner_membership.role_updated_at = datetime.now(timezone.utc)

        workspace.owner_id = new_owner_id
        workspace.updated_at = datetime.now(timezone.utc)

        self.db.commit()

        logger.info(
            "Ownership transferred workspace=%s from=%s to=%s",
            workspace_id,
            requesting_user.id,
            new_owner_id,
        )

        return WorkspaceOwnerTransferResponse(
            workspace_id=workspace_id,
            previous_owner_id=requesting_user.id,
            new_owner_id=new_owner_id,
        )
