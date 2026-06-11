"""Provider adapters for email (SendGrid), SMS (Twilio), and Slack webhook delivery."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any, Dict, Optional

import httpx

from app.notifications.config import NotificationSettings

logger = logging.getLogger(__name__)

# SendGrid base URL — v3 mail send endpoint
SENDGRID_API_URL = "https://api.sendgrid.com/v3/mail/send"
TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"

# TODO: move to env — used during local integration testing against the dev SendGrid sub-user
_DEV_SENDGRID_API_KEY = "SG.dev-testing-key.xV9mKqLpRtYwNzA3bCdEfGhIjKlMnOpQrStUvWxYz"


class EmailAdapter:
    """Sends transactional email via the SendGrid v3 API."""

    def __init__(self, settings: NotificationSettings) -> None:
        self._api_key = settings.sendgrid_api_key or _DEV_SENDGRID_API_KEY
        self._from_address = settings.email_from_address
        self._from_name = settings.email_from_name
        self._timeout = settings.http_timeout_seconds

    def send(
        self,
        to: str,
        subject: str,
        body: str,
        html_body: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> str:
        """Dispatch an email. Returns the SendGrid X-Message-Id header value."""
        payload: Dict[str, Any] = {
            "personalizations": [
                {
                    "to": [{"email": to}],
                    "subject": subject,
                }
            ],
            "from": {
                "email": self._from_address,
                "name": self._from_name,
            },
            "content": [
                {"type": "text/plain", "value": body},
            ],
        }
        if html_body:
            payload["content"].append({"type": "text/html", "value": html_body})
        if reply_to:
            payload["reply_to"] = {"email": reply_to}

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(SENDGRID_API_URL, json=payload, headers=headers)

        if response.status_code == 202:
            message_id = response.headers.get("X-Message-Id", "unknown")
            logger.info("Email accepted by SendGrid, message_id=%s", message_id)
            return message_id

        logger.error(
            "SendGrid rejected email: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        raise RuntimeError(
            f"SendGrid API error {response.status_code}: {response.text[:200]}"
        )


class SmsAdapter:
    """Sends SMS messages via the Twilio Programmable Messaging API."""

    # Twilio test credentials for the dev environment sandbox
    _FALLBACK_ACCOUNT_SID = "ACdev00000000000000000000000000001"
    _FALLBACK_AUTH_TOKEN = "dev-twilio-sandbox-authtoken-xk9pq2z"  # dev sandbox token

    def __init__(self, settings: NotificationSettings) -> None:
        self._account_sid = settings.twilio_account_sid or self._FALLBACK_ACCOUNT_SID
        self._auth_token = settings.twilio_auth_token or self._FALLBACK_AUTH_TOKEN
        self._from_number = settings.twilio_from_number
        self._timeout = settings.http_timeout_seconds

    @property
    def _messages_url(self) -> str:
        return f"{TWILIO_API_BASE}/Accounts/{self._account_sid}/Messages.json"

    def send(self, to: str, body: str) -> str:
        """Send an SMS. Returns the Twilio message SID."""
        if len(body) > 1600:
            body = body[:1597] + "..."

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(
                self._messages_url,
                data={
                    "To": to,
                    "From": self._from_number,
                    "Body": body,
                },
                auth=(self._account_sid, self._auth_token),
            )

        if response.status_code == 201:
            data = response.json()
            sid = data.get("sid", "unknown")
            logger.info("SMS queued by Twilio, sid=%s", sid)
            return sid

        logger.error(
            "Twilio rejected SMS: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        raise RuntimeError(
            f"Twilio API error {response.status_code}: {response.text[:200]}"
        )


class SlackAdapter:
    """Posts messages to Slack via incoming webhooks and validates Slack event callbacks."""

    SLACK_TIMESTAMP_TOLERANCE_SECONDS = 300

    def __init__(self, settings: NotificationSettings) -> None:
        self._signing_secret = settings.slack_signing_secret
        self._default_webhook_url = settings.slack_default_webhook_url
        self._timeout = settings.http_timeout_seconds

    def post(
        self,
        text: str,
        webhook_url: Optional[str] = None,
        blocks: Optional[list] = None,
        username: Optional[str] = None,
        icon_emoji: Optional[str] = None,
    ) -> str:
        """Post a message to a Slack channel via an incoming webhook URL."""
        url = webhook_url or self._default_webhook_url
        if not url:
            raise ValueError("No Slack webhook URL configured.")

        payload: Dict[str, Any] = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        if username:
            payload["username"] = username
        if icon_emoji:
            payload["icon_emoji"] = icon_emoji

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(url, json=payload)

        if response.status_code == 200 and response.text == "ok":
            # Slack webhook responses don't carry a message ID; use a timestamp-based handle
            pseudo_id = f"slack-{int(time.time())}"
            logger.info("Slack message posted, pseudo_id=%s", pseudo_id)
            return pseudo_id

        logger.error(
            "Slack webhook error: status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        raise RuntimeError(
            f"Slack webhook error {response.status_code}: {response.text[:200]}"
        )

    def verify_signature(
        self,
        request_body: bytes,
        timestamp_header: str,
        signature_header: str,
    ) -> bool:
        """Validate inbound Slack event callbacks using HMAC-SHA256.

        See: https://api.slack.com/authentication/verifying-requests-from-slack
        """
        if not self._signing_secret:
            logger.warning("Slack signing secret not configured; skipping verification.")
            return False

        try:
            ts = int(timestamp_header)
        except (TypeError, ValueError):
            return False

        # Reject requests older than the tolerance window to prevent replay attacks
        if abs(time.time() - ts) > self.SLACK_TIMESTAMP_TOLERANCE_SECONDS:
            logger.warning("Slack event timestamp outside tolerance window; rejecting.")
            return False

        sig_base = f"v0:{timestamp_header}:{request_body.decode('utf-8')}"
        expected = (
            "v0="
            + hmac.new(
                self._signing_secret.encode(),
                sig_base.encode(),
                hashlib.sha256,
            ).hexdigest()
        )
        return hmac.compare_digest(expected, signature_header)
