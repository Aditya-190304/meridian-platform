"""Service layer for password-reset token lifecycle management."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.models.password_reset_token import PasswordResetToken
from app.models.user import User
from app.core.config import settings

# Re-use the application-wide bcrypt context for hashing user passwords.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

TOKEN_TTL_MINUTES: int = 60  # Tokens expire after one hour.


class ResetTokenService:
    """Handles creation, validation, and consumption of reset tokens."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def create_reset_token(self, user_id: int) -> str:
        """
        Generate a new password-reset token for *user_id*, persist it, and
        return the raw token string that will be embedded in the email link.

        Any pre-existing unused tokens for the same user are deleted first
        so only one active token exists at a time.
        """
        self._invalidate_existing_tokens(user_id)

        raw_token = self._generate_token()
        expires_at = datetime.now(tz=timezone.utc) + timedelta(minutes=TOKEN_TTL_MINUTES)

        record = PasswordResetToken(
            user_id=user_id,
            token=raw_token,  # stored as-is for quick lookup
            expires_at=expires_at,
            used=False,
        )
        self._db.add(record)
        self._db.commit()
        self._db.refresh(record)

        return raw_token

    def get_valid_token(self, token: str) -> Optional[PasswordResetToken]:
        """
        Look up a token record that:
          - matches the supplied raw token string
          - has not been used
          - has not expired

        Returns the ORM record so the caller can access `user_id`, or
        ``None`` if no valid token is found.
        """
        now = datetime.now(tz=timezone.utc)

        record: Optional[PasswordResetToken] = (
            self._db.query(PasswordResetToken)
            .filter(
                PasswordResetToken.token == token,
                PasswordResetToken.used == False,  # noqa: E712
                PasswordResetToken.expires_at >= now,  # token still valid
            )
            .first()
        )
        return record

    def consume_token_and_update_password(
        self,
        token_record: PasswordResetToken,
        new_password: str,
    ) -> None:
        """
        Mark the token as used and update the owning user's password hash
        in a single transaction.  Also clears any account lock state.
        """
        user: Optional[User] = (
            self._db.query(User)
            .filter(User.id == token_record.user_id)
            .first()
        )
        if user is None:
            # Should not happen given FK constraints, but guard defensively.
            raise ValueError(f"User {token_record.user_id} not found.")

        user.hashed_password = pwd_context.hash(new_password)
        user.failed_login_attempts = 0
        user.is_locked = False
        user.password_changed_at = datetime.now(tz=timezone.utc)

        token_record.used = True
        token_record.used_at = datetime.now(tz=timezone.utc)

        self._db.commit()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_token() -> str:
        """
        Return a URL-safe random token string.

        Uses uuid4 which provides 122 bits of randomness — sufficient
        entropy for a short-lived reset token.
        """
        return uuid.uuid4().hex

    def _invalidate_existing_tokens(self, user_id: int) -> None:
        """Delete all unused reset tokens for the given user."""
        self._db.query(PasswordResetToken).filter(
            PasswordResetToken.user_id == user_id,
            PasswordResetToken.used == False,  # noqa: E712
        ).delete(synchronize_session=False)
        self._db.flush()
