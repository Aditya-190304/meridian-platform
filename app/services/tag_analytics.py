"""Tag analytics service for the Meridian Platform.

Computes tag frequency rankings and co-occurrence data across all tasks in a
workspace to power smart auto-suggestion when users create or edit tasks.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Optional

from sqlalchemy.orm import Session

from app.core.cache import redis_client
from app.models.tag import Tag
from app.models.task import Task
from app.schemas.tag_analytics import (
    CoOccurrenceEntry,
    TagFrequency,
    TagSuggestion,
    WorkspaceTagStats,
)

logger = logging.getLogger(__name__)

# Cache TTL — 30 minutes is long enough to absorb burst traffic but short
# enough that a flurry of new tags becomes visible reasonably quickly.
_CACHE_TTL_SECONDS = 1800
_CACHE_KEY_PREFIX = "meridian:tag_analytics"


def _workspace_cache_key(workspace_id: int) -> str:
    return f"{_CACHE_KEY_PREFIX}:{workspace_id}"


def invalidate_workspace_cache(workspace_id: int) -> None:
    """Drop the analytics cache for a workspace.

    Called by tag-mutation endpoints (create, delete, rename) so that
    subsequent suggestion requests reflect the updated tag taxonomy.
    """
    key = _workspace_cache_key(workspace_id)
    redis_client.delete(key)
    logger.debug("Invalidated tag analytics cache for workspace %d", workspace_id)


def _load_cached_stats(workspace_id: int) -> Optional[WorkspaceTagStats]:
    raw = redis_client.get(_workspace_cache_key(workspace_id))
    if raw is None:
        return None
    try:
        return WorkspaceTagStats.model_validate_json(raw)
    except Exception:  # noqa: BLE001
        logger.warning(
            "Failed to deserialise cached tag stats for workspace %d — recomputing",
            workspace_id,
        )
        return None


def _store_cached_stats(workspace_id: int, stats: WorkspaceTagStats) -> None:
    key = _workspace_cache_key(workspace_id)
    redis_client.setex(key, _CACHE_TTL_SECONDS, stats.model_dump_json())


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def compute_workspace_tag_stats(
    db: Session,
    workspace_id: int,
    *,
    force_recompute: bool = False,
) -> WorkspaceTagStats:
    """Return frequency and co-occurrence stats for all tags in a workspace.

    Results are cached in Redis.  Pass ``force_recompute=True`` from the
    background pre-compute task to bypass the cache and refresh it.
    """
    if not force_recompute:
        cached = _load_cached_stats(workspace_id)
        if cached is not None:
            logger.debug("Cache hit for workspace %d tag analytics", workspace_id)
            return cached

    logger.info("Computing tag analytics for workspace %d", workspace_id)

    # Fetch all tasks that belong to this workspace, with their tags
    # eagerly loaded to avoid N+1 on the relationship.
    tasks: list[Task] = (
        db.query(Task)
        .filter(Task.workspace_id == workspace_id, Task.deleted_at.is_(None))
        .all()
    )

    # Fetch canonical tag objects for the workspace so we can map id -> name.
    db_tags: list[Tag] = (
        db.query(Tag)
        .filter(Tag.workspace_id == workspace_id, Tag.deleted_at.is_(None))
        .order_by(Tag.name)
        .all()
    )
    tag_id_to_name: dict[int, str] = {t.id: t.name for t in db_tags}

    if not tasks or not db_tags:
        empty = WorkspaceTagStats(
            workspace_id=workspace_id,
            frequencies=[],
            co_occurrences={},
        )
        _store_cached_stats(workspace_id, empty)
        return empty

    # ------------------------------------------------------------------
    # Step 1 — build a task-to-tag-set index.
    # task_tags[task_id] = {tag_id, tag_id, ...}
    # ------------------------------------------------------------------
    task_tags: dict[int, set[int]] = {}
    for task in tasks:
        task_tags[task.id] = {tag.id for tag in task.tags}

    # ------------------------------------------------------------------
    # Step 2 — frequency count.
    # Count how many tasks each tag appears on.
    # ------------------------------------------------------------------
    tag_task_count: dict[int, int] = defaultdict(int)
    for task in tasks:
        for tag in task.tags:
            tag_task_count[tag.id] += 1

    # ------------------------------------------------------------------
    # Step 3 — co-occurrence matrix.
    #
    # For every unique tag A, iterate over ALL tasks to collect the tasks
    # that have tag A, then for each of those tasks iterate their tag set
    # to accumulate counts for every tag B that co-appears with A.
    #
    # This gives us co_occurrence[tag_a_id][tag_b_id] = shared_task_count.
    # ------------------------------------------------------------------
    unique_tag_ids = list(tag_id_to_name.keys())
    co_occurrence: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))

    for tag_a_id in unique_tag_ids:
        # Walk every task to find ones that carry tag_a.
        for task in tasks:                          # O(N) per tag
            if tag_a_id not in task_tags[task.id]:
                continue
            # This task has tag_a — credit all its other tags.
            for tag_b_id in task_tags[task.id]:    # O(tags per task) — typically small
                if tag_b_id != tag_a_id:
                    co_occurrence[tag_a_id][tag_b_id] += 1

    # ------------------------------------------------------------------
    # Step 4 — serialise into response schema.
    # ------------------------------------------------------------------
    frequencies: list[TagFrequency] = [
        TagFrequency(
            tag_id=tag_id,
            tag_name=tag_id_to_name[tag_id],
            task_count=tag_task_count.get(tag_id, 0),
        )
        for tag_id in unique_tag_ids
        if tag_id in tag_task_count
    ]
    frequencies.sort(key=lambda f: f.task_count, reverse=True)

    co_occurrences: dict[str, list[CoOccurrenceEntry]] = {}
    for tag_a_id, neighbours in co_occurrence.items():
        tag_name = tag_id_to_name.get(tag_a_id)
        if tag_name is None:
            continue
        entries = [
            CoOccurrenceEntry(
                tag_id=tag_b_id,
                tag_name=tag_id_to_name[tag_b_id],
                co_occurrence_count=count,
            )
            for tag_b_id, count in neighbours.items()
            if tag_b_id in tag_id_to_name
        ]
        entries.sort(key=lambda e: e.co_occurrence_count, reverse=True)
        co_occurrences[str(tag_a_id)] = entries

    stats = WorkspaceTagStats(
        workspace_id=workspace_id,
        frequencies=frequencies,
        co_occurrences=co_occurrences,
    )
    _store_cached_stats(workspace_id, stats)
    logger.info(
        "Tag analytics computed for workspace %d: %d tags, %d tasks",
        workspace_id,
        len(unique_tag_ids),
        len(tasks),
    )
    return stats


# ---------------------------------------------------------------------------
# Suggestion scoring
# ---------------------------------------------------------------------------

def suggest_tags(
    db: Session,
    workspace_id: int,
    *,
    partial_query: str = "",
    current_tag_ids: list[int] | None = None,
    limit: int = 10,
) -> list[TagSuggestion]:
    """Return ranked tag suggestions for a task being created/edited.

    Scoring logic:
    - Base score = normalised frequency rank (most-used tag = 1.0).
    - Co-occurrence bonus: for each tag already on the task, add the
      normalised co-occurrence weight for the candidate tag.
    - Partial query filter: if supplied, restrict to tags whose name
      contains the query string (case-insensitive).
    """
    stats = compute_workspace_tag_stats(db, workspace_id)

    if not stats.frequencies:
        return []

    current_ids: set[int] = set(current_tag_ids or [])
    max_count = stats.frequencies[0].task_count or 1

    # Build a quick lookup: tag_id -> TagFrequency
    freq_map = {f.tag_id: f for f in stats.frequencies}

    suggestions: list[TagSuggestion] = []
    for freq in stats.frequencies:
        # Skip tags the task already has.
        if freq.tag_id in current_ids:
            continue

        # Apply partial query filter.
        if partial_query and partial_query.lower() not in freq.tag_name.lower():
            continue

        base_score = freq.task_count / max_count

        # Co-occurrence boost from currently applied tags.
        co_boost = 0.0
        for current_id in current_ids:
            neighbours = stats.co_occurrences.get(str(current_id), [])
            for entry in neighbours:
                if entry.tag_id == freq.tag_id:
                    max_co = neighbours[0].co_occurrence_count if neighbours else 1
                    co_boost += entry.co_occurrence_count / (max_co or 1)
                    break

        score = base_score + co_boost
        suggestions.append(
            TagSuggestion(
                tag_id=freq.tag_id,
                tag_name=freq.tag_name,
                score=round(score, 4),
                reason="co_occurrence" if co_boost > 0 else "frequency",
            )
        )

    suggestions.sort(key=lambda s: s.score, reverse=True)
    return suggestions[:limit]
