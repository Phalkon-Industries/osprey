"""Avatar image processing.

Uploads are re-encoded once, at save time: EXIF orientation applied,
long edge capped, result stored as WebP. A multi-megabyte camera photo
lands at tens of KB with no visible difference at avatar display sizes.
The compress_avatars management command gives files stored before this
existed the same treatment.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import PurePosixPath

from django.core.files.base import ContentFile
from PIL import Image, ImageOps

MAX_EDGE = 512
QUALITY = 85
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


def process(source, name_hint: str = "avatar") -> ContentFile:
    """Re-encode an image file object as capped WebP.

    Returns a named ContentFile ready to assign to an ImageField.
    Raises whatever Pillow raises on undecodable input; callers turn
    that into a validation error or a skip.
    """
    with Image.open(source) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)
        keeps_alpha = image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        )
        image = image.convert("RGBA" if keeps_alpha else "RGB")
        buffer = BytesIO()
        image.save(buffer, format="WEBP", quality=QUALITY, method=6)
    stem = PurePosixPath(name_hint).stem or "avatar"
    return ContentFile(buffer.getvalue(), name=f"{stem}.webp")


def is_processed(fieldfile) -> bool:
    """True when the stored file already looks like our output."""
    try:
        with fieldfile.open("rb") as handle:
            with Image.open(handle) as image:
                return image.format == "WEBP" and max(image.size) <= MAX_EDGE
    except Exception:  # noqa: BLE001 - unreadable means "reprocess it"
        return False
