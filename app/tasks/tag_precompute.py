"""Celery background tasks for pre-computing tag analytics.

These tasks are registered on the beat schedule so that analytics data is
always warm in Redis before a user request hits it.  They also run after
bulk operations (imports, migrations) that would otherwise stale the cache
for an entire workspace at once.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from celery.utils.log import get_task_logger
from sqlalchemy.exc import OperationalError

from app.core.celery_app import celery_app
from app.core.db import SessionLocal
from app.models.workspace import Workspace
from app.services.tag_analytics import compute_workspace_tag_stats

logger = get_task_logger(__name__)

# How long before the cached stats expire do we consider them "stale enough"
# to bother re-running the pre-compute.  Set to 5 minutes so we don't hammer
# the DB if the beat fires slightly early.
_PRECOMPUTE_STALE_THRESHOLD_SECONDS = 300


@shared_task(
    name="tag_analytics.precompute_workspace",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    queue="analytics",
)
def precompute_workspace_tag_analytics(self, workspace_id: int) -> dict:
    """Compute (or refresh) the tag analytics cache for a single workspace.

    Safe to call multiple times — if the cache is still fresh the computation
    is skipped and the task returns early.
    """
    logger.info("[workspace=%d] Starting tag analytics pre-compute", workspace_id)
    db = SessionLocal()
    try:
        stats = compute_workspace_tag_stats(
            db,
            workspace_id,
            force_recompute=True,
        )
        result = {
            "workspace_id": workspace_id,
            "tags_computed": len(stats.frequencies),
            "status": "ok",
        }
        logger.info(
            "[workspace=%d] Tag analytics pre-compute finished: %d tags",
            workspace_id,
            len(stats.frequencies),
        )
        return result
    except OperationalError as exc:
        logger.error(
            "[workspace=%d] DB error during tag analytics pre-compute: %s",
            workspace_id,
            exc,
        )
        raise self.retry(exc=exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "[workspace=%d] Unexpected error during tag analytics pre-compute",
            workspace_id,
        )
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(
    name="tag_analytics.precompute_all_workspaces",
    acks_late=True,
    queue="analytics",
)
def precompute_all_workspaces() -> dict:
    """Fan out per-workspace pre-compute tasks for every active workspace.

    Registered on the Celery beat schedule to run every 25 minutes so that
    the Redis TTL (30 min) is refreshed before it expires under normal load.
    """
    db = SessionLocal()
    try:
        workspace_ids: list[int] = [
            row[0]
            for row in db.query(Workspace.id)
            .filter(Workspace.is_active.is_(True))
            .all()
        ]
    finally:
        db.close()

    logger.info("Queuing tag analytics pre-compute for %d workspaces", len(workspace_ids))
    for wid in workspace_ids:
        precompute_workspace_tag_analytics.delay(wid)

    return {"queued": len(workspace_ids)}


# ---------------------------------------------------------------------------
# Beat schedule registration (imported by celery_app.py)
# ---------------------------------------------------------------------------

ANALYTICS_BEAT_SCHEDULE = {
    "tag-analytics-precompute-all": {
        "task": "tag_analytics.precompute_all_workspaces",
        "schedule": timedelta(minutes=25),
        "options": {"queue": "analytics"},
    },
}
