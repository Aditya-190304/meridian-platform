"""Celery tasks for search index management.

Two task families:

1. ``rebuild_workspace_index``  — full index rebuild for a single workspace.
   Scheduled nightly via Celery Beat; also triggered manually when a
   workspace is first onboarded or when an operator requests a forced
   refresh.

2. ``rebuild_all_workspaces``   — fan-out task that enqueues
   ``rebuild_workspace_index`` for every active workspace.  Runs weekly.

All tasks are idempotent: running them multiple times produces the same
result (the index is rebuilt from the current DB state each time).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from celery import shared_task
from celery.utils.log import get_task_logger

from services.index_builder_service import (
    IndexBuildError,
    IndexBuilderService,
    WorkspaceNotFoundError,
)
from storage.index_storage_adapter import IndexStorageAdapter

log = get_task_logger(__name__)

# ---------------------------------------------------------------------------
# Dependency wiring
# ---------------------------------------------------------------------------
# In production these are injected via the app factory; here we use simple
# module-level singletons that are initialised lazily on first task execution.
# Tests replace them via monkeypatching or Celery's task_always_eager setting.

_storage: Optional[IndexStorageAdapter] = None
_builder: Optional[IndexBuilderService] = None


def _get_storage() -> IndexStorageAdapter:
    global _storage
    if _storage is None:
        import os
        from storage.index_storage_adapter import IndexStorageAdapter
        index_dir = os.environ.get("SEARCH_INDEX_DIR", "/var/meridian/search_index")
        _storage = IndexStorageAdapter(base_dir=index_dir)
    return _storage


def _get_builder() -> IndexBuilderService:
    global _builder
    if _builder is None:
        from database import get_session_factory  # noqa: PLC0415 — lazy import
        _builder = IndexBuilderService(
            db_session_factory=get_session_factory(),
            storage=_get_storage(),
        )
    return _builder


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@shared_task(
    name="search.rebuild_workspace_index",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    track_started=True,
)
def rebuild_workspace_index(
    self: Any,
    workspace_id: str,
    triggered_by: str = "scheduled",
) -> Dict[str, Any]:
    """Rebuild the full-text search index for a single workspace.

    Args:
        workspace_id: ID of the workspace to rebuild.
        triggered_by: Human-readable label for logging (e.g. "scheduled",
                      "onboarding", "operator").

    Returns:
        Summary dict: ``{workspace_id, tasks, comments, total, elapsed_s}``.
    """
    log.info(
        "rebuild_workspace_index started: workspace=%s triggered_by=%s attempt=%d",
        workspace_id, triggered_by, self.request.retries + 1,
    )
    start_ts = time.monotonic()

    def _progress(pct: float, message: str) -> None:
        self.update_state(
            state="PROGRESS",
            meta={"pct": round(pct, 1), "message": message, "workspace_id": workspace_id},
        )

    try:
        builder = _get_builder()
        stats = builder.rebuild_workspace(
            workspace_id=workspace_id,
            progress_callback=_progress,
        )
    except WorkspaceNotFoundError:
        log.error("rebuild_workspace_index: workspace %s not found; not retrying", workspace_id)
        return {
            "workspace_id": workspace_id,
            "status": "not_found",
            "error": "workspace not found",
        }
    except IndexBuildError as exc:
        log.warning(
            "rebuild_workspace_index: build error for workspace %s (attempt %d): %s",
            workspace_id, self.request.retries + 1, exc,
        )
        raise self.retry(exc=exc)
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "rebuild_workspace_index: unexpected error for workspace %s: %s",
            workspace_id, exc,
        )
        raise self.retry(exc=exc)

    elapsed = time.monotonic() - start_ts
    result = {
        "workspace_id": workspace_id,
        "status": "ok",
        "tasks": stats["tasks"],
        "comments": stats["comments"],
        "total": stats["total"],
        "elapsed_s": round(elapsed, 2),
        "triggered_by": triggered_by,
    }
    log.info("rebuild_workspace_index complete: %s", result)
    return result


@shared_task(
    name="search.rebuild_all_workspaces",
    bind=True,
    max_retries=1,
    default_retry_delay=300,
)
def rebuild_all_workspaces(self: Any) -> Dict[str, Any]:
    """Fan-out task: enqueue a full rebuild for every active workspace.

    Intended to run once per week (Sunday 02:00 UTC) via Celery Beat to
    recover from any accumulated drift between the index and the DB.
    """
    from database import get_session_factory  # noqa: PLC0415

    log.info("rebuild_all_workspaces: fetching active workspace list")
    workspace_ids: List[str] = []

    with get_session_factory()() as session:
        rows = session.execute(
            """
            SELECT id FROM workspaces
            WHERE deleted_at IS NULL
              AND plan != 'deactivated'
            ORDER BY id
            """
        ).fetchall()
        workspace_ids = [str(r[0]) for r in rows]

    log.info("rebuild_all_workspaces: enqueuing %d workspace rebuilds", len(workspace_ids))
    for wid in workspace_ids:
        rebuild_workspace_index.apply_async(
            kwargs={"workspace_id": wid, "triggered_by": "weekly_rebuild"},
            queue="search_index",
            countdown=0,
        )

    return {"enqueued": len(workspace_ids)}


# ---------------------------------------------------------------------------
# Celery Beat schedule entries (register in celeryconfig.py)
# ---------------------------------------------------------------------------

BEAT_SCHEDULE: Dict[str, Any] = {
    "search-rebuild-all-workspaces-weekly": {
        "task": "search.rebuild_all_workspaces",
        "schedule": _cron(day_of_week=0, hour=2, minute=0),  # Sun 02:00 UTC
        "options": {"queue": "search_index"},
    },
}


def _cron(**kwargs: Any) -> Any:
    """Thin wrapper so celeryconfig.py doesn't need to import celery.schedules."""
    from celery.schedules import crontab  # noqa: PLC0415
    return crontab(**kwargs)
