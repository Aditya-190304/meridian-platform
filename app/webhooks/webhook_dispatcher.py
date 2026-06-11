from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import update

from app.database import get_db_session
from app.models import Webhook, WebhookDeliveryLog
from app.webhooks.schemas import DeliveryStatus, WebhookStatus

logger = logging.getLogger(__name__)

# Retry schedule: delays in seconds between successive attempts
RETRY_DELAYS: list[int] = [30, 300, 1800, 7200, 28800]  # 30s, 5m, 30m, 2h, 8h
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1  # initial attempt + retries
DELIVERY_TIMEOUT_SECONDS = 10


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def build_signed_headers(secret: str, body: bytes, event_type: str) -> Dict[str, str]:
    """Construct the HTTP headers for a signed webhook delivery.

    The ``X-Meridian-Signature-256`` header contains the HMAC-SHA256
    signature of the raw request body, prefixed with ``sha256=``.
    Subscribers should verify this header before processing the payload.
    """
    timestamp = str(int(time.time()))
    # Include timestamp in the signed content to prevent replay attacks
    signed_content = f"{timestamp}.{body.decode('utf-8', errors='replace')}"
    mac = hmac.new(
        secret.encode(),
        signed_content.encode(),
        hashlib.sha256,
    )
    signature = f"sha256={mac.hexdigest()}"
    return {
        "Content-Type": "application/json",
        "User-Agent": "Meridian-Webhooks/1.0",
        "X-Meridian-Event": event_type,
        "X-Meridian-Delivery": str(uuid4()),
        "X-Meridian-Signature-256": signature,
        "X-Meridian-Timestamp": timestamp,
    }


def verify_signature(
    secret: str,
    body: bytes,
    timestamp: str,
    received_signature: str,
) -> bool:
    """Verify an incoming webhook signature.

    Returns True only when the computed HMAC matches *received_signature*
    and the timestamp is within the acceptable drift window (5 minutes).
    """
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        return False

    # Reject requests with a timestamp more than 5 minutes old
    now = int(time.time())
    if abs(now - ts) > 300:
        logger.warning("webhook.signature_expired", extra={"timestamp": timestamp})
        return False

    signed_content = f"{timestamp}.{body.decode('utf-8', errors='replace')}"
    mac = hmac.new(
        secret.encode(),
        signed_content.encode(),
        hashlib.sha256,
    )
    expected = f"sha256={mac.hexdigest()}"
    return hmac.compare_digest(expected, received_signature)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

async def deliver_event(
    webhook: Webhook,
    event_type: str,
    payload: Dict[str, Any],
    attempt_number: int = 1,
) -> bool:
    """POST *payload* to *webhook.url* with the correct signing headers.

    Returns True on a successful delivery (2xx response), False otherwise.
    Logs every attempt to ``webhook_delivery_logs``.
    """
    body = json.dumps(payload, default=str).encode()
    headers = build_signed_headers(webhook.secret, body, event_type)

    log_id = uuid4()
    started_at = time.monotonic()
    http_status: Optional[int] = None
    success = False

    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_SECONDS) as client:
            response = await client.post(str(webhook.url), content=body, headers=headers)
        http_status = response.status_code
        success = 200 <= http_status < 300
        if not success:
            logger.warning(
                "webhook.delivery_failed",
                extra={
                    "webhook_id": str(webhook.id),
                    "event_type": event_type,
                    "attempt": attempt_number,
                    "http_status": http_status,
                },
            )
    except httpx.TimeoutException:
        logger.warning(
            "webhook.delivery_timeout",
            extra={"webhook_id": str(webhook.id), "attempt": attempt_number},
        )
    except httpx.RequestError as exc:
        logger.error(
            "webhook.delivery_error",
            extra={"webhook_id": str(webhook.id), "error": str(exc)},
        )

    latency_ms = int((time.monotonic() - started_at) * 1000)

    async with get_db_session() as db:
        await _write_delivery_log(
            db=db,
            log_id=log_id,
            webhook_id=webhook.id,
            event_type=event_type,
            attempt_number=attempt_number,
            status=DeliveryStatus.delivered if success else DeliveryStatus.failed,
            http_status_code=http_status,
            latency_ms=latency_ms,
        )

        if success:
            await db.execute(
                update(Webhook)
                .where(Webhook.id == webhook.id)
                .values(last_triggered_at=datetime.now(timezone.utc))
            )
        elif attempt_number >= MAX_ATTEMPTS:
            logger.error(
                "webhook.max_retries_exceeded",
                extra={"webhook_id": str(webhook.id)},
            )
            await db.execute(
                update(Webhook)
                .where(Webhook.id == webhook.id)
                .values(status=WebhookStatus.suspended)
            )

        await db.commit()

    return success


async def schedule_retry(
    webhook: Webhook,
    event_type: str,
    payload: Dict[str, Any],
    attempt_number: int,
) -> None:
    """Schedule the next retry attempt with exponential backoff.

    If the maximum number of attempts has been reached this function is
    a no-op; the webhook has already been suspended by *deliver_event*.
    """
    if attempt_number >= MAX_ATTEMPTS:
        return

    delay = RETRY_DELAYS[attempt_number - 1]
    logger.info(
        "webhook.retry_scheduled",
        extra={
            "webhook_id": str(webhook.id),
            "next_attempt": attempt_number + 1,
            "delay_seconds": delay,
        },
    )
    # In production this would push to a task queue (Celery / ARQ / SQS).
    # For the async path we simulate with asyncio.sleep for integration tests.
    await asyncio.sleep(delay)
    await deliver_event(webhook, event_type, payload, attempt_number + 1)


async def dispatch_with_retry(
    webhook: Webhook,
    event_type: str,
    payload: Dict[str, Any],
) -> None:
    """Attempt delivery and schedule retries on failure."""
    success = await deliver_event(webhook, event_type, payload, attempt_number=1)
    if not success:
        asyncio.create_task(
            schedule_retry(webhook, event_type, payload, attempt_number=1)
        )


# ---------------------------------------------------------------------------
# Internal DB helpers
# ---------------------------------------------------------------------------

async def _write_delivery_log(
    db: AsyncSession,
    log_id: UUID,
    webhook_id: UUID,
    event_type: str,
    attempt_number: int,
    status: DeliveryStatus,
    http_status_code: Optional[int],
    latency_ms: Optional[int],
    next_retry_at: Optional[datetime] = None,
) -> None:
    log_entry = WebhookDeliveryLog(
        id=log_id,
        webhook_id=webhook_id,
        event_type=event_type,
        attempt_number=attempt_number,
        status=status,
        http_status_code=http_status_code,
        response_latency_ms=latency_ms,
        next_retry_at=next_retry_at,
        created_at=datetime.now(timezone.utc),
    )
    db.add(log_entry)
