"""Notification dispatch service for Meridian Platform.

Handles multi-channel delivery, template rendering, delivery tracking,
and retry scheduling across email, SMS, and Slack providers.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from jinja2 import Environment, StrictUndefined, TemplateNotFound, UndefinedError
from sqlalchemy.orm import Session

from app.notifications.adapters import EmailAdapter, SlackAdapter, SmsAdapter
from app.notifications.config import NotificationSettings
from app.notifications.schemas import (
    ChannelType,
    DeliveryStatus,
    NotificationCreate,
    NotificationRecord,
    RetryPolicy,
    TemplateRecord,
)

logger = logging.getLogger(__name__)

DEFAULT_RETRY_POLICY = RetryPolicy(
    max_attempts=4,
    base_delay_seconds=30,
    backoff_multiplier=2.5,
    max_delay_seconds=600,
)


class TemplateRenderer:
    """Renders Jinja2 notification templates with tenant variable context."""

    def __init__(self) -> None:
        self._env = Environment(
            undefined=StrictUndefined,
            autoescape=True,
        )

    def render(self, template_body: str, context: Dict[str, Any]) -> str:
        try:
            tmpl = self._env.from_string(template_body)
            return tmpl.render(**context)
        except UndefinedError as exc:
            raise ValueError(f"Template variable missing: {exc}") from exc

    def render_subject(self, subject_template: str, context: Dict[str, Any]) -> str:
        try:
            tmpl = self._env.from_string(subject_template)
            return tmpl.render(**context)
        except UndefinedError:
            return subject_template


class DeliveryTracker:
    """Persists and updates notification delivery lifecycle events."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def create_record(
        self,
        notification_id: str,
        tenant_id: str,
        channel: ChannelType,
        recipient: str,
        template_id: Optional[str],
        payload_hash: str,
    ) -> NotificationRecord:
        record = NotificationRecord(
            id=notification_id,
            tenant_id=tenant_id,
            channel=channel,
            recipient=recipient,
            template_id=template_id,
            payload_hash=payload_hash,
            status=DeliveryStatus.QUEUED,
            attempt_count=0,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        self._db.add(record)
        self._db.commit()
        return record

    def mark_dispatched(self, notification_id: str, provider_message_id: str) -> None:
        record = self._db.query(NotificationRecord).get(notification_id)
        if record:
            record.status = DeliveryStatus.DISPATCHED
            record.provider_message_id = provider_message_id
            record.dispatched_at = datetime.utcnow()
            record.updated_at = datetime.utcnow()
            record.attempt_count += 1
            self._db.commit()

    def mark_delivered(self, notification_id: str) -> None:
        record = self._db.query(NotificationRecord).get(notification_id)
        if record:
            record.status = DeliveryStatus.DELIVERED
            record.delivered_at = datetime.utcnow()
            record.updated_at = datetime.utcnow()
            self._db.commit()

    def mark_failed(
        self,
        notification_id: str,
        error_message: str,
        permanent: bool = False,
    ) -> None:
        record = self._db.query(NotificationRecord).get(notification_id)
        if record:
            record.status = DeliveryStatus.FAILED if permanent else DeliveryStatus.RETRYING
            record.last_error = error_message[:1024]
            record.attempt_count += 1
            record.updated_at = datetime.utcnow()
            self._db.commit()

    def schedule_retry(
        self,
        notification_id: str,
        attempt: int,
        policy: RetryPolicy,
    ) -> Optional[datetime]:
        delay = min(
            policy.base_delay_seconds * (policy.backoff_multiplier ** (attempt - 1)),
            policy.max_delay_seconds,
        )
        next_attempt_at = datetime.utcnow() + timedelta(seconds=delay)
        record = self._db.query(NotificationRecord).get(notification_id)
        if record:
            record.next_attempt_at = next_attempt_at
            self._db.commit()
        return next_attempt_at

    def get_record(self, notification_id: str) -> Optional[NotificationRecord]:
        return self._db.query(NotificationRecord).get(notification_id)


class NotificationService:
    """Orchestrates template rendering, provider dispatch, and delivery tracking."""

    def __init__(
        self,
        db: Session,
        settings: NotificationSettings,
    ) -> None:
        self._db = db
        self._settings = settings
        self._renderer = TemplateRenderer()
        self._tracker = DeliveryTracker(db)
        self._email = EmailAdapter(settings)
        self._sms = SmsAdapter(settings)
        self._slack = SlackAdapter(settings)

    def _resolve_template(
        self, tenant_id: str, template_id: str
    ) -> Optional[TemplateRecord]:
        return (
            self._db.query(TemplateRecord)
            .filter(
                TemplateRecord.tenant_id == tenant_id,
                TemplateRecord.id == template_id,
                TemplateRecord.active == True,  # noqa: E712
            )
            .first()
        )

    def _hash_payload(self, payload: Dict[str, Any]) -> str:
        import hashlib, json
        serialized = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode()).hexdigest()[:16]

    def send(
        self,
        notification: NotificationCreate,
        retry_policy: Optional[RetryPolicy] = None,
    ) -> str:
        policy = retry_policy or DEFAULT_RETRY_POLICY
        notification_id = str(uuid.uuid4())
        payload_hash = self._hash_payload(notification.context or {})

        record = self._tracker.create_record(
            notification_id=notification_id,
            tenant_id=notification.tenant_id,
            channel=notification.channel,
            recipient=notification.recipient,
            template_id=notification.template_id,
            payload_hash=payload_hash,
        )

        body: str
        subject: Optional[str] = None

        if notification.template_id:
            template = self._resolve_template(notification.tenant_id, notification.template_id)
            if template is None:
                raise ValueError(
                    f"Template {notification.template_id!r} not found for tenant {notification.tenant_id!r}"
                )
            body = self._renderer.render(template.body, notification.context or {})
            if template.subject:
                subject = self._renderer.render_subject(template.subject, notification.context or {})
        else:
            body = notification.body or ""
            subject = notification.subject

        try:
            provider_message_id = self._dispatch(
                channel=notification.channel,
                recipient=notification.recipient,
                body=body,
                subject=subject,
                tenant_id=notification.tenant_id,
            )
            self._tracker.mark_dispatched(notification_id, provider_message_id)
            logger.info(
                "Notification dispatched",
                extra={"notification_id": notification_id, "channel": notification.channel},
            )
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "Dispatch failed, scheduling retry",
                extra={"notification_id": notification_id, "error": str(exc)},
            )
            self._tracker.mark_failed(notification_id, str(exc), permanent=False)
            next_at = self._tracker.schedule_retry(notification_id, attempt=1, policy=policy)
            logger.info("Retry scheduled for %s at %s", notification_id, next_at)

        return notification_id

    def _dispatch(
        self,
        channel: ChannelType,
        recipient: str,
        body: str,
        subject: Optional[str],
        tenant_id: str,
    ) -> str:
        if channel == ChannelType.EMAIL:
            return self._email.send(to=recipient, subject=subject or "Meridian Notification", body=body)
        elif channel == ChannelType.SMS:
            return self._sms.send(to=recipient, body=body)
        elif channel == ChannelType.SLACK:
            return self._slack.post(webhook_url=recipient, text=body)
        else:
            raise ValueError(f"Unsupported channel: {channel}")

    def retry_pending(self) -> List[str]:
        """Process all notifications scheduled for retry. Called by the background worker."""
        now = datetime.utcnow()
        pending = (
            self._db.query(NotificationRecord)
            .filter(
                NotificationRecord.status == DeliveryStatus.RETRYING,
                NotificationRecord.next_attempt_at <= now,
                NotificationRecord.attempt_count < DEFAULT_RETRY_POLICY.max_attempts,
            )
            .all()
        )
        retried: List[str] = []
        for record in pending:
            try:
                provider_message_id = self._dispatch(
                    channel=record.channel,
                    recipient=record.recipient,
                    body="[retry]",  # re-render from stored payload in production
                    subject=None,
                    tenant_id=record.tenant_id,
                )
                self._tracker.mark_dispatched(record.id, provider_message_id)
                retried.append(record.id)
            except Exception as exc:  # pylint: disable=broad-except
                is_permanent = record.attempt_count + 1 >= DEFAULT_RETRY_POLICY.max_attempts
                self._tracker.mark_failed(record.id, str(exc), permanent=is_permanent)
                if not is_permanent:
                    self._tracker.schedule_retry(
                        record.id,
                        attempt=record.attempt_count + 1,
                        policy=DEFAULT_RETRY_POLICY,
                    )
        return retried

    def get_status(self, notification_id: str, tenant_id: str) -> Optional[NotificationRecord]:
        record = self._tracker.get_record(notification_id)
        if record and record.tenant_id == tenant_id:
            return record
        return None
