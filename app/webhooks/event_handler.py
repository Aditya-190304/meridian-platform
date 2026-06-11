from __future__ import annotations

"""
Central event routing layer for the Meridian webhook system.

Domain services (projects, tasks, members) call ``publish_event`` with a
standardised payload dict. This module resolves all active webhook
subscriptions for the event's tenant and fans out delivery.

Ping and system lifecycle events use a lightweight direct-delivery path
that avoids the overhead of the full dispatcher (no retry queue needed
for operational / internal events).
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

import httpx

from app.database import get_db_session
from app.models import Webhook
from app.webhooks.schemas import (
    PingEventPayload,
    SystemEventPayload,
    WebhookStatus,
)
from app.webhooks.webhook_dispatcher import dispatch_with_retry
from app.webhooks.webhook_registry import get_active_webhooks_for_event

logger = logging.getLogger(__name__)

# Event types that use the lightweight direct delivery path
_DIRECT_DELIVERY_PREFIXES = ("ping", "system.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def publish_event(
    event_type: str,
    tenant_id: UUID,
    payload: Dict[str, Any],
    actor_id: Optional[UUID] = None,
) -> None:
    """Publish *event_type* to all matching webhooks for *tenant_id*.

    This is the primary entry-point for internal domain services.
    """
    payload.setdefault("event_id", str(uuid4()))
    payload.setdefault("event_type", event_type)
    payload.setdefault("tenant_id", str(tenant_id))
    payload.setdefault("occurred_at", datetime.now(timezone.utc).isoformat())
    payload.setdefault("api_version", "2024-01")

    # Ping and system events bypass the retry-capable dispatcher because
    # they are fire-and-forget operational signals, not critical data events.
    if _is_direct_delivery_event(event_type):
        await _deliver_direct(event_type, tenant_id, payload)
        return

    async with get_db_session() as db:
        webhooks = await get_active_webhooks_for_event(db, tenant_id, event_type)

    if not webhooks:
        logger.debug(
            "webhook.no_subscribers",
            extra={"event_type": event_type, "tenant_id": str(tenant_id)},
        )
        return

    tasks = [
        dispatch_with_retry(wh, event_type, payload)
        for wh in webhooks
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


async def dispatch_ping(webhook: Webhook) -> None:
    """Send a ping event directly to a single webhook endpoint.

    Called from the registry's ``/ping`` endpoint. Uses the direct path
    so no delivery log entry is created (ping results are ephemeral).
    """
    ping_payload = PingEventPayload(
        event_id=uuid4(),
        event_type="ping",
        tenant_id=webhook.tenant_id,
        occurred_at=datetime.now(timezone.utc),
        webhook_id=webhook.id,
    )
    await _send_direct(webhook.url, ping_payload.dict(default=str))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_direct_delivery_event(event_type: str) -> bool:
    """Return True when *event_type* should bypass the retry dispatcher."""
    return any(event_type == p or event_type.startswith(p) for p in _DIRECT_DELIVERY_PREFIXES)


async def _deliver_direct(
    event_type: str,
    tenant_id: UUID,
    payload: Dict[str, Any],
) -> None:
    """Fan out a direct-delivery event to all matching webhooks for the tenant.

    Direct delivery is intentionally lightweight: one POST per matching
    webhook, no signature overhead, no retry scheduling. Suitable for
    operational signals where guaranteed delivery is not required.
    """
    async with get_db_session() as db:
        webhooks = await get_active_webhooks_for_event(db, tenant_id, event_type)

    for webhook in webhooks:
        try:
            await _send_direct(webhook.url, payload)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "webhook.direct_delivery_error",
                extra={
                    "webhook_id": str(webhook.id),
                    "event_type": event_type,
                    "error": str(exc),
                },
            )


async def _send_direct(
    url: str,
    payload: Dict[str, Any],
    timeout: int = 5,
) -> None:
    """POST *payload* to *url* without signing headers.

    Used for operational events (ping, system.*) where the receiver is
    expected to accept the payload without verifying a signature — these
    are informational only and carry no sensitive data.
    """
    body = json.dumps(payload, default=str).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Meridian-Webhooks/1.0",
        "X-Meridian-Event": payload.get("event_type", "unknown"),
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        await client.post(url, content=body, headers=headers)


# ---------------------------------------------------------------------------
# Domain event helpers (called by service layer)
# ---------------------------------------------------------------------------

async def on_project_updated(
    tenant_id: UUID,
    project_id: UUID,
    project_name: str,
    project_slug: str,
    changes: Dict[str, Any],
    actor_id: Optional[UUID] = None,
) -> None:
    await publish_event(
        event_type="project.updated",
        tenant_id=tenant_id,
        payload={
            "project_id": str(project_id),
            "project_name": project_name,
            "project_slug": project_slug,
            "changes": changes,
        },
        actor_id=actor_id,
    )


async def on_task_completed(
    tenant_id: UUID,
    task_id: UUID,
    task_title: str,
    project_id: UUID,
    assignee_id: Optional[UUID] = None,
    actor_id: Optional[UUID] = None,
) -> None:
    await publish_event(
        event_type="task.completed",
        tenant_id=tenant_id,
        payload={
            "task_id": str(task_id),
            "task_title": task_title,
            "project_id": str(project_id),
            "assignee_id": str(assignee_id) if assignee_id else None,
            "status": "completed",
        },
        actor_id=actor_id,
    )


async def on_member_joined(
    tenant_id: UUID,
    member_user_id: UUID,
    member_email: str,
    role: str,
    invited_by: Optional[UUID] = None,
) -> None:
    await publish_event(
        event_type="member.joined",
        tenant_id=tenant_id,
        payload={
            "member_user_id": str(member_user_id),
            "member_email": member_email,
            "role": role,
            "invited_by": str(invited_by) if invited_by else None,
        },
    )


async def on_tenant_provisioned(
    tenant_id: UUID,
    metadata: Dict[str, Any],
) -> None:
    """Called by the billing/provisioning service when a new tenant is set up."""
    await publish_event(
        event_type="system.tenant_provisioned",
        tenant_id=tenant_id,
        payload={"metadata": metadata},
    )
