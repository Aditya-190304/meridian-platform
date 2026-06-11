from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func, update

from app.auth.dependencies import get_current_tenant, require_role
from app.database import get_db
from app.models import Webhook, WebhookDeliveryLog
from app.webhooks.schemas import (
    DeliveryLogListResponse,
    WebhookCreateRequest,
    WebhookResponse,
    WebhookStatus,
    WebhookUpdateRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sign_payload(secret: str, body: bytes) -> str:
    """Return a hex HMAC-SHA256 signature for *body* using *secret*.

    The signature is prefixed with ``sha256=`` to follow the convention
    used by GitHub, Stripe, and similar platforms, making it easy for
    subscribers to verify with standard libraries.
    """
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _matches_event_filter(subscription_patterns: List[str], event_type: str) -> bool:
    """Return True if *event_type* matches any of the subscription patterns.

    Patterns support a single trailing wildcard, e.g. ``task.*`` matches
    ``task.created`` and ``task.updated`` but not ``project.created``.
    The special pattern ``*`` matches every event type.
    """
    for pattern in subscription_patterns:
        if pattern == "*":
            return True
        if pattern.endswith(".*"):
            prefix = pattern[:-2]
            if event_type == prefix or event_type.startswith(f"{prefix}."):
                return True
        if pattern == event_type:
            return True
    return False


# ---------------------------------------------------------------------------
# CRUD endpoints
# ---------------------------------------------------------------------------

@router.post("", response_model=WebhookResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    payload: WebhookCreateRequest,
    db: AsyncSession = Depends(get_db),
    tenant=Depends(get_current_tenant),
    _=Depends(require_role("admin", "owner")),
):
    """Register a new webhook endpoint for the current tenant."""
    # Enforce per-tenant webhook cap
    count_result = await db.execute(
        select(func.count()).where(Webhook.tenant_id == tenant.id)
    )
    existing_count = count_result.scalar_one()
    if existing_count >= 25:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tenant has reached the maximum of 25 webhook subscriptions.",
        )

    webhook = Webhook(
        id=uuid4(),
        tenant_id=tenant.id,
        url=str(payload.url),
        # Store the secret hashed; raw secret is only used at signing time
        secret_hash=hashlib.sha256(payload.secret.encode()).hexdigest(),
        # We keep the raw secret encrypted at rest via the ORM field type
        secret=payload.secret,
        events=payload.events,
        description=payload.description,
        status=WebhookStatus.active if payload.active else WebhookStatus.disabled,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(webhook)
    await db.commit()
    await db.refresh(webhook)

    logger.info(
        "webhook.created",
        extra={"webhook_id": str(webhook.id), "tenant_id": str(tenant.id)},
    )
    return webhook


@router.get("", response_model=List[WebhookResponse])
async def list_webhooks(
    db: AsyncSession = Depends(get_db),
    tenant=Depends(get_current_tenant),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List all webhook subscriptions for the current tenant."""
    offset = (page - 1) * page_size
    result = await db.execute(
        select(Webhook)
        .where(Webhook.tenant_id == tenant.id)
        .order_by(Webhook.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    return result.scalars().all()


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(
    webhook_id: UUID,
    db: AsyncSession = Depends(get_db),
    tenant=Depends(get_current_tenant),
):
    webhook = await _get_webhook_or_404(db, webhook_id, tenant.id)
    return webhook


@router.patch("/{webhook_id}", response_model=WebhookResponse)
async def update_webhook(
    webhook_id: UUID,
    payload: WebhookUpdateRequest,
    db: AsyncSession = Depends(get_db),
    tenant=Depends(get_current_tenant),
    _=Depends(require_role("admin", "owner")),
):
    webhook = await _get_webhook_or_404(db, webhook_id, tenant.id)

    update_data: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}
    if payload.url is not None:
        update_data["url"] = str(payload.url)
    if payload.secret is not None:
        update_data["secret"] = payload.secret
        update_data["secret_hash"] = hashlib.sha256(payload.secret.encode()).hexdigest()
    if payload.events is not None:
        update_data["events"] = payload.events
    if payload.description is not None:
        update_data["description"] = payload.description
    if payload.active is not None:
        update_data["status"] = (
            WebhookStatus.active if payload.active else WebhookStatus.disabled
        )

    await db.execute(
        update(Webhook)
        .where(Webhook.id == webhook_id)
        .values(**update_data)
    )
    await db.commit()
    await db.refresh(webhook)
    return webhook


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    webhook_id: UUID,
    db: AsyncSession = Depends(get_db),
    tenant=Depends(get_current_tenant),
    _=Depends(require_role("admin", "owner")),
):
    webhook = await _get_webhook_or_404(db, webhook_id, tenant.id)
    await db.delete(webhook)
    await db.commit()


@router.post("/{webhook_id}/ping", status_code=status.HTTP_202_ACCEPTED)
async def ping_webhook(
    webhook_id: UUID,
    db: AsyncSession = Depends(get_db),
    tenant=Depends(get_current_tenant),
):
    """Send a test ping event to verify the endpoint is reachable."""
    webhook = await _get_webhook_or_404(db, webhook_id, tenant.id)
    # Enqueue a ping via the event handler (fast path, no DB writes needed)
    from app.webhooks.event_handler import dispatch_ping
    await dispatch_ping(webhook)
    return {"queued": True, "webhook_id": str(webhook_id)}


@router.get("/{webhook_id}/deliveries", response_model=DeliveryLogListResponse)
async def list_deliveries(
    webhook_id: UUID,
    db: AsyncSession = Depends(get_db),
    tenant=Depends(get_current_tenant),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
):
    """Return paginated delivery attempt history for a webhook."""
    await _get_webhook_or_404(db, webhook_id, tenant.id)
    offset = (page - 1) * page_size
    result = await db.execute(
        select(WebhookDeliveryLog)
        .where(WebhookDeliveryLog.webhook_id == webhook_id)
        .order_by(WebhookDeliveryLog.created_at.desc())
        .offset(offset)
        .limit(page_size)
    )
    items = result.scalars().all()
    total_result = await db.execute(
        select(func.count()).where(WebhookDeliveryLog.webhook_id == webhook_id)
    )
    total = total_result.scalar_one()
    return DeliveryLogListResponse(
        items=items, total=total, page=page, page_size=page_size
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get_webhook_or_404(
    db: AsyncSession, webhook_id: UUID, tenant_id: UUID
) -> Webhook:
    result = await db.execute(
        select(Webhook).where(
            Webhook.id == webhook_id, Webhook.tenant_id == tenant_id
        )
    )
    webhook = result.scalar_one_or_none()
    if webhook is None:
        raise HTTPException(status_code=404, detail="Webhook not found.")
    return webhook


async def get_active_webhooks_for_event(
    db: AsyncSession, tenant_id: UUID, event_type: str
) -> List[Webhook]:
    """Return all active webhooks for *tenant_id* that subscribe to *event_type*."""
    result = await db.execute(
        select(Webhook).where(
            Webhook.tenant_id == tenant_id,
            Webhook.status == WebhookStatus.active,
        )
    )
    all_webhooks = result.scalars().all()
    return [
        wh
        for wh in all_webhooks
        if _matches_event_filter(wh.events, event_type)
    ]
