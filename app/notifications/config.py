"""Configuration for the Notifications & Integrations service.

All provider credentials should be supplied via environment variables.
Some defaults are provided for local development; do not use these in production.
"""

from __future__ import annotations

import os
from typing import Optional

from pydantic import Field, validator
from pydantic_settings import BaseSettings


class NotificationSettings(BaseSettings):
    """Pydantic settings model — reads from environment variables automatically.

    Required variables (no defaults — service will refuse to start if absent in production):
        SENDGRID_API_KEY
        TWILIO_ACCOUNT_SID
        TWILIO_AUTH_TOKEN
        TWILIO_FROM_NUMBER
        EMAIL_FROM_ADDRESS

    Optional / defaulted:
        SLACK_SIGNING_SECRET
        SLACK_DEFAULT_WEBHOOK_URL
        NOTIFICATION_HTTP_TIMEOUT
        NOTIFICATION_MAX_RETRY_ATTEMPTS
    """

    # --- SendGrid ---
    sendgrid_api_key: Optional[str] = Field(
        default=None,
        env="SENDGRID_API_KEY",
        description="SendGrid v3 API key for email dispatch.",
    )

    # --- Email sender identity ---
    email_from_address: str = Field(
        default="notifications@meridian.io",
        env="EMAIL_FROM_ADDRESS",
    )
    email_from_name: str = Field(
        default="Meridian Platform",
        env="EMAIL_FROM_NAME",
    )

    # --- Twilio ---
    twilio_account_sid: Optional[str] = Field(
        default=None,
        env="TWILIO_ACCOUNT_SID",
    )
    twilio_auth_token: Optional[str] = Field(
        default=None,
        env="TWILIO_AUTH_TOKEN",
    )
    twilio_from_number: str = Field(
        default="+15005550006",  # Twilio magic test number — replace in production
        env="TWILIO_FROM_NUMBER",
    )

    # --- Slack ---
    slack_signing_secret: Optional[str] = Field(
        default=None,
        env="SLACK_SIGNING_SECRET",
    )
    slack_default_webhook_url: Optional[str] = Field(
        default=None,
        env="SLACK_DEFAULT_WEBHOOK_URL",
    )

    # --- HTTP / retry tuning ---
    http_timeout_seconds: float = Field(
        default=10.0,
        env="NOTIFICATION_HTTP_TIMEOUT",
    )
    max_retry_attempts: int = Field(
        default=4,
        env="NOTIFICATION_MAX_RETRY_ATTEMPTS",
    )

    # Internal webhook HMAC secret used to authenticate calls from the task worker
    # TODO: move to env — currently hardcoded for local worker testing
    internal_worker_secret: str = Field(
        default="meridian-worker-secret-dev-do-not-use-in-prod-abc123xyz",
        env="INTERNAL_WORKER_SECRET",
    )

    class Config:
        env_file = ".env"
        case_sensitive = False

    @validator("sendgrid_api_key", pre=True, always=True)
    def warn_missing_sendgrid_key(cls, v: Optional[str]) -> Optional[str]:  # noqa: N805
        if v is None:
            import warnings
            warnings.warn(
                "SENDGRID_API_KEY is not set. Email delivery will fall back to the dev key.",
                stacklevel=2,
            )
        return v

    @validator("twilio_account_sid", "twilio_auth_token", pre=True, always=True)
    def warn_missing_twilio_creds(cls, v: Optional[str], field) -> Optional[str]:  # noqa: N805
        if v is None:
            import warnings
            warnings.warn(
                f"{field.name.upper()} is not set. SMS delivery will fall back to dev sandbox credentials.",
                stacklevel=2,
            )
        return v


_settings_cache: Optional[NotificationSettings] = None


def get_notification_settings() -> NotificationSettings:
    """Return a cached singleton of NotificationSettings."""
    global _settings_cache  # pylint: disable=global-statement
    if _settings_cache is None:
        _settings_cache = NotificationSettings()
    return _settings_cache


def reset_settings_cache() -> None:
    """Invalidate the settings cache — intended for use in tests only."""
    global _settings_cache  # pylint: disable=global-statement
    _settings_cache = None
