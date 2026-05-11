"""Cover-image ingestion: download (if URL) and re-encode to WebP."""
from __future__ import annotations

import io
import logging
import uuid
from urllib import error, request

from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

# Downloads larger than this are rejected. Mirrors the form-level cap.
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB
# Re-encode target. Quality=100 keeps the image visually identical; we only
# resize to bring file size down. Method=6 is Pillow's slowest/best WebP
# encoder.
MAX_LONG_EDGE = 1200
WEBP_QUALITY = 100


def _open_image(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def _to_webp_bytes(image: Image.Image) -> bytes:
    # Strip EXIF-derived rotation, drop EXIF + ICC.
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA"):
        image = image.convert("RGBA")
    else:
        image = image.convert("RGB")
    image.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, format="WEBP", quality=WEBP_QUALITY, method=6)
    return out.getvalue()


def _fetch_url(url: str) -> bytes | None:
    try:
        req = request.Request(
            url,
            headers={"User-Agent": "OSPREY cover-image fetcher"},
        )
        with request.urlopen(req, timeout=15) as response:
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
            if len(data) > MAX_DOWNLOAD_BYTES:
                logger.warning("Cover image at %s exceeds %d bytes; skipping.", url, MAX_DOWNLOAD_BYTES)
                return None
            return data
    except (error.URLError, error.HTTPError, ValueError) as exc:
        logger.warning("Could not fetch cover image %s: %s", url, exc)
        return None


def process_cover_image(project) -> bool:
    """Re-encode an uploaded or URL cover into a stored WebP file.

    Returns True if `project.cover_image` was updated. The caller is
    responsible for saving the project. Operates in-memory; no temporary
    files on disk.
    """
    source: bytes | None = None
    if project.cover_image and hasattr(project.cover_image, "file"):
        try:
            project.cover_image.open("rb")
            source = project.cover_image.read()
        finally:
            try:
                project.cover_image.close()
            except Exception:  # pragma: no cover - best effort
                pass
        # Skip re-processing already-stored .webp files (e.g. when the user
        # didn't change the upload). We detect by extension.
        existing_name = (project.cover_image.name or "").lower()
        if existing_name.endswith(".webp") and source is not None:
            # Was previously processed by us; nothing to do.
            return False
    elif project.cover_image_url:
        source = _fetch_url(project.cover_image_url)
        if source is None:
            return False
    else:
        return False

    try:
        image = _open_image(source)
    except (UnidentifiedImageError, OSError) as exc:
        logger.warning("Cover image for project %s could not be decoded: %s", project.pk, exc)
        return False

    try:
        encoded = _to_webp_bytes(image)
    except (OSError, ValueError) as exc:
        logger.warning("Cover image for project %s could not be encoded: %s", project.pk, exc)
        return False

    filename = f"{project.slug or 'project'}-{uuid.uuid4().hex[:8]}.webp"
    project.cover_image.save(filename, ContentFile(encoded), save=False)
    return True
