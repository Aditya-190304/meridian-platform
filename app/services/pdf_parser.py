"""PDF parsing service for the Meridian Platform document processing pipeline.

Uses PyMuPDF for page rendering and text extraction, and lxml for
parsing XMP/XML metadata embedded in PDF streams.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from lxml import etree

logger = logging.getLogger(__name__)

# Optional dependency: PyMuPDF (fitz). Imported lazily to avoid import-time
# errors in environments where the C extension isn't available.
try:
    import fitz  # PyMuPDF
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False
    logger.warning(
        "PyMuPDF (fitz) not available — PDF parsing will be degraded"
    )

# Optional Pillow import for thumbnail generation.
try:
    from PIL import Image as PILImage
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False
    logger.warning("Pillow not available — PDF thumbnail generation disabled")


THUMBNAIL_MAX_DIM = 512  # px, longest edge
THUMBNAIL_DPI = 96


@dataclass
class PDFParseResult:
    """Structured output from parsing a single PDF file."""

    page_count: int = 0
    word_count: int = 0
    image_count: int = 0
    full_text: str = ""
    thumbnail_bytes: Optional[bytes] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


class PDFParser:
    """Parses PDF files to extract text, images, and XMP metadata.

    Public interface:
        parse(path) -> PDFParseResult
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self, local_path: str) -> PDFParseResult:
        """Parse a PDF file and return structured extraction results.

        Args:
            local_path: Absolute filesystem path to the PDF.

        Returns:
            A :class:`PDFParseResult` populated with text, metadata,
            an optional thumbnail, and extraction statistics.
        """
        result = PDFParseResult()

        if not FITZ_AVAILABLE:
            result.warnings.append("PyMuPDF unavailable; skipping PDF extraction")
            return result

        logger.info("Opening PDF: %s", local_path)
        doc = fitz.open(local_path)

        try:
            result.page_count = len(doc)
            result.metadata = self._extract_metadata(doc)
            result.full_text, result.word_count = self._extract_text(doc)
            result.image_count = self._count_images(doc)
            result.thumbnail_bytes = self._render_thumbnail(doc)
        finally:
            doc.close()

        logger.info(
            "PDF parsed: %d pages, %d words, %d images",
            result.page_count,
            result.word_count,
            result.image_count,
        )
        return result

    # ------------------------------------------------------------------
    # Text extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(doc: "fitz.Document") -> tuple[str, int]:
        """Extract plain text from all pages and return (text, word_count)."""
        pages_text: List[str] = []

        for page_num, page in enumerate(doc):
            blocks = page.get_text("blocks")
            page_lines: List[str] = []

            for block in blocks:
                # Block tuple layout: (x0, y0, x1, y1, text, block_no, block_type)
                if block[6] == 0:  # block_type 0 = text
                    raw = block[4].strip()
                    if raw:
                        page_lines.append(raw)

            pages_text.append("\n".join(page_lines))
            logger.debug("Page %d: extracted %d text blocks", page_num + 1, len(page_lines))

        full_text = "\n\n".join(pages_text)
        word_count = len(re.findall(r"\b\w+\b", full_text))
        return full_text, word_count

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------

    def _extract_metadata(self, doc: "fitz.Document") -> Dict[str, Any]:
        """Extract document metadata from the PDF info dict and XMP stream."""
        info = doc.metadata or {}

        metadata: Dict[str, Any] = {
            "title": info.get("title", ""),
            "author": info.get("author", ""),
            "subject": info.get("subject", ""),
            "creator": info.get("creator", ""),
            "producer": info.get("producer", ""),
            "creation_date": info.get("creationDate", ""),
            "mod_date": info.get("modDate", ""),
            "encrypted": doc.is_encrypted,
            "pdf_version": doc.pdf_version() if hasattr(doc, "pdf_version") else "",
        }

        # Try to enrich metadata from the embedded XMP stream.
        xmp_metadata = self._parse_xmp(doc)
        if xmp_metadata:
            metadata["xmp"] = xmp_metadata

        return metadata

    @staticmethod
    def _parse_xmp(doc: "fitz.Document") -> Dict[str, str]:
        """Parse the XMP metadata stream embedded in the PDF, if present.

        Uses lxml for robust XML parsing of the XMP packet.
        """
        xmp_result: Dict[str, str] = {}

        try:
            xmp_raw = doc.get_xml_metadata()
        except AttributeError:
            # Older PyMuPDF versions use xmp_metadata property.
            xmp_raw = getattr(doc, "xmp_metadata", None)

        if not xmp_raw:
            return xmp_result

        try:
            # Parse with lxml — resolves namespace prefixes automatically.
            root = etree.fromstring(
                xmp_raw.encode("utf-8") if isinstance(xmp_raw, str) else xmp_raw
            )

            ns_dc = "http://purl.org/dc/elements/1.1/"
            ns_xmp = "http://ns.adobe.com/xap/1.0/"

            title_el = root.find(f".//{{{ns_dc}}}title")
            if title_el is not None and title_el.text:
                xmp_result["dc_title"] = title_el.text.strip()

            desc_el = root.find(f".//{{{ns_dc}}}description")
            if desc_el is not None and desc_el.text:
                xmp_result["dc_description"] = desc_el.text.strip()

            create_el = root.find(f".//{{{ns_xmp}}}CreateDate")
            if create_el is not None and create_el.text:
                xmp_result["xmp_create_date"] = create_el.text.strip()

            modify_el = root.find(f".//{{{ns_xmp}}}ModifyDate")
            if modify_el is not None and modify_el.text:
                xmp_result["xmp_modify_date"] = modify_el.text.strip()

            logger.debug("XMP metadata parsed: %d fields", len(xmp_result))
        except etree.XMLSyntaxError as exc:
            logger.warning("XMP parse error (non-fatal): %s", exc)

        return xmp_result

    # ------------------------------------------------------------------
    # Image counting
    # ------------------------------------------------------------------

    @staticmethod
    def _count_images(doc: "fitz.Document") -> int:
        """Count the total number of raster images across all pages."""
        total = 0
        for page in doc:
            total += len(page.get_images(full=False))
        return total

    # ------------------------------------------------------------------
    # Thumbnail generation
    # ------------------------------------------------------------------

    @staticmethod
    def _render_thumbnail(doc: "fitz.Document") -> Optional[bytes]:
        """Render the first page as a JPEG thumbnail using PyMuPDF + Pillow.

        Returns raw JPEG bytes, or None if either library is unavailable
        or the document has no pages.
        """
        if not PILLOW_AVAILABLE or len(doc) == 0:
            return None

        page = doc[0]
        mat = fitz.Matrix(THUMBNAIL_DPI / 72, THUMBNAIL_DPI / 72)
        pixmap = page.get_pixmap(matrix=mat, alpha=False)

        # Convert PyMuPDF pixmap to Pillow Image for resizing.
        img = PILImage.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

        # Constrain to THUMBNAIL_MAX_DIM on the longest edge.
        img.thumbnail((THUMBNAIL_MAX_DIM, THUMBNAIL_MAX_DIM), PILImage.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue()
