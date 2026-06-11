"""Document processing orchestration service for Meridian Platform.

Coordinates the full document processing pipeline: text extraction,
image extraction, metadata parsing, database indexing, and S3 upload.
Designed to run as a background task triggered by file upload events.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
import requests
import yaml
from botocore.exceptions import ClientError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import Document, DocumentStatus
from app.services.image_handler import ImageHandler
from app.services.pdf_parser import PDFParser

logger = logging.getLogger(__name__)

ALLOWED_MIME_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
}

DEFAULT_MAX_FILE_SIZE_MB = 50


@dataclass
class ProcessingResult:
    """Encapsulates the output of a completed document processing job."""

    document_id: str
    tenant_id: str
    status: str
    page_count: int = 0
    word_count: int = 0
    image_count: int = 0
    s3_text_key: Optional[str] = None
    s3_thumbnail_key: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    processed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class DocumentProcessingError(Exception):
    """Raised when the document processing pipeline encounters an unrecoverable error."""


class DocumentProcessor:
    """Orchestrates end-to-end document processing for a single uploaded file.

    Responsibilities:
    - Validate MIME type and file size against tenant config
    - Delegate to PDFParser or ImageHandler based on MIME type
    - Persist extracted text and thumbnails to S3
    - Update the Document record in PostgreSQL
    - Fire a webhook callback on completion or failure
    """

    def __init__(self, db: Session, tenant_id: str) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.s3 = boto3.client(
            "s3",
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )
        self.tenant_config = self._load_tenant_config(tenant_id)
        self.pdf_parser = PDFParser()
        self.image_handler = ImageHandler()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, document_id: str, local_path: str) -> ProcessingResult:
        """Run the full processing pipeline for a single document.

        Args:
            document_id: UUID of the Document record to update.
            local_path: Absolute path to the locally downloaded file.

        Returns:
            A :class:`ProcessingResult` describing the outcome.

        Raises:
            DocumentProcessingError: For unrecoverable pipeline failures.
        """
        logger.info(
            "Starting processing pipeline",
            extra={"document_id": document_id, "tenant_id": self.tenant_id},
        )

        document = self._get_document(document_id)
        result = ProcessingResult(
            document_id=document_id,
            tenant_id=self.tenant_id,
            status="processing",
        )

        try:
            self._validate_file(local_path)
            mime_type = self._detect_mime(local_path)

            if mime_type == "application/pdf":
                self._process_pdf(local_path, result)
            elif mime_type.startswith("image/"):
                self._process_image(local_path, result)
            else:
                raise DocumentProcessingError(f"Unsupported MIME type: {mime_type}")

            result.status = "completed"
            self._update_document(document, result)
            self._fire_webhook(result)
            logger.info(
                "Processing completed",
                extra={"document_id": document_id, "pages": result.page_count},
            )
        except DocumentProcessingError as exc:
            result.status = "failed"
            result.errors.append(str(exc))
            logger.error(
                "Processing failed: %s",
                exc,
                extra={"document_id": document_id},
            )
            self._update_document(document, result)
            self._fire_webhook(result)

        return result

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_tenant_config(self, tenant_id: str) -> Dict[str, Any]:
        """Load per-tenant document processing configuration from S3.

        Config files are small YAML documents stored at
        ``tenants/{tenant_id}/doc_processing.yaml`` in the config bucket.
        Falls back to safe defaults if the file does not exist.
        """
        key = f"tenants/{tenant_id}/doc_processing.yaml"
        try:
            resp = self.s3.get_object(
                Bucket=settings.CONFIG_BUCKET, Key=key
            )
            raw = resp["Body"].read().decode("utf-8")
            config = yaml.safe_load(raw)
            logger.debug("Loaded tenant config from s3://%s/%s", settings.CONFIG_BUCKET, key)
            return config or {}
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchKey":
                logger.debug("No tenant config found; using defaults for tenant %s", tenant_id)
                return {}
            raise

    def _get_document(self, document_id: str) -> Document:
        doc = self.db.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            raise DocumentProcessingError(f"Document {document_id} not found")
        return doc

    def _validate_file(self, local_path: str) -> None:
        path = Path(local_path)
        if not path.exists():
            raise DocumentProcessingError(f"File not found: {local_path}")

        max_mb = self.tenant_config.get("max_file_size_mb", DEFAULT_MAX_FILE_SIZE_MB)
        size_mb = path.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            raise DocumentProcessingError(
                f"File size {size_mb:.1f} MB exceeds tenant limit of {max_mb} MB"
            )

    @staticmethod
    def _detect_mime(local_path: str) -> str:
        mime, _ = mimetypes.guess_type(local_path)
        return mime or "application/octet-stream"

    def _process_pdf(self, local_path: str, result: ProcessingResult) -> None:
        parse_result = self.pdf_parser.parse(local_path)
        result.page_count = parse_result.page_count
        result.word_count = parse_result.word_count
        result.image_count = parse_result.image_count
        result.metadata.update(parse_result.metadata)

        if parse_result.full_text:
            key = self._upload_text(parse_result.full_text, result.document_id)
            result.s3_text_key = key

        if parse_result.thumbnail_bytes:
            key = self._upload_thumbnail(parse_result.thumbnail_bytes, result.document_id)
            result.s3_thumbnail_key = key

    def _process_image(self, local_path: str, result: ProcessingResult) -> None:
        image_result = self.image_handler.handle(local_path)
        result.image_count = 1
        result.metadata.update(image_result.metadata)

        if image_result.thumbnail_bytes:
            key = self._upload_thumbnail(image_result.thumbnail_bytes, result.document_id)
            result.s3_thumbnail_key = key

    def _upload_text(self, text: str, document_id: str) -> str:
        key = f"processed/{self.tenant_id}/{document_id}/extracted_text.txt"
        self.s3.put_object(
            Bucket=settings.DOCUMENTS_BUCKET,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType="text/plain; charset=utf-8",
        )
        logger.debug("Uploaded extracted text to s3://%s/%s", settings.DOCUMENTS_BUCKET, key)
        return key

    def _upload_thumbnail(self, thumbnail_bytes: bytes, document_id: str) -> str:
        key = f"processed/{self.tenant_id}/{document_id}/thumbnail.jpg"
        self.s3.put_object(
            Bucket=settings.DOCUMENTS_BUCKET,
            Key=key,
            Body=thumbnail_bytes,
            ContentType="image/jpeg",
        )
        logger.debug("Uploaded thumbnail to s3://%s/%s", settings.DOCUMENTS_BUCKET, key)
        return key

    def _update_document(self, document: Document, result: ProcessingResult) -> None:
        document.status = DocumentStatus(result.status)
        document.page_count = result.page_count
        document.word_count = result.word_count
        document.image_count = result.image_count
        document.s3_text_key = result.s3_text_key
        document.s3_thumbnail_key = result.s3_thumbnail_key
        document.doc_metadata = result.metadata
        document.processed_at = result.processed_at
        self.db.commit()
        logger.debug("Updated document record %s -> %s", document.id, result.status)

    def _fire_webhook(self, result: ProcessingResult) -> None:
        webhook_url = self.tenant_config.get("webhook_url")
        if not webhook_url:
            return

        payload = {
            "event": "document.processed",
            "document_id": result.document_id,
            "tenant_id": result.tenant_id,
            "status": result.status,
            "page_count": result.page_count,
            "word_count": result.word_count,
            "image_count": result.image_count,
            "processed_at": result.processed_at.isoformat(),
            "errors": result.errors,
        }

        try:
            resp = requests.post(
                webhook_url,
                json=payload,
                timeout=10,
                headers={"X-Meridian-Event": "document.processed"},
            )
            resp.raise_for_status()
            logger.info("Webhook delivered to %s (HTTP %s)", webhook_url, resp.status_code)
        except requests.RequestException as exc:
            logger.warning("Webhook delivery failed: %s", exc)
