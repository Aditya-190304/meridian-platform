from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.models.workspace_membership import InviteStatus, MemberRole


# ---------------------------------------------------------------------------
# Invite member
# ---------------------------------------------------------------------------

class WorkspaceMemberInviteRequest(BaseModel):
    email: EmailStr = Field(..., description="Email address of the person to invite")
    role: MemberRole = Field(
        default=MemberRole.viewer,
        description="Role to assign upon accepting the invite",
    )

    @field_validator("role")
    @classmethod
    def role_cannot_be_owner(cls, v: MemberRole) -> MemberRole:
        if v == MemberRole.owner:
            raise ValueError("Cannot invite a member with the owner role")
        return v

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"email": "alice@example.com", "role": "editor"}
            ]
        }
    }


class WorkspaceMemberInviteResponse(BaseModel):
    membership_id: UUID
    invited_email: str
    role: MemberRole
    status: InviteStatus
    invite_expires_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# List members
# ---------------------------------------------------------------------------

class WorkspaceMemberEntry(BaseModel):
    user_id: UUID
    email: str
    full_name: Optional[str] = None
    role: MemberRole
    joined_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class WorkspaceMemberListResponse(BaseModel):
    total: int = Field(..., description="Total number of accepted members")
    skip: int
    limit: int
    members: List[WorkspaceMemberEntry]


# ---------------------------------------------------------------------------
# Update member role
# ---------------------------------------------------------------------------

class WorkspaceMemberRoleUpdateRequest(BaseModel):
    role: MemberRole = Field(..., description="New role to assign to the member")

    @field_validator("role")
    @classmethod
    def role_cannot_be_owner(cls, v: MemberRole) -> MemberRole:
        if v == MemberRole.owner:
            raise ValueError(
                "Cannot assign owner role via this endpoint. Use transfer-ownership."
            )
        return v

    model_config = {
        "json_schema_extra": {
            "examples": [{"role": "editor"}]
        }
    }


class WorkspaceMemberRoleUpdateResponse(BaseModel):
    user_id: UUID
    workspace_id: UUID
    old_role: MemberRole
    new_role: MemberRole

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Transfer ownership
# ---------------------------------------------------------------------------

class WorkspaceOwnerTransferRequest(BaseModel):
    new_owner_id: UUID = Field(
        ...,
        description="User ID of the member who will become the new workspace owner",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{"new_owner_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"}]
        }
    }


class WorkspaceOwnerTransferResponse(BaseModel):
    workspace_id: UUID
    previous_owner_id: UUID
    new_owner_id: UUID

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Generic workspace schemas (existing, referenced elsewhere)
# ---------------------------------------------------------------------------

class WorkspaceBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    slug: str = Field(..., min_length=1, max_length=60, pattern=r"^[a-z0-9-]+$")
    description: Optional[str] = Field(default=None, max_length=500)


class WorkspaceCreateRequest(WorkspaceBase):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "name": "Acme Corp",
                    "slug": "acme-corp",
                    "description": "Main workspace for Acme Corp projects",
                }
            ]
        }
    }


class WorkspaceResponse(WorkspaceBase):
    id: UUID
    owner_id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
