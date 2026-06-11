from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from celery import shared_task
from celery.utils.log import get_task_logger

from app.core.redis_client import get_redis
from app.db.session import SessionLocal
from app.repositories.project_repository import ProjectRepository
from app.repositories.task_repository import TaskRepository
from app.schemas.export_schemas import ExportFormat, ExportStatus
from app.services.export_service import ExportService, ExportServiceError

logger = get_task_logger(__name__)

# Redis key helpers
_JOB_KEY = "meridian:export:job:{job_id}"
_JOB_TTL = 60 * 60 * 24  # 24 hours


def _job_key(job_id: str) -> str:
    return _JOB_KEY.format(job_id=job_id)


def _set_job_status(
    redis_client,
    job_id: str,
    status: ExportStatus,
    progress: int = 0,
    error_message: Optional[str] = None,
    s3_key: Optional[str] = None,
) -> None:
    """Persist job progress metadata to Redis."""
    mapping: Dict[str, str] = {
        "status": status.value if hasattr(status, 'value') else status,
        "progress": str(progress),
        "updated_at": datetime.utcnow().isoformat(),
    }
    if error_message:
        mapping["error_message"] = error_message
    if s3_key:
        mapping["s3_key"] = s3_key

    key = _job_key(job_id)
    redis_client.hset(key, mapping=mapping)
    redis_client.expire(key, _JOB_TTL)


@shared_task(
    bind=True,
    name="meridian.tasks.export_report",
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
    reject_on_worker_lost=True,
)
def export_report_task(
    self,
    *,
    job_id: str,
    project_id: str,
    tenant_id: str,
    export_format: str,
    report_title: str,
    template_name: str,
    include_closed_tasks: bool,
    date_range_start: Optional[str],
    date_range_end: Optional[str],
    requested_by_user_id: str,
) -> Dict[str, Any]:
    """
    Celery task: generate a project report export and upload it to S3.

    Progress stages:
      0  -> queued / picked up
      10 -> fetching project data
      30 -> fetching tasks
      50 -> generating file
      80 -> uploading to S3
     100 -> done
    """
    redis = get_redis()

    logger.info(
        "[%s] Starting export job for project=%s format=%s",
        job_id, project_id, export_format,
    )

    _set_job_status(redis, job_id, ExportStatus.processing, progress=0)

    db = SessionLocal()
    try:
        # --- Stage 1: load project -----------------------------------------
        _set_job_status(redis, job_id, ExportStatus.processing, progress=10)
        project_repo = ProjectRepository(db)
        project = project_repo.get_by_id_and_tenant(
            project_id=project_id, tenant_id=tenant_id
        )
        if project is None:
            raise ExportServiceError(f"Project {project_id} not found in tenant {tenant_id}")

        project_data = {
            "id": str(project.id),
            "name": project.name,
            "description": project.description or "",
            "owner_email": project.owner.email if project.owner else "",
            "created_at": project.created_at.isoformat(),
        }

        # --- Stage 2: load tasks -------------------------------------------
        _set_job_status(redis, job_id, ExportStatus.processing, progress=30)
        task_repo = TaskRepository(db)

        filters: Dict[str, Any] = {"project_id": project_id, "tenant_id": tenant_id}
        if not include_closed_tasks:
            filters["exclude_statuses"] = ["closed", "cancelled"]
        if date_range_start:
            filters["created_after"] = date_range_start
        if date_range_end:
            filters["created_before"] = date_range_end

        raw_tasks = task_repo.list_for_export(**filters)
        tasks: List[Dict[str, Any]] = [
            {
                "id": str(t.id),
                "title": t.title,
                "assignee_email": t.assignee.email if t.assignee else "",
                "status": t.status,
                "priority": t.priority,
                "due_date": t.due_date.isoformat() if t.due_date else "",
                "created_at": t.created_at.isoformat(),
                "updated_at": t.updated_at.isoformat(),
                "tags": [tag.name for tag in (t.tags or [])],
            }
            for t in raw_tasks
        ]

        logger.info("[%s] Loaded %d tasks for export", job_id, len(tasks))

        # --- Stage 3: generate file ----------------------------------------
        _set_job_status(redis, job_id, ExportStatus.processing, progress=50)
        svc = ExportService()
        s3_key = svc.generate_report(
            project_data=project_data,
            tasks=tasks,
            export_format=ExportFormat(export_format),
            report_title=report_title,
            template_name=template_name,
            job_id=job_id,
        )

        # --- Stage 4: mark complete ----------------------------------------
        _set_job_status(
            redis, job_id, ExportStatus.completed, progress=100, s3_key=s3_key
        )
        logger.info("[%s] Export completed — s3_key=%s", job_id, s3_key)

        return {"job_id": job_id, "s3_key": s3_key, "task_count": len(tasks)}

    except ExportServiceError as exc:
        logger.error("[%s] Export service error: %s", job_id, exc)
        _set_job_status(
            redis, job_id, ExportStatus.failed, error_message=str(exc)
        )
        # Don't retry service-level errors (bad template, wkhtmltopdf failure, etc.)
        raise

    except Exception as exc:
        logger.exception("[%s] Unexpected error during export: %s", job_id, exc)
        _set_job_status(
            redis, job_id, ExportStatus.failed,
            error_message="An unexpected error occurred. Please try again.",
        )
        # Retry transient errors (DB blips, S3 timeouts)
        raise self.retry(exc=exc)

    finally:
        db.close()
