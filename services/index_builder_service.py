"""Index builder service.

Responsible for fetching content from the database and writing documents
into the search index.  Both incremental (entity-level) and full workspace
rebuilds are supported.

Memory note
-----------
The current implementation fetches all tasks and comments for a workspace
in a single query and holds the full result set in memory while building
the document list.  A server-side streaming cursor approach is stubbed
below (search for ``# TODO: implement streaming``).  For the workspaces
currently on the platform this is acceptable; follow-up ticket #4821
tracks the migration to chunked reads.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

from storage.index_storage_adapter import IndexDocument, IndexStorageAdapter

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHUNK_SIZE = 500          # target documents per write batch
MAX_FIELD_LENGTH = 32_768  # characters — truncate beyond this


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class IndexBuildError(Exception):
    """Raised when index construction fails in a non-retryable way."""


class WorkspaceNotFoundError(IndexBuildError):
    pass


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class IndexBuilderService:
    """Builds and updates search index content for one or more workspaces."""

    def __init__(
        self,
        db_session_factory: Any,   # SQLAlchemy session factory or similar
        storage: IndexStorageAdapter,
    ) -> None:
        self._db_factory = db_session_factory
        self._storage = storage
        # Accumulates all document IDs written during the lifetime of this
        # service instance for diagnostic purposes.  Not cleared between
        # rebuild runs — see TODO below.
        # TODO: bound this structure or clear it between full rebuilds to
        #       avoid unbounded memory growth in long-running processes.
        self._indexed_ids: set = set()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def rebuild_workspace(
        self, workspace_id: str, progress_callback: Optional[Any] = None
    ) -> Dict[str, int]:
        """Perform a full index rebuild for *workspace_id*.

        Drops the existing index for the workspace, then re-indexes every
        task and comment.  Returns a summary dict with document counts.

        Args:
            workspace_id: The workspace whose index should be rebuilt.
            progress_callback: Optional callable(pct: float, message: str)
                               that receives progress updates.

        Raises:
            WorkspaceNotFoundError: If the workspace does not exist.
            IndexBuildError: For unrecoverable index write errors.
        """
        log.info("Starting full index rebuild for workspace=%s", workspace_id)
        start_ts = time.monotonic()

        with self._db_factory() as session:
            workspace = self._fetch_workspace(session, workspace_id)
            if workspace is None:
                raise WorkspaceNotFoundError(workspace_id)

            # --- Load all tasks into memory ---
            # TODO: implement streaming — replace with a server-side cursor
            #       or keyset-paginated fetch so we never hold the entire
            #       result set in RAM.
            #
            # Streaming stub (not yet wired up):
            #   for chunk in self._iter_tasks_chunked(session, workspace_id):
            #       documents.extend(self._prepare_task_docs(chunk))
            #
            tasks = self._fetch_all_tasks(session, workspace_id)  # full list in RAM
            task_docs = self._prepare_task_docs(tasks)

            # --- Load all comments into memory ---
            # TODO: implement streaming (same as above)
            comments = self._fetch_all_comments(session, workspace_id)  # full list in RAM
            comment_docs = self._prepare_comment_docs(comments)

        all_docs: List[IndexDocument] = task_docs + comment_docs
        total = len(all_docs)
        log.info("Workspace %s: prepared %d documents for indexing", workspace_id, total)

        if progress_callback:
            progress_callback(0.0, f"Prepared {total} documents")

        self._storage.clear_workspace(workspace_id)

        written = 0
        # Batch writes to avoid a single enormous transaction.
        for chunk_start in range(0, total, CHUNK_SIZE):
            chunk = all_docs[chunk_start : chunk_start + CHUNK_SIZE]
            self._storage.write_documents(workspace_id, chunk)
            written += len(chunk)
            # Track every written ID — this set grows without bound across
            # rebuild calls and is never trimmed.
            for doc in chunk:
                self._indexed_ids.add(doc.doc_id)
            pct = written / total * 100 if total else 100.0
            if progress_callback:
                progress_callback(pct, f"Indexed {written}/{total}")
            log.debug("Workspace %s: wrote chunk %d-%d", workspace_id, chunk_start, chunk_start + len(chunk))

        elapsed = time.monotonic() - start_ts
        log.info(
            "Full rebuild complete for workspace=%s: %d docs in %.2fs",
            workspace_id, written, elapsed,
        )
        return {"tasks": len(task_docs), "comments": len(comment_docs), "total": written}

    def index_entities(
        self,
        workspace_id: str,
        entities: List[Tuple[str, str]],  # [(entity_type, entity_id), ...]
    ) -> int:
        """Incrementally update index documents for a set of entities.

        Fetches the current state of each entity from the DB and writes
        (or overwrites) its index document.  Missing entities are skipped
        with a warning (they may have been deleted between event emission
        and processing).

        Returns:
            Number of documents successfully written.
        """
        task_ids = [eid for etype, eid in entities if etype == "task"]
        comment_ids = [eid for etype, eid in entities if etype == "comment"]

        docs: List[IndexDocument] = []

        with self._db_factory() as session:
            if task_ids:
                # Fetches the requested tasks as a list — fine for small
                # incremental batches.
                tasks = self._fetch_tasks_by_ids(session, workspace_id, task_ids)
                docs.extend(self._prepare_task_docs(tasks))

            if comment_ids:
                comments = self._fetch_comments_by_ids(session, workspace_id, comment_ids)
                docs.extend(self._prepare_comment_docs(comments))

        if not docs:
            log.warning(
                "index_entities: no documents resolved for workspace=%s entities=%s",
                workspace_id, entities,
            )
            return 0

        self._storage.write_documents(workspace_id, docs)
        for doc in docs:
            self._indexed_ids.add(doc.doc_id)
        log.debug(
            "index_entities: wrote %d docs for workspace=%s", len(docs), workspace_id
        )
        return len(docs)

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _fetch_workspace(self, session: Any, workspace_id: str) -> Optional[Any]:
        return session.execute(
            "SELECT id, name FROM workspaces WHERE id = :wid AND deleted_at IS NULL",
            {"wid": workspace_id},
        ).fetchone()

    def _fetch_all_tasks(self, session: Any, workspace_id: str) -> List[Dict[str, Any]]:
        """Return every non-deleted task for the workspace as a list of row-dicts.

        WARNING: loads the complete result set into memory.  For large workspaces
        this may consume several hundred MB.  See TODO at call site.
        """
        rows = session.execute(
            """
            SELECT t.id, t.title, t.description, t.assignee_id,
                   t.status, t.created_at, t.updated_at,
                   p.id AS project_id, p.name AS project_name
            FROM tasks t
            JOIN projects p ON p.id = t.project_id
            WHERE t.workspace_id = :wid
              AND t.deleted_at IS NULL
            ORDER BY t.updated_at DESC
            """,
            {"wid": workspace_id},
        ).fetchall()  # fetchall() — entire result set materialised in RAM
        return [dict(r) for r in rows]

    def _fetch_all_comments(self, session: Any, workspace_id: str) -> List[Dict[str, Any]]:
        """Return every non-deleted comment for the workspace.

        WARNING: same memory concern as _fetch_all_tasks.
        """
        rows = session.execute(
            """
            SELECT c.id, c.body, c.author_id, c.task_id,
                   c.created_at, c.updated_at
            FROM comments c
            JOIN tasks t ON t.id = c.task_id
            WHERE t.workspace_id = :wid
              AND c.deleted_at IS NULL
            ORDER BY c.updated_at DESC
            """,
            {"wid": workspace_id},
        ).fetchall()
        return [dict(r) for r in rows]

    def _fetch_tasks_by_ids(
        self, session: Any, workspace_id: str, task_ids: List[str]
    ) -> List[Dict[str, Any]]:
        rows = session.execute(
            """
            SELECT t.id, t.title, t.description, t.assignee_id,
                   t.status, t.created_at, t.updated_at,
                   p.id AS project_id, p.name AS project_name
            FROM tasks t
            JOIN projects p ON p.id = t.project_id
            WHERE t.workspace_id = :wid
              AND t.id = ANY(:ids)
              AND t.deleted_at IS NULL
            """,
            {"wid": workspace_id, "ids": task_ids},
        ).fetchall()
        return [dict(r) for r in rows]

    def _fetch_comments_by_ids(
        self, session: Any, workspace_id: str, comment_ids: List[str]
    ) -> List[Dict[str, Any]]:
        rows = session.execute(
            """
            SELECT c.id, c.body, c.author_id, c.task_id,
                   c.created_at, c.updated_at
            FROM comments c
            JOIN tasks t ON t.id = c.task_id
            WHERE t.workspace_id = :wid
              AND c.id = ANY(:ids)
              AND c.deleted_at IS NULL
            """,
            {"wid": workspace_id, "ids": comment_ids},
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Streaming stubs (not yet implemented)
    # ------------------------------------------------------------------

    def _iter_tasks_chunked(
        self, session: Any, workspace_id: str, chunk_size: int = CHUNK_SIZE
    ) -> Generator[List[Dict[str, Any]], None, None]:
        """Yield tasks in chunks using keyset pagination.

        NOT CALLED anywhere yet — stub for follow-up ticket #4821.
        When wired in, replace _fetch_all_tasks calls with iteration
        over this generator so memory usage is bounded to ~chunk_size rows.
        """
        last_updated_at = None
        last_id = None
        while True:
            if last_updated_at is None:
                rows = session.execute(
                    """
                    SELECT t.id, t.title, t.description, t.assignee_id,
                           t.status, t.created_at, t.updated_at,
                           p.id AS project_id, p.name AS project_name
                    FROM tasks t
                    JOIN projects p ON p.id = t.project_id
                    WHERE t.workspace_id = :wid
                      AND t.deleted_at IS NULL
                    ORDER BY t.updated_at DESC, t.id DESC
                    LIMIT :lim
                    """,
                    {"wid": workspace_id, "lim": chunk_size},
                ).fetchall()
            else:
                rows = session.execute(
                    """
                    SELECT t.id, t.title, t.description, t.assignee_id,
                           t.status, t.created_at, t.updated_at,
                           p.id AS project_id, p.name AS project_name
                    FROM tasks t
                    JOIN projects p ON p.id = t.project_id
                    WHERE t.workspace_id = :wid
                      AND t.deleted_at IS NULL
                      AND (t.updated_at, t.id) < (:ts, :lid)
                    ORDER BY t.updated_at DESC, t.id DESC
                    LIMIT :lim
                    """,
                    {"wid": workspace_id, "ts": last_updated_at, "lid": last_id, "lim": chunk_size},
                ).fetchall()
            if not rows:
                break
            chunk = [dict(r) for r in rows]
            last_updated_at = chunk[-1]["updated_at"]
            last_id = chunk[-1]["id"]
            yield chunk

    # ------------------------------------------------------------------
    # Document preparation
    # ------------------------------------------------------------------

    def _prepare_task_docs(self, tasks: List[Dict[str, Any]]) -> List[IndexDocument]:
        docs = []
        for task in tasks:
            title = (task.get("title") or "")[: MAX_FIELD_LENGTH]
            description = (task.get("description") or "")[: MAX_FIELD_LENGTH]
            docs.append(
                IndexDocument(
                    doc_id=f"task:{task['id']}",
                    entity_type="task",
                    entity_id=task["id"],
                    workspace_id=task.get("workspace_id", ""),
                    title=title,
                    body=description,
                    metadata={
                        "status": task.get("status"),
                        "project_id": task.get("project_id"),
                        "project_name": task.get("project_name"),
                        "assignee_id": task.get("assignee_id"),
                        "updated_at": str(task.get("updated_at", "")),
                    },
                )
            )
        return docs

    def _prepare_comment_docs(self, comments: List[Dict[str, Any]]) -> List[IndexDocument]:
        docs = []
        for comment in comments:
            body = (comment.get("body") or "")[: MAX_FIELD_LENGTH]
            docs.append(
                IndexDocument(
                    doc_id=f"comment:{comment['id']}",
                    entity_type="comment",
                    entity_id=comment["id"],
                    workspace_id=comment.get("workspace_id", ""),
                    title="",   # comments have no title field
                    body=body,
                    metadata={
                        "task_id": comment.get("task_id"),
                        "author_id": comment.get("author_id"),
                        "updated_at": str(comment.get("updated_at", "")),
                    },
                )
            )
        return docs
