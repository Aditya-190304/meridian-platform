"""Index storage adapter.

Provides a thin abstraction over the on-disk full-text index (whoosh).
Consumers interact only with IndexDocument and IndexStorageAdapter;
whoosh internals are fully encapsulated here so the underlying engine
could be swapped (e.g., to Tantivy via tantivy-py) without touching
calling code.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from whoosh import index as whoosh_index
    from whoosh.fields import ID, TEXT, Schema, StoredField
    from whoosh.qparser import MultifieldParser
    from whoosh.writing import AsyncWriter
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "whoosh is required for search index storage. "
        "Install it with: pip install whoosh"
    ) from exc

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

INDEX_SCHEMA = Schema(
    doc_id=ID(stored=True, unique=True),
    entity_type=ID(stored=True),
    entity_id=ID(stored=True),
    workspace_id=ID(stored=True),
    title=TEXT(stored=False),
    body=TEXT(stored=False),
    project_name=TEXT(stored=False),
    status=StoredField(),
    metadata_json=StoredField(),
)

SEARCH_FIELDS = ["title", "body", "project_name"]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class IndexDocument:
    """A single document to be stored in the index."""

    doc_id: str                        # globally unique; e.g. "task:abc123"
    entity_type: str                   # "task" | "comment"
    entity_id: str
    workspace_id: str
    title: str
    body: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResult:
    doc_id: str
    entity_type: str
    entity_id: str
    score: float
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class IndexStorageAdapter:
    """Manages per-workspace whoosh indices stored under *base_dir*."""

    def __init__(self, base_dir: str | os.PathLike) -> None:
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        # One lock per workspace to allow concurrent reads while serialising
        # writes within a workspace.
        self._locks: Dict[str, threading.Lock] = {}
        self._lock_registry_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Index lifecycle
    # ------------------------------------------------------------------

    def _workspace_dir(self, workspace_id: str) -> Path:
        safe = workspace_id.replace("/", "_").replace("..", "")
        return self._base_dir / safe

    def _get_or_create_index(self, workspace_id: str) -> Any:
        idx_dir = self._workspace_dir(workspace_id)
        if whoosh_index.exists_in(str(idx_dir)):
            return whoosh_index.open_dir(str(idx_dir))
        idx_dir.mkdir(parents=True, exist_ok=True)
        log.info("Creating new index directory for workspace=%s at %s", workspace_id, idx_dir)
        return whoosh_index.create_in(str(idx_dir), schema=INDEX_SCHEMA)

    def _workspace_lock(self, workspace_id: str) -> threading.Lock:
        with self._lock_registry_lock:
            if workspace_id not in self._locks:
                self._locks[workspace_id] = threading.Lock()
            return self._locks[workspace_id]

    def clear_workspace(self, workspace_id: str) -> None:
        """Delete all documents for *workspace_id* and reset the index."""
        with self._workspace_lock(workspace_id):
            idx_dir = self._workspace_dir(workspace_id)
            if idx_dir.exists():
                import shutil
                shutil.rmtree(str(idx_dir))
                log.info("Cleared index for workspace=%s", workspace_id)
            idx_dir.mkdir(parents=True, exist_ok=True)
            whoosh_index.create_in(str(idx_dir), schema=INDEX_SCHEMA)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def write_documents(
        self, workspace_id: str, documents: List[IndexDocument]
    ) -> None:
        """Write (upsert) a list of documents into the workspace index.

        Uses whoosh AsyncWriter so the caller is not blocked on the OS
        flush; commits are batched automatically.
        """
        if not documents:
            return

        with self._workspace_lock(workspace_id):
            idx = self._get_or_create_index(workspace_id)
            writer = AsyncWriter(idx)
            try:
                for doc in documents:
                    import json
                    writer.update_document(
                        doc_id=doc.doc_id,
                        entity_type=doc.entity_type,
                        entity_id=doc.entity_id,
                        workspace_id=workspace_id,
                        title=doc.title or "",
                        body=doc.body or "",
                        project_name=doc.metadata.get("project_name") or "",
                        status=doc.metadata.get("status") or "",
                        metadata_json=json.dumps(doc.metadata),
                    )
                writer.commit()
                log.debug(
                    "Wrote %d documents to workspace=%s index",
                    len(documents), workspace_id,
                )
            except Exception:
                writer.cancel()
                raise

    def delete_document(
        self, workspace_id: str, entity_type: str, entity_id: str
    ) -> bool:
        """Remove a single document from the index.

        Returns True if the document was found and deleted, False if it
        was not present (which is not treated as an error).
        """
        doc_id = f"{entity_type}:{entity_id}"
        with self._workspace_lock(workspace_id):
            idx = self._get_or_create_index(workspace_id)
            writer = idx.writer()
            try:
                deleted = writer.delete_by_term("doc_id", doc_id)
                writer.commit()
                found = deleted > 0
                if not found:
                    log.debug(
                        "delete_document: doc_id=%s not found in workspace=%s",
                        doc_id, workspace_id,
                    )
                return found
            except Exception:
                writer.cancel()
                raise

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def search(
        self,
        workspace_id: str,
        query_string: str,
        limit: int = 20,
        entity_types: Optional[List[str]] = None,
    ) -> List[SearchResult]:
        """Run a full-text search against the workspace index.

        Args:
            workspace_id: The workspace to search within.
            query_string: Free-text query (supports whoosh query syntax).
            limit: Maximum number of results to return.
            entity_types: If provided, restrict results to these entity types.

        Returns:
            List of SearchResult ordered by relevance score descending.
        """
        idx = self._get_or_create_index(workspace_id)
        results: List[SearchResult] = []

        with idx.searcher() as searcher:
            parser = MultifieldParser(SEARCH_FIELDS, schema=idx.schema)
            try:
                q = parser.parse(query_string)
            except Exception as exc:  # noqa: BLE001
                log.warning("Query parse error for '%s': %s", query_string, exc)
                return []

            hits = searcher.search(q, limit=limit)
            import json
            for hit in hits:
                if entity_types and hit["entity_type"] not in entity_types:
                    continue
                try:
                    metadata = json.loads(hit["metadata_json"])
                except (KeyError, ValueError):
                    metadata = {}
                results.append(
                    SearchResult(
                        doc_id=hit["doc_id"],
                        entity_type=hit["entity_type"],
                        entity_id=hit["entity_id"],
                        score=hit.score,
                        metadata=metadata,
                    )
                )

        return results

    def document_count(self, workspace_id: str) -> int:
        """Return the number of documents currently in the workspace index."""
        if not self._workspace_dir(workspace_id).exists():
            return 0
        idx = self._get_or_create_index(workspace_id)
        return idx.doc_count()

    def index_exists(self, workspace_id: str) -> bool:
        return whoosh_index.exists_in(str(self._workspace_dir(workspace_id)))
