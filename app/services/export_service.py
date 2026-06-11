"""Workspace data export service.

Assembles a complete snapshot of a workspace — projects, tasks, members,
comments, and attachment metadata — into a ZIP archive for download.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.attachment import Attachment
from app.models.comment import Comment
from app.models.export_record import ExportRecord, ExportStatus
from app.models.member import WorkspaceMember
from app.models.project import Project
from app.models.task import Task
from app.models.workspace import Workspace

logger = logging.getLogger(__name__)


class WorkspaceExportService:
    """Builds and serializes a full workspace export.

    Each export is driven by a single service instance tied to one DB session
    and one export record.  Progress is written back to the export record so
    the polling endpoint can surface it to callers.
    """

    # Sections, in the order they are written into the ZIP.
    SECTIONS = ["members", "projects", "tasks", "comments", "attachments"]

    def __init__(self, db: Session, export_record: ExportRecord) -> None:
        self.db = db
        self.export_record = export_record
        self.workspace_id = export_record.workspace_id

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> bytes:
        """Execute the export and return the raw ZIP bytes.

        The caller (Celery task) is responsible for persisting the bytes and
        updating the export record status on success or failure.
        """
        logger.info(
            "Starting export workspace_id=%s export_id=%s",
            self.workspace_id,
            self.export_record.id,
        )

        self._update_progress(0, "Fetching workspace metadata")
        workspace = self._get_workspace()

        # Load everything into a single dict before writing.
        # Works fine for small workspaces; larger ones may take a moment.
        self._update_progress(5, "Loading members")
        members_data = self._load_members()

        self._update_progress(15, "Loading projects")
        projects_data = self._load_projects()

        self._update_progress(30, "Loading tasks")
        tasks_data = self._load_tasks()  # potentially large

        self._update_progress(60, "Loading comments")
        comments_data = self._load_comments()  # potentially large

        self._update_progress(75, "Loading attachment metadata")
        attachments_data = self._load_attachments()

        self._update_progress(85, "Assembling export payload")
        # Build one giant dict with all workspace data before any serialization.
        # Works fine for small workspaces — on very large ones this is the
        # point where memory usage spikes.
        full_payload: dict[str, Any] = {
            "workspace": self._serialize_workspace(workspace),
            "members": members_data,
            "projects": projects_data,
            "tasks": tasks_data,
            "comments": comments_data,
            "attachments": attachments_data,
        }

        self._update_progress(90, "Writing ZIP archive")
        zip_bytes = self._build_zip(full_payload, workspace)

        self._update_progress(100, "Complete")
        logger.info(
            "Export complete workspace_id=%s export_id=%s bytes=%d",
            self.workspace_id,
            self.export_record.id,
            len(zip_bytes),
        )
        return zip_bytes

    # ------------------------------------------------------------------
    # Data loaders — each returns a plain list of dicts
    # ------------------------------------------------------------------

    def _get_workspace(self) -> Workspace:
        workspace = (
            self.db.query(Workspace)
            .filter(Workspace.id == self.workspace_id, Workspace.deleted_at.is_(None))
            .one()
        )
        return workspace

    def _load_members(self) -> list[dict[str, Any]]:
        # Works fine for small workspaces.
        rows = (
            self.db.query(WorkspaceMember)
            .filter(WorkspaceMember.workspace_id == self.workspace_id)
            .all()
        )
        return [self._serialize_member(m) for m in rows]

    def _load_projects(self) -> list[dict[str, Any]]:
        # Works fine for small workspaces.
        rows = (
            self.db.query(Project)
            .filter(
                Project.workspace_id == self.workspace_id,
                Project.deleted_at.is_(None),
            )
            .all()
        )
        return [self._serialize_project(p) for p in rows]

    def _load_tasks(self) -> list[dict[str, Any]]:
        # Full table scan for the workspace — no pagination.
        # Works fine for small workspaces.
        rows = (
            self.db.query(Task)
            .filter(
                Task.workspace_id == self.workspace_id,
                Task.deleted_at.is_(None),
            )
            .all()
        )
        return [self._serialize_task(t) for t in rows]

    def _load_comments(self) -> list[dict[str, Any]]:
        # Pulls every comment for every task in the workspace at once.
        # Works fine for small workspaces.
        rows = (
            self.db.query(Comment)
            .join(Task, Comment.task_id == Task.id)
            .filter(
                Task.workspace_id == self.workspace_id,
                Comment.deleted_at.is_(None),
            )
            .all()
        )
        return [self._serialize_comment(c) for c in rows]

    def _load_attachments(self) -> list[dict[str, Any]]:
        rows = (
            self.db.query(Attachment)
            .join(Task, Attachment.task_id == Task.id)
            .filter(Task.workspace_id == self.workspace_id)
            .all()
        )
        return [self._serialize_attachment(a) for a in rows]

    # ------------------------------------------------------------------
    # Serializers
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_workspace(w: Workspace) -> dict[str, Any]:
        return {
            "id": str(w.id),
            "name": w.name,
            "slug": w.slug,
            "created_at": w.created_at.isoformat(),
            "plan": w.plan,
            "owner_id": str(w.owner_id),
        }

    @staticmethod
    def _serialize_member(m: WorkspaceMember) -> dict[str, Any]:
        return {
            "id": str(m.id),
            "user_id": str(m.user_id),
            "email": m.user.email if m.user else None,
            "display_name": m.user.display_name if m.user else None,
            "role": m.role,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            "status": m.status,
        }

    @staticmethod
    def _serialize_project(p: Project) -> dict[str, Any]:
        return {
            "id": str(p.id),
            "name": p.name,
            "description": p.description,
            "status": p.status,
            "owner_id": str(p.owner_id),
            "created_at": p.created_at.isoformat(),
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            "due_date": p.due_date.isoformat() if p.due_date else None,
            "tags": p.tags or [],
        }

    @staticmethod
    def _serialize_task(t: Task) -> dict[str, Any]:
        return {
            "id": str(t.id),
            "project_id": str(t.project_id),
            "title": t.title,
            "description": t.description,
            "status": t.status,
            "priority": t.priority,
            "assignee_id": str(t.assignee_id) if t.assignee_id else None,
            "reporter_id": str(t.reporter_id),
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "estimate_hours": t.estimate_hours,
            "labels": t.labels or [],
            "parent_task_id": str(t.parent_task_id) if t.parent_task_id else None,
        }

    @staticmethod
    def _serialize_comment(c: Comment) -> dict[str, Any]:
        return {
            "id": str(c.id),
            "task_id": str(c.task_id),
            "author_id": str(c.author_id),
            "body": c.body,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
            "is_system": c.is_system,
        }

    @staticmethod
    def _serialize_attachment(a: Attachment) -> dict[str, Any]:
        return {
            "id": str(a.id),
            "task_id": str(a.task_id),
            "filename": a.filename,
            "content_type": a.content_type,
            "size_bytes": a.size_bytes,
            "storage_key": a.storage_key,
            "uploaded_by": str(a.uploaded_by),
            "uploaded_at": a.uploaded_at.isoformat(),
        }

    # ------------------------------------------------------------------
    # ZIP construction — builds the archive in a BytesIO buffer
    # ------------------------------------------------------------------

    def _build_zip(self, payload: dict[str, Any], workspace: Workspace) -> bytes:
        """Serialize the full payload dict into a ZIP and return the bytes.

        Everything is written into an in-memory BytesIO buffer; there is no
        streaming to disk.  Works fine for small workspaces.
        """
        buf = io.BytesIO()

        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Manifest
            manifest = {
                "schema_version": "1.0",
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "workspace_id": str(self.workspace_id),
                "workspace_name": workspace.name,
                "record_counts": {
                    section: len(payload[section])
                    for section in self.SECTIONS
                },
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))

            # Per-section JSON files
            for section in self.SECTIONS:
                records = payload[section]
                zf.writestr(
                    f"{section}/{section}.json",
                    json.dumps(records, indent=2, default=str),
                )

            # CSV variants for spreadsheet consumers
            self._write_csv_section(zf, "members", payload["members"])
            self._write_csv_section(zf, "projects", payload["projects"])
            self._write_csv_section(zf, "tasks", payload["tasks"])
            self._write_csv_section(zf, "comments", payload["comments"])
            self._write_csv_section(zf, "attachments", payload["attachments"])

        return buf.getvalue()

    @staticmethod
    def _write_csv_section(
        zf: zipfile.ZipFile, section: str, records: list[dict[str, Any]]
    ) -> None:
        if not records:
            return
        csv_buf = io.StringIO()
        fieldnames = list(records[0].keys())
        writer = csv.DictWriter(csv_buf, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
        zf.writestr(f"{section}/{section}.csv", csv_buf.getvalue())

    # ------------------------------------------------------------------
    # Progress helpers
    # ------------------------------------------------------------------

    def _update_progress(self, pct: int, message: str) -> None:
        self.export_record.progress_pct = pct
        self.export_record.progress_message = message
        self.db.commit()
        logger.debug("Export progress %d%% — %s", pct, message)
