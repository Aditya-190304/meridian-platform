from __future__ import annotations

import base64
import logging
from typing import Any, Dict, List, Optional, Tuple

from databases import Database

from meridian.schemas.search import (
    ProjectSearchParams,
    ProjectResult,
    TaskResult,
    TaskSearchParams,
)

logger = logging.getLogger(__name__)


def _encode_page_token(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def _decode_page_token(token: str) -> int:
    try:
        return int(base64.urlsafe_b64decode(token.encode()).decode())
    except Exception:
        return 0


class SearchService:
    """
    Handles advanced search queries for projects and tasks.

    All queries are scoped to a single tenant via ``tenant_id`` to enforce
    data isolation in the multi-tenant environment.
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Task search
    # ------------------------------------------------------------------

    async def search_tasks(
        self, tenant_id: str, params: TaskSearchParams
    ) -> Tuple[List[TaskResult], int, Optional[str]]:
        """
        Execute a filtered, sorted, paginated task search.

        Returns a tuple of (items, total_count, next_page_token).
        """
        offset = _decode_page_token(params.page_token) if params.page_token else 0

        where_clauses: List[str] = ["t.tenant_id = :tenant_id", "t.deleted_at IS NULL"]
        bind: Dict[str, Any] = {"tenant_id": tenant_id}

        # Free-text search
        if params.q:
            where_clauses.append(
                "(t.name ILIKE :q OR t.description ILIKE :q)"
            )
            bind["q"] = f"%{params.q}%"

        # Status filter
        if params.status:
            where_clauses.append("t.status = :status")
            bind["status"] = params.status.value

        # Priority filter
        if params.priority is not None:
            where_clauses.append("t.priority = :priority")
            bind["priority"] = params.priority

        # Project filter
        if params.project_id:
            where_clauses.append("t.project_id = :project_id")
            bind["project_id"] = params.project_id

        # Assignee filter — one or many UUIDs supplied as a comma-separated string
        if params.assignee_ids:
            ids = [a.strip() for a in params.assignee_ids.split(",") if a.strip()]
            if ids:
                placeholders = ", ".join(f":assignee_{i}" for i in range(len(ids)))
                where_clauses.append(f"t.assignee_id IN ({placeholders})")
                for i, uid in enumerate(ids):
                    bind[f"assignee_{i}"] = uid

        # Date range — created_at
        if params.created_after:
            where_clauses.append("t.created_at >= :created_after")
            bind["created_after"] = params.created_after.isoformat()
        if params.created_before:
            where_clauses.append("t.created_at <= :created_before")
            bind["created_before"] = params.created_before.isoformat()

        # Date range — due_date
        if params.due_after:
            where_clauses.append("t.due_date >= :due_after")
            bind["due_after"] = params.due_after.isoformat()
        if params.due_before:
            where_clauses.append("t.due_date <= :due_before")
            bind["due_before"] = params.due_before.isoformat()

        # Tag filter
        # Tags are stored as a comma-separated string in t.tags (e.g. "backend,urgent,q2").
        # We need each requested tag to appear somewhere in that string.  A simple LIKE
        # approach avoids needing a lateral join for this legacy column layout.
        if params.tags:
            tag_list = [tg.strip() for tg in params.tags.split(",") if tg.strip()]
            if tag_list:
                # Build one LIKE condition per tag so partial matches are avoided by
                # anchoring on commas or string boundaries.
                tag_conditions = " AND ".join(
                    f"(',' || t.tags || ',') LIKE '%,{tag},%'"
                    for tag in tag_list
                )
                where_clauses.append(f"({tag_conditions})")

        where_sql = " AND ".join(where_clauses)

        # Sort — sort_by is validated against the allowlist in the schema validator
        # before reaching this point, so it is safe to interpolate.
        order_sql = f"t.{params.sort_by} {params.sort_dir.value}"

        count_sql = f"""
            SELECT COUNT(*) AS cnt
            FROM tasks t
            WHERE {where_sql}
        """

        data_sql = f"""
            SELECT
                t.id,
                t.name,
                t.description,
                t.status,
                t.priority,
                t.assignee_id,
                t.project_id,
                t.tags,
                t.created_at,
                t.updated_at,
                t.due_date
            FROM tasks t
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT :limit
            OFFSET :offset
        """

        bind["limit"] = params.page_size
        bind["offset"] = offset

        logger.debug("task search sql=%s bind_keys=%s", data_sql, list(bind.keys()))

        total_row = await self._db.fetch_one(count_sql, bind)
        total_count: int = total_row["cnt"] if total_row else 0

        rows = await self._db.fetch_all(data_sql, bind)

        items: List[TaskResult] = []
        for row in rows:
            raw_tags = row["tags"] or ""
            items.append(
                TaskResult(
                    id=str(row["id"]),
                    name=row["name"],
                    description=row["description"],
                    status=row["status"],
                    priority=row["priority"],
                    assignee_id=str(row["assignee_id"]) if row["assignee_id"] else None,
                    project_id=str(row["project_id"]),
                    tags=[t.strip() for t in raw_tags.split(",") if t.strip()],
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                    due_date=str(row["due_date"]) if row["due_date"] else None,
                )
            )

        next_token: Optional[str] = None
        new_offset = offset + params.page_size
        if new_offset < total_count:
            next_token = _encode_page_token(new_offset)

        return items, total_count, next_token

    # ------------------------------------------------------------------
    # Project search
    # ------------------------------------------------------------------

    async def search_projects(
        self, tenant_id: str, params: ProjectSearchParams
    ) -> Tuple[List[ProjectResult], int, Optional[str]]:
        """
        Execute a filtered, sorted, paginated project search.

        Returns a tuple of (items, total_count, next_page_token).
        """
        offset = _decode_page_token(params.page_token) if params.page_token else 0

        where_clauses: List[str] = ["p.tenant_id = :tenant_id", "p.deleted_at IS NULL"]
        bind: Dict[str, Any] = {"tenant_id": tenant_id}

        if params.q:
            where_clauses.append("p.name ILIKE :q")
            bind["q"] = f"%{params.q}%"

        if params.status:
            where_clauses.append("p.status = :status")
            bind["status"] = params.status.value

        if params.owner_id:
            where_clauses.append("p.owner_id = :owner_id")
            bind["owner_id"] = params.owner_id

        if params.start_after:
            where_clauses.append("p.start_date >= :start_after")
            bind["start_after"] = params.start_after.isoformat()

        if params.end_before:
            where_clauses.append("p.end_date <= :end_before")
            bind["end_before"] = params.end_before.isoformat()

        where_sql = " AND ".join(where_clauses)
        order_sql = f"p.{params.sort_by} {params.sort_dir.value}"

        count_sql = f"""
            SELECT COUNT(*) AS cnt
            FROM projects p
            WHERE {where_sql}
        """

        data_sql = f"""
            SELECT
                p.id,
                p.name,
                p.description,
                p.status,
                p.owner_id,
                p.created_at,
                p.updated_at,
                p.start_date,
                p.end_date,
                (
                    SELECT COUNT(*)
                    FROM tasks t2
                    WHERE t2.project_id = p.id
                      AND t2.deleted_at IS NULL
                ) AS task_count
            FROM projects p
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT :limit
            OFFSET :offset
        """

        bind["limit"] = params.page_size
        bind["offset"] = offset

        logger.debug("project search sql=%s bind_keys=%s", data_sql, list(bind.keys()))

        total_row = await self._db.fetch_one(count_sql, bind)
        total_count: int = total_row["cnt"] if total_row else 0

        rows = await self._db.fetch_all(data_sql, bind)

        items: List[ProjectResult] = [
            ProjectResult(
                id=str(row["id"]),
                name=row["name"],
                description=row["description"],
                status=row["status"],
                owner_id=str(row["owner_id"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
                start_date=str(row["start_date"]) if row["start_date"] else None,
                end_date=str(row["end_date"]) if row["end_date"] else None,
                task_count=row["task_count"],
            )
            for row in rows
        ]

        next_token: Optional[str] = None
        new_offset = offset + params.page_size
        if new_offset < total_count:
            next_token = _encode_page_token(new_offset)

        return items, total_count, next_token
