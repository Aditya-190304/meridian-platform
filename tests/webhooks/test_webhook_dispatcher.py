from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.webhooks.webhook_dispatcher import (
    MAX_ATTEMPTS,
    RETRY_DELAYS,
    build_signed_headers,
    deliver_event,
    verify_signature,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_webhook():
    wh = MagicMock()
    wh.id = uuid4()
    wh.tenant_id = uuid4()
    wh.url = "https://example.com/hook"
    wh.secret = "super-secret-key-for-testing-1234"
    wh.events = ["*"]
    return wh


@pytest.fixture()
def sample_payload() -> Dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "event_type": "task.completed",
        "tenant_id": str(uuid4()),
        "occurred_at": "2024-03-15T12:00:00Z",
        "task_id": str(uuid4()),
        "task_title": "Implement OAuth flow",
        "project_id": str(uuid4()),
        "status": "completed",
    }


# ---------------------------------------------------------------------------
# Signing tests
# ---------------------------------------------------------------------------

class TestBuildSignedHeaders:
    def test_returns_required_headers(self, fake_webhook, sample_payload):
        body = json.dumps(sample_payload).encode()
        headers = build_signed_headers(fake_webhook.secret, body, "task.completed")

        assert "X-Meridian-Signature-256" in headers
        assert "X-Meridian-Timestamp" in headers
        assert "X-Meridian-Delivery" in headers
        assert headers["X-Meridian-Event"] == "task.completed"
        assert headers["Content-Type"] == "application/json"

    def test_signature_has_sha256_prefix(self, fake_webhook, sample_payload):
        body = json.dumps(sample_payload).encode()
        headers = build_signed_headers(fake_webhook.secret, body, "task.completed")
        assert headers["X-Meridian-Signature-256"].startswith("sha256=")

    def test_different_secrets_produce_different_signatures(self, sample_payload):
        body = json.dumps(sample_payload).encode()
        h1 = build_signed_headers("secret-aaa-1111111111111111", body, "task.created")
        h2 = build_signed_headers("secret-bbb-2222222222222222", body, "task.created")
        assert h1["X-Meridian-Signature-256"] != h2["X-Meridian-Signature-256"]

    def test_different_bodies_produce_different_signatures(self, fake_webhook):
        body1 = b'{"event_type": "task.created"}'
        body2 = b'{"event_type": "task.deleted"}'
        h1 = build_signed_headers(fake_webhook.secret, body1, "task.created")
        h2 = build_signed_headers(fake_webhook.secret, body2, "task.deleted")
        assert h1["X-Meridian-Signature-256"] != h2["X-Meridian-Signature-256"]


class TestVerifySignature:
    def _make_signature(self, secret: str, timestamp: str, body: bytes) -> str:
        signed = f"{timestamp}.{body.decode()}"
        mac = hmac.new(secret.encode(), signed.encode(), hashlib.sha256)
        return f"sha256={mac.hexdigest()}"

    def test_valid_signature_passes(self, fake_webhook):
        body = b'{"event_type": "task.created"}'
        ts = str(int(time.time()))
        sig = self._make_signature(fake_webhook.secret, ts, body)
        assert verify_signature(fake_webhook.secret, body, ts, sig) is True

    def test_wrong_secret_fails(self, fake_webhook):
        body = b'{"event_type": "task.created"}'
        ts = str(int(time.time()))
        sig = self._make_signature("wrong-secret-value-00000000000", ts, body)
        assert verify_signature(fake_webhook.secret, body, ts, sig) is False

    def test_tampered_body_fails(self, fake_webhook):
        original_body = b'{"event_type": "task.created"}'
        tampered_body = b'{"event_type": "task.deleted"}'
        ts = str(int(time.time()))
        sig = self._make_signature(fake_webhook.secret, ts, original_body)
        assert verify_signature(fake_webhook.secret, tampered_body, ts, sig) is False

    def test_expired_timestamp_fails(self, fake_webhook):
        body = b'{"event_type": "task.created"}'
        old_ts = str(int(time.time()) - 400)  # 400 seconds ago
        sig = self._make_signature(fake_webhook.secret, old_ts, body)
        assert verify_signature(fake_webhook.secret, body, old_ts, sig) is False

    def test_invalid_timestamp_fails(self, fake_webhook):
        body = b'{"event_type": "task.created"}'
        assert verify_signature(fake_webhook.secret, body, "not-a-number", "sha256=abc") is False


# ---------------------------------------------------------------------------
# Delivery tests
# ---------------------------------------------------------------------------

class TestDeliverEvent:
    @pytest.mark.asyncio()
    async def test_successful_delivery_returns_true(self, fake_webhook, sample_payload):
        mock_response = MagicMock()
        mock_response.status_code = 200

        with (
            patch("app.webhooks.webhook_dispatcher.httpx.AsyncClient") as mock_client_cls,
            patch("app.webhooks.webhook_dispatcher.get_db_session") as mock_db_ctx,
        ):
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_db = AsyncMock()
            mock_db_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await deliver_event(fake_webhook, "task.completed", sample_payload)

        assert result is True

    @pytest.mark.asyncio()
    async def test_non_2xx_response_returns_false(self, fake_webhook, sample_payload):
        mock_response = MagicMock()
        mock_response.status_code = 500

        with (
            patch("app.webhooks.webhook_dispatcher.httpx.AsyncClient") as mock_client_cls,
            patch("app.webhooks.webhook_dispatcher.get_db_session") as mock_db_ctx,
        ):
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_db = AsyncMock()
            mock_db_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await deliver_event(fake_webhook, "task.completed", sample_payload)

        assert result is False

    @pytest.mark.asyncio()
    async def test_max_attempts_suspends_webhook(self, fake_webhook, sample_payload):
        mock_response = MagicMock()
        mock_response.status_code = 503

        with (
            patch("app.webhooks.webhook_dispatcher.httpx.AsyncClient") as mock_client_cls,
            patch("app.webhooks.webhook_dispatcher.get_db_session") as mock_db_ctx,
        ):
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_db = AsyncMock()
            mock_db_ctx.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            mock_db_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await deliver_event(
                fake_webhook, "task.completed", sample_payload, attempt_number=MAX_ATTEMPTS
            )

        assert result is False
        mock_db.execute.assert_called()


# ---------------------------------------------------------------------------
# Retry schedule tests
# ---------------------------------------------------------------------------

class TestRetrySchedule:
    def test_retry_delays_count_matches_max_attempts(self):
        assert len(RETRY_DELAYS) == MAX_ATTEMPTS - 1

    def test_retry_delays_are_increasing(self):
        for i in range(len(RETRY_DELAYS) - 1):
            assert RETRY_DELAYS[i] < RETRY_DELAYS[i + 1]
