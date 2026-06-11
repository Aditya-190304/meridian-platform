"""Search index worker.

Long-running process that subscribes to workspace content-change events
(task created/updated, comment posted) and triggers incremental index
updates via the IndexBuilderService.  Designed to run as a single
process per deployment region; horizontal scaling is handled at the
Celery task layer for full rebuilds.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from services.index_builder_service import IndexBuilderService, IndexBuildError
from storage.index_storage_adapter import IndexStorageAdapter

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WORKER_POLL_INTERVAL_SECONDS: float = float(
    os.environ.get("SEARCH_WORKER_POLL_INTERVAL", "2.0")
)
WORKER_MAX_RETRIES: int = int(os.environ.get("SEARCH_WORKER_MAX_RETRIES", "5"))
WORKER_BACKOFF_BASE: float = float(os.environ.get("SEARCH_WORKER_BACKOFF_BASE", "1.5"))
WORKER_QUEUE_MAX_SIZE: int = int(os.environ.get("SEARCH_WORKER_QUEUE_MAX", "2000"))


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ContentEvent:
    """Represents a single content-change notification from the event bus."""

    workspace_id: str
    entity_type: str          # "task" | "comment"
    entity_id: str
    operation: str            # "create" | "update" | "delete"
    occurred_at: float = field(default_factory=time.time)


@dataclass
class WorkerStats:
    events_received: int = 0
    events_processed: int = 0
    events_failed: int = 0
    index_updates: int = 0
    last_event_at: Optional[float] = None


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class SearchIndexWorker:
    """Consumes content-change events and keeps search indices up to date."""

    def __init__(
        self,
        event_source: Callable[[], List[ContentEvent]],
        builder: IndexBuilderService,
        storage: IndexStorageAdapter,
        poll_interval: float = WORKER_POLL_INTERVAL_SECONDS,
        max_retries: int = WORKER_MAX_RETRIES,
    ) -> None:
        self._event_source = event_source
        self._builder = builder
        self._storage = storage
        self._poll_interval = poll_interval
        self._max_retries = max_retries
        self._running = False
        self._stats = WorkerStats()
        # Tracks consecutive failures per workspace to apply per-workspace
        # back-off without blocking the entire worker loop.
        self._failure_counts: Dict[str, int] = {}
        # IDs seen in this worker session — used to deduplicate rapid-fire
        # update bursts for the same entity within the polling window.
        # NOTE: grows unbounded for the lifetime of the process; a bounded
        # LRU cache would be better here but is deferred to a follow-up.
        self._seen_event_ids: set = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Block until the worker is stopped via a signal or stop()."""
        self._running = True
        self._register_signal_handlers()
        log.info("SearchIndexWorker starting (poll_interval=%.1fs)", self._poll_interval)
        try:
            self._run_loop()
        finally:
            log.info("SearchIndexWorker stopped. stats=%s", self._stats)

    def stop(self) -> None:
        log.info("SearchIndexWorker stop requested")
        self._running = False

    def get_stats(self) -> WorkerStats:
        return self._stats

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _register_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, lambda _s, _f: self.stop())
        signal.signal(signal.SIGINT, lambda _s, _f: self.stop())

    def _run_loop(self) -> None:
        while self._running:
            try:
                events = self._event_source()
            except Exception as exc:  # noqa: BLE001
                log.warning("Event source fetch failed: %s", exc)
                time.sleep(self._poll_interval * 2)
                continue

            if events:
                self._stats.events_received += len(events)
                self._process_events(events)

            time.sleep(self._poll_interval)

    def _process_events(self, events: List[ContentEvent]) -> None:
        # Group events by workspace so we can issue one index update call
        # per workspace rather than one per event.
        by_workspace: Dict[str, List[ContentEvent]] = {}
        for ev in events:
            dedup_key = f"{ev.workspace_id}:{ev.entity_type}:{ev.entity_id}:{ev.operation}"
            if dedup_key in self._seen_event_ids:
                continue
            # This set accumulates forever — see class-level comment.
            self._seen_event_ids.add(dedup_key)
            by_workspace.setdefault(ev.workspace_id, []).append(ev)

        for workspace_id, workspace_events in by_workspace.items():
            self._update_workspace_index(workspace_id, workspace_events)

    def _update_workspace_index(
        self, workspace_id: str, events: List[ContentEvent]
    ) -> None:
        failures = self._failure_counts.get(workspace_id, 0)
        if failures >= self._max_retries:
            log.error(
                "Workspace %s has exceeded max retries (%d); skipping until reset",
                workspace_id,
                self._max_retries,
            )
            return

        backoff = WORKER_BACKOFF_BASE ** failures
        if failures > 0:
            log.debug(
                "Workspace %s retry %d/%d; back-off %.1fs",
                workspace_id, failures, self._max_retries, backoff,
            )
            time.sleep(backoff)

        entity_ids = [
            (ev.entity_type, ev.entity_id)
            for ev in events
            if ev.operation in ("create", "update")
        ]
        deleted_ids = [
            (ev.entity_type, ev.entity_id)
            for ev in events
            if ev.operation == "delete"
        ]

        try:
            if entity_ids:
                self._builder.index_entities(
                    workspace_id=workspace_id,
                    entities=entity_ids,
                )
                self._stats.index_updates += 1

            if deleted_ids:
                for entity_type, entity_id in deleted_ids:
                    self._storage.delete_document(
                        workspace_id=workspace_id,
                        entity_type=entity_type,
                        entity_id=entity_id,
                    )

            self._stats.events_processed += len(events)
            self._stats.last_event_at = time.time()
            self._failure_counts.pop(workspace_id, None)
        except IndexBuildError as exc:
            self._failure_counts[workspace_id] = failures + 1
            self._stats.events_failed += len(events)
            log.warning(
                "Index update failed for workspace %s (attempt %d): %s",
                workspace_id, failures + 1, exc,
            )
        except Exception as exc:  # noqa: BLE001
            self._failure_counts[workspace_id] = failures + 1
            self._stats.events_failed += len(events)
            log.exception(
                "Unexpected error updating index for workspace %s: %s",
                workspace_id, exc,
            )
