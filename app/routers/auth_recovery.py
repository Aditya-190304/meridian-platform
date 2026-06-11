from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.auth_recovery import (
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    UnlockAccountRequest,
    UnlockAccountResponse,
)
from app.services.reset_token_service import ResetTokenService
from app.services.auth_audit_logger import AuthAuditLogger
from app.services.email_dispatcher import EmailDispatcher
from app.crud.users import get_user_by_email

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/forgot-password",
    response_model=ForgotPasswordResponse,
    status_code=status.HTTP_200_OK,
)
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ForgotPasswordResponse:
    """
    Request a password reset email.

    Always returns 200 regardless of whether the email exists in order
    to avoid leaking account information to callers.
    """
    audit = AuthAuditLogger(db)
    token_svc = ResetTokenService(db)
    email_svc = EmailDispatcher()

    user = get_user_by_email(db, email=payload.email)

    if user is not None:
        token = token_svc.create_reset_token(user_id=user.id)
        email_svc.send_password_reset(
            to_address=user.email,
            display_name=user.full_name,
            reset_token=token,
        )
        audit.record(
            event="password_reset_requested",
            user_id=user.id,
            request=request,
            meta={"email": payload.email},
        )

    return ForgotPasswordResponse(
        message="If that email is registered you will receive a reset link shortly."
    )


@router.post(
    "/reset-password",
    response_model=ResetPasswordResponse,
    status_code=status.HTTP_200_OK,
)
def reset_password(
    payload: ResetPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ResetPasswordResponse:
    """
    Consume a password reset token and set a new password.

    The token is single-use and expires after 1 hour. Once used the
    token row is deleted so it cannot be replayed.
    """
    audit = AuthAuditLogger(db)
    token_svc = ResetTokenService(db)

    token_record = token_svc.get_valid_token(token=payload.token)
    if token_record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reset token is invalid or has expired.",
        )

    token_svc.consume_token_and_update_password(
        token_record=token_record,
        new_password=payload.new_password,
    )

    audit.record(
        event="password_reset_completed",
        user_id=token_record.user_id,
        request=request,
        meta={},
    )

    return ResetPasswordResponse(message="Password updated successfully.")


@router.post(
    "/unlock-account",
    response_model=UnlockAccountResponse,
    status_code=status.HTTP_200_OK,
)
def unlock_account(
    payload: UnlockAccountRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> UnlockAccountResponse:
    """
    Request an account unlock for a locked-out user.

    A locked account is one where `failed_login_attempts` has exceeded
    the tenant threshold. Sending a password-reset email also clears
    the lock once the reset is completed.
    """
    audit = AuthAuditLogger(db)
    token_svc = ResetTokenService(db)
    email_svc = EmailDispatcher()

    user = get_user_by_email(db, email=payload.email)

    if user is None:
        return UnlockAccountResponse(
            message="If your account exists and is locked you will receive an unlock email."
        )

    if not user.is_locked:
        return UnlockAccountResponse(
            message="Account is not currently locked. Try logging in normally."
        )

    token = token_svc.create_reset_token(user_id=user.id)
    email_svc.send_account_unlock(
        to_address=user.email,
        display_name=user.full_name,
        reset_token=token,
    )
    audit.record(
        event="account_unlock_requested",
        user_id=user.id,
        request=request,
        meta={"email": payload.email},
    )

    return UnlockAccountResponse(
        message="Unlock instructions have been sent to the address on file."
    )
