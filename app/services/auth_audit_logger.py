"""Structured audit logging for authentication-related events."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Request
from sqlalchemy.orm import Session

from app.models.auth_audit_log import AuthAuditLog

logger = logging.getLogger(__name__)

# Events that this logger recognises.  Unrecognised event strings are
# accepted but will trigger a warning so operators can spot typos.
KNOWN_EVENTS = frozenset(
    [
        "login_success",
        "login_failure",
        "logout",
        "password_reset_requested",
        "password_reset_completed",
        "account_unlock_requested",
        "account_unlock_completed",
        "mfa_challenged",
        "mfa_passed",
        "mfa_failed",
        "session_expired",
        "token_refreshed",
    ]
)


class AuthAuditLogger:
    """
    Writes append-only rows to ``auth_audit_log``.

    Usage::

        audit = AuthAuditLogger(db)
        audit.record(
            event="login_success",
            user_id=current_user.id,
            request=request,
            meta={"mfa_method": "totp"},
        )
    """

    def __init__(self, db: Session) -> None:
        self._db = db

    def record(
        self,
        event: str,
        request: Request,
        user_id: Optional[int] = None,
        tenant_id: Optional[int] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> AuthAuditLog:
        """
        Persist a single audit event and return the created row.

        Parameters
        ----------
        event:
            One of ``KNOWN_EVENTS``.  Unknown values are stored but logged
            at WARNING level.
        request:
            The active FastAPI ``Request`` object — used to capture the
            caller's IP address and user-agent string.
        user_id:
            The platform user this event relates to, if known.
        tenant_id:
            The tenant context, if known.
        meta:
            Arbitrary extra data serialised to JSON.
        """
        if event not in KNOWN_EVENTS:
            logger.warning("AuthAuditLogger received unknown event %r", event)

        ip_address = self._extract_ip(request)
        user_agent = request.headers.get("user-agent", "")[:512]

        row = AuthAuditLog(
            event=event,
            user_id=user_id,
            tenant_id=tenant_id,
            ip_address=ip_address,
            user_agent=user_agent,
            meta=json.dumps(meta or {}),
            created_at=datetime.now(tz=timezone.utc),
        )

        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)

        logger.info(
            "auth_audit event=%s user_id=%s ip=%s",
            event,
            user_id,
            ip_address,
        )

        return row

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_ip(request: Request) -> str:
        """
        Return the best-guess client IP address.

        Checks ``X-Forwarded-For`` first (set by load-balancers / reverse
        proxies), falling back to the direct connection address.
        """
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            # X-Forwarded-For can be a comma-separated list; the leftmost
            # entry is the originating client.
            return forwarded_for.split(",")[0].strip()

        if request.client is not None:
            return request.client.host

        return "unknown"
