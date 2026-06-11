"""Celery tasks for workspace data export.

Exports are always run asynchronously so the HTTP request returns immediately
with an export ID that the client can poll.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from celery import Task as CeleryTask

from app.celery_app import celery
from app.db.session import SessionLocal
from app.models.export_record import ExportRecord, ExportStatus
from app.services.export_service import WorkspaceExportService
from app.storage import upload_export_artifact

logger = logging.getLogger(__name__)

# Maximum wall-clock seconds before Celery hard-kills the task.
# Set high because large workspaces can take a while to serialize.
EXPORT_TASK_SOFT_TIMEOUT = 1500
EXPORT_TASK_HARD_TIMEOUT = 1800


class ExportBaseTask(CeleryTask):
    """Base class that opens and tears down a DB session per task invocation."""

    abstract = True
    _db = None

    def after_return(self, *args, **kwargs) -> None:  # noqa: ANN002
        if self._db is not None:
            self._db.close()
            self._db = None

    @property
    def db(self):
        if self._db is None:
            self._db = SessionLocal()
        return self._db


@celery.task(
    bind=True,
    base=ExportBaseTask,
    name="meridian.export.run_workspace_export",
    soft_time_limit=EXPORT_TASK_SOFT_TIMEOUT,
    time_limit=EXPORT_TASK_HARD_TIMEOUT,
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
    reject_on_worker_lost=True,
)
def run_workspace_export(self, export_id: str) -> dict:
    """Execute a workspace export and persist the resulting ZIP.

    Args:
        export_id: UUID string of the ExportRecord to process.

    Returns:
        A small summary dict (logged by Celery result backend).
    """
    db = self.db
    record: ExportRecord | None = (
        db.query(ExportRecord).filter(ExportRecord.id == UUID(export_id)).one_or_none()
    )

    if record is None:
        logger.error("ExportRecord %s not found — aborting task", export_id)
        return {"status": "not_found", "export_id": export_id}

    if record.status not in (ExportStatus.PENDING, ExportStatus.QUEUED):
        logger.warning(
            "ExportRecord %s already in status %s — skipping",
            export_id,
            record.status,
        )
        return {"status": record.status, "export_id": export_id}

    record.status = ExportStatus.IN_PROGRESS
    record.started_at = datetime.now(timezone.utc)
    db.commit()

    try:
        service = WorkspaceExportService(db=db, export_record=record)
        zip_bytes = service.run()

        # Upload to object storage so the download endpoint can stream it.
        storage_key = upload_export_artifact(
            workspace_id=str(record.workspace_id),
            export_id=export_id,
            data=zip_bytes,
        )

        record.status = ExportStatus.COMPLETE
        record.completed_at = datetime.now(timezone.utc)
        record.storage_key = storage_key
        record.file_size_bytes = len(zip_bytes)
        db.commit()

        logger.info(
            "Export %s complete — %d bytes stored at %s",
            export_id,
            len(zip_bytes),
            storage_key,
        )
        return {
            "status": "complete",
            "export_id": export_id,
            "file_size_bytes": len(zip_bytes),
        }

    except MemoryError:
        # Shouldn't happen for typical workspaces but log clearly if it does.
        logger.exception(
            "MemoryError during export %s — workspace may be too large", export_id
        )
        _mark_failed(db, record, "Export exceeded available worker memory.")
        raise  # let Celery retry

    except Exception as exc:
        logger.exception("Unexpected error during export %s", export_id)
        _mark_failed(db, record, str(exc))
        raise self.retry(exc=exc)


def _mark_failed(db, record: ExportRecord, reason: str) -> None:
    record.status = ExportStatus.FAILED
    record.completed_at = datetime.now(timezone.utc)
    record.error_message = reason[:1024]
    try:
        db.commit()
    except Exception:
        logger.exception("Failed to persist export failure status for %s", record.id)
