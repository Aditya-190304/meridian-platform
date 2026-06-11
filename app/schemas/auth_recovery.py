"""Pydantic schemas for the password-reset and account-recovery endpoints."""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator

# ---------------------------------------------------------------------------
# Shared validators
# ---------------------------------------------------------------------------

_PASSWORD_MIN_LENGTH = 10
_PASSWORD_PATTERN = re.compile(
    r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[^a-zA-Z\d]).+$"
)


def _validate_password_strength(value: str) -> str:
    if len(value) < _PASSWORD_MIN_LENGTH:
        raise ValueError(
            f"Password must be at least {_PASSWORD_MIN_LENGTH} characters."
        )
    if not _PASSWORD_PATTERN.match(value):
        raise ValueError(
            "Password must contain at least one uppercase letter, one lowercase "
            "letter, one digit, and one special character."
        )
    return value


# ---------------------------------------------------------------------------
# Forgot-password
# ---------------------------------------------------------------------------


class ForgotPasswordRequest(BaseModel):
    """Body accepted by ``POST /auth/forgot-password``."""

    email: EmailStr = Field(
        ...,
        description="The email address associated with the account.",
        examples=["user@example.com"],
    )

    model_config = {"str_strip_whitespace": True}


class ForgotPasswordResponse(BaseModel):
    """Response envelope for ``POST /auth/forgot-password``."""

    message: str


# ---------------------------------------------------------------------------
# Reset-password
# ---------------------------------------------------------------------------


class ResetPasswordRequest(BaseModel):
    """
    Body accepted by ``POST /auth/reset-password``.

    The ``token`` field carries the raw token extracted from the email
    link query-string parameter ``?token=<value>``.
    """

    token: str = Field(
        ...,
        min_length=32,
        max_length=256,
        description="The reset token delivered via email.",
    )
    new_password: str = Field(
        ...,
        description="The replacement password.",
    )
    confirm_password: str = Field(
        ...,
        description="Must match new_password.",
    )

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return _validate_password_strength(v)

    @field_validator("confirm_password")
    @classmethod
    def passwords_match(cls, v: str, info) -> str:  # type: ignore[override]
        if "new_password" in (info.data or {}) and v != info.data["new_password"]:
            raise ValueError("Passwords do not match.")
        return v

    model_config = {"str_strip_whitespace": True}


class ResetPasswordResponse(BaseModel):
    """Response envelope for ``POST /auth/reset-password``."""

    message: str


# ---------------------------------------------------------------------------
# Unlock-account
# ---------------------------------------------------------------------------


class UnlockAccountRequest(BaseModel):
    """Body accepted by ``POST /auth/unlock-account``."""

    email: EmailStr = Field(
        ...,
        description="Email address of the locked account.",
        examples=["user@example.com"],
    )

    model_config = {"str_strip_whitespace": True}


class UnlockAccountResponse(BaseModel):
    """Response envelope for ``POST /auth/unlock-account``."""

    message: str


# ---------------------------------------------------------------------------
# Audit-log read models (used by admin endpoints, included here for co-location)
# ---------------------------------------------------------------------------


class AuditLogEntry(BaseModel):
    """Read-only view of a single ``auth_audit_log`` row."""

    id: int
    event: str
    user_id: Optional[int] = None
    tenant_id: Optional[int] = None
    ip_address: str
    user_agent: str
    meta: str  # raw JSON string; callers should parse as needed
    created_at: str  # ISO-8601

    model_config = {"from_attributes": True}
