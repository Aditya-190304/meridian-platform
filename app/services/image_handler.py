"""Image handling service for the Meridian Platform document processing pipeline.

Handles standalone image uploads (JPEG, PNG, TIFF, WebP): generates
thumbnails, extracts EXIF/image metadata, and provides an OCR stub
for future Tesseract integration.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from PIL import ExifTags, Image, UnidentifiedImageError
from PIL.TiffImagePlugin import IFDRational

logger = logging.getLogger(__name__)

THUMBNAIL_SIZE = (512, 512)  # max (width, height) for generated thumbnails
JPEG_QUALITY = 85

# Feature flag — set ENABLE_OCR=1 in environment to activate the Tesseract path.
ENABLE_OCR = os.getenv("ENABLE_OCR", "0") == "1"


@dataclass
class ImageHandleResult:
    """Structured output from processing a single image file."""

    width: int = 0
    height: int = 0
    format: str = ""
    mode: str = ""
    thumbnail_bytes: Optional[bytes] = None
    ocr_text: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: list = field(default_factory=list)


class ImageHandler:
    """Processes standalone image uploads: thumbnail generation and metadata extraction.

    Public interface:
        handle(path) -> ImageHandleResult
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def handle(self, local_path: str) -> ImageHandleResult:
        """Process an image file and return structured results.

        Args:
            local_path: Absolute filesystem path to the image.

        Returns:
            An :class:`ImageHandleResult` with thumbnail bytes,
            extracted metadata, and (stubbed) OCR text.
        """
        result = ImageHandleResult()

        try:
            img = Image.open(local_path)
        except UnidentifiedImageError as exc:
            result.warnings.append(f"Cannot identify image file: {exc}")
            logger.warning("Cannot identify image at %s: %s", local_path, exc)
            return result

        logger.info(
            "Processing image: %s, format=%s, size=%dx%d",
            local_path,
            img.format,
            img.width,
            img.height,
        )

        result.width = img.width
        result.height = img.height
        result.format = img.format or "UNKNOWN"
        result.mode = img.mode

        result.metadata = self._extract_metadata(img)
        result.thumbnail_bytes = self._generate_thumbnail(img)

        if ENABLE_OCR:
            result.ocr_text = self._run_ocr(local_path)
        else:
            logger.debug("OCR disabled via ENABLE_OCR flag; skipping for %s", local_path)

        img.close()
        return result

    # ------------------------------------------------------------------
    # Metadata extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_metadata(img: Image.Image) -> Dict[str, Any]:
        """Extract image metadata: basic properties and EXIF tags if present."""
        metadata: Dict[str, Any] = {
            "width": img.width,
            "height": img.height,
            "format": img.format or "",
            "mode": img.mode,
            "is_animated": getattr(img, "is_animated", False),
            "n_frames": getattr(img, "n_frames", 1),
        }

        # Extract EXIF data for JPEG and TIFF images.
        exif_data = img.getexif() if hasattr(img, "getexif") else None
        if exif_data:
            exif_decoded: Dict[str, Any] = {}
            for tag_id, value in exif_data.items():
                tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                # IFDRational values aren't JSON-serialisable; convert to float.
                if isinstance(value, IFDRational):
                    value = float(value)
                elif isinstance(value, bytes):
                    # Skip raw binary blobs (e.g. MakerNote).
                    continue
                exif_decoded[tag_name] = value

            if exif_decoded:
                metadata["exif"] = exif_decoded
                logger.debug("Extracted %d EXIF tags", len(exif_decoded))

        # Extract ICC colour profile name if present.
        icc = img.info.get("icc_profile")
        if icc:
            metadata["has_icc_profile"] = True

        # DPI / resolution info.
        dpi = img.info.get("dpi")
        if dpi:
            try:
                metadata["dpi_x"] = float(dpi[0])
                metadata["dpi_y"] = float(dpi[1])
            except (TypeError, IndexError):
                pass

        return metadata

    # ------------------------------------------------------------------
    # Thumbnail generation
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_thumbnail(img: Image.Image) -> Optional[bytes]:
        """Generate a JPEG thumbnail of the image, constrained to THUMBNAIL_SIZE.

        Converts RGBA/P modes to RGB before JPEG encoding to avoid
        "cannot write mode RGBA as JPEG" errors.

        Returns:
            Raw JPEG bytes of the thumbnail.
        """
        working = img.copy()

        if working.mode in ("RGBA", "P", "LA"):
            background = Image.new("RGB", working.size, (255, 255, 255))
            if working.mode in ("RGBA", "LA"):
                background.paste(working, mask=working.split()[-1])
            else:
                background.paste(working.convert("RGBA"), mask=working.convert("RGBA").split()[-1])
            working = background
        elif working.mode != "RGB":
            working = working.convert("RGB")

        working.thumbnail(THUMBNAIL_SIZE, Image.LANCZOS)

        buf = io.BytesIO()
        working.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        working.close()

        logger.debug(
            "Generated thumbnail: %dx%d JPEG (%d bytes)",
            working.width if not working.fp else 0,
            working.height if not working.fp else 0,
            buf.tell(),
        )
        return buf.getvalue()

    # ------------------------------------------------------------------
    # OCR stub
    # ------------------------------------------------------------------

    @staticmethod
    def _run_ocr(local_path: str) -> str:
        """Run OCR on the image using Tesseract.

        This is currently a stub. Full implementation is tracked in
        MER-847 (Tesseract worker pool integration).

        Args:
            local_path: Path to the image file.

        Returns:
            Extracted text string, or empty string if OCR is unavailable.
        """
        try:
            import pytesseract  # noqa: PLC0415 — optional runtime dep

            text = pytesseract.image_to_string(local_path)
            logger.info(
                "OCR completed for %s: %d characters extracted",
                local_path,
                len(text),
            )
            return text.strip()
        except ImportError:
            logger.warning(
                "pytesseract not installed; OCR skipped for %s. "
                "Set ENABLE_OCR=0 to suppress this warning.",
                local_path,
            )
            return ""
        except Exception as exc:  # noqa: BLE001
            logger.error("OCR failed for %s: %s", local_path, exc)
            return ""
