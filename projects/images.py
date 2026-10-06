"""Project images: validation, re-encoding, and limits.

Every upload is rotated to match its camera orientation, stripped of
metadata (including phone GPS), and stored as three WebP sizes at quality
90 (decided 2026-10-06): a thumbnail for cards and the thumbnail strip, a
display size for the main viewer, and a full size for the full-screen
view. Originals are not kept. Animated GIFs stay animated. Uploads are
capped at 50 megapixels across all frames: the byte limit says nothing
about the decoded size.
"""
from __future__ import annotations

import io
import uuid
from dataclasses import dataclass

from django.core.files.base import ContentFile
from PIL import Image, ImageOps, ImageSequence, UnidentifiedImageError

MAX_IMAGES = 20
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 50_000_000  # across every frame of an animation
QUALITY = 90
SIZES = {"thumb": 480, "image": 1600, "full": 2400}
ACCEPTED_FORMATS = {"PNG", "JPEG", "GIF", "WEBP", "MPO"}
ACCEPT_ATTR = "image/png,image/jpeg,image/gif,image/webp"
LIMIT_TEXT = f"PNG, JPEG, GIF or WebP. Up to {MAX_IMAGES} images, {MAX_UPLOAD_BYTES // (1024 * 1024)} MB each."


class ImageRejected(ValueError):
    """The upload can't become a project image; the message is for the user."""


@dataclass
class Processed:
    files: dict[str, ContentFile]  # keyed like SIZES
    width: int
    height: int


def _encode(frames: list[Image.Image], durations: list[int], loop: int, edge: int) -> bytes:
    out = io.BytesIO()
    resized = []
    for frame in frames:
        f = frame.copy()
        f.thumbnail((edge, edge), Image.LANCZOS)
        resized.append(f)
    if len(resized) > 1:
        resized[0].save(
            out, format="WEBP", quality=QUALITY, method=4, save_all=True,
            append_images=resized[1:], duration=durations, loop=loop,
        )
    else:
        resized[0].save(out, format="WEBP", quality=QUALITY, method=6)
    return out.getvalue()


def _normalize(frame: Image.Image) -> Image.Image:
    has_alpha = frame.mode in ("RGBA", "LA") or (frame.mode == "P" and "transparency" in frame.info)
    return frame.convert("RGBA" if has_alpha else "RGB")


def process(upload) -> Processed:
    """Validate and re-encode one uploaded file. Raises ImageRejected."""
    size = getattr(upload, "size", None)
    if size is not None and size > MAX_UPLOAD_BYTES:
        raise ImageRejected(f"{getattr(upload, 'name', 'That file')} is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    try:
        upload.seek(0)
    except Exception:  # noqa: BLE001
        pass
    data = upload.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageRejected(f"{getattr(upload, 'name', 'That file')} is over {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    try:
        image = Image.open(io.BytesIO(data))
        fmt = (image.format or "").upper()
        if fmt not in ACCEPTED_FORMATS:
            raise ImageRejected("Only PNG, JPEG, GIF and WebP images are accepted.")
        animated = getattr(image, "is_animated", False) and getattr(image, "n_frames", 1) > 1
        # Checked from the header, before any pixels are decoded: a solid
        # PNG a few KB long can hold hundreds of megapixels.
        frame_count = image.n_frames if animated else 1
        if frame_count * image.width * image.height > MAX_PIXELS:
            raise ImageRejected(f"{getattr(upload, 'name', 'That file')} is over {MAX_PIXELS // 1_000_000} megapixels.")
        if animated:
            frames, durations = [], []
            for frame in ImageSequence.Iterator(image):
                frames.append(_normalize(frame))
                durations.append(int(frame.info.get("duration", 100)) or 100)
            loop = int(image.info.get("loop", 0))
        else:
            image.load()
            frames, durations, loop = [_normalize(ImageOps.exif_transpose(image))], [0], 0
    except ImageRejected:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageRejected("That file isn't an image OSPREY can read.") from exc
    width, height = frames[0].size
    key = uuid.uuid4().hex[:12]
    files = {}
    for name, edge in SIZES.items():
        files[name] = ContentFile(_encode(frames, durations, loop, edge), name=f"{key}-{name}.webp")
    shown = frames[0].copy()
    shown.thumbnail((SIZES["image"], SIZES["image"]))
    return Processed(files=files, width=shown.width, height=shown.height)


def store(project, upload, *, kind: str, caption: str = ""):
    """Create a ProjectImage from an upload, enforcing the per-project limit."""
    from django.db.models import Max

    from .models import ProjectImage

    if project.images.count() >= MAX_IMAGES:
        raise ImageRejected(f"A project can have up to {MAX_IMAGES} images.")
    processed = process(upload)
    next_order = (project.images.aggregate(m=Max("order"))["m"] or 0) + 1
    img = ProjectImage(
        project=project, kind=kind, caption=caption[:300], order=next_order,
        width=processed.width, height=processed.height,
    )
    img.thumb.save(processed.files["thumb"].name, processed.files["thumb"], save=False)
    img.image.save(processed.files["image"].name, processed.files["image"], save=False)
    img.full.save(processed.files["full"].name, processed.files["full"], save=False)
    img.save()
    return img


def delete_files(img) -> None:
    for field in (img.thumb, img.image, img.full):
        if field:
            try:
                field.delete(save=False)
            except Exception:  # noqa: BLE001 - a missing file shouldn't block deletion
                pass


# --- README captions -------------------------------------------------------
# A README image's caption is the text in its Markdown brackets. The tile's
# caption field and the brackets are kept in sync (decided 2026-10-06).

import re as _re


def markdown_caption(caption: str) -> str:
    """Caption text safe inside ![...]: no brackets, no line breaks."""
    return _re.sub(r"\s+", " ", _re.sub(r"[\[\]\r\n]+", " ", caption or "")).strip()


def _ref_re(url: str):
    return _re.compile(r"!\[([^\]]*)\]\(\s*" + _re.escape(url) + r"\s*\)")


def set_readme_caption(readme: str, url: str, caption: str) -> str:
    """Rewrite the brackets of every reference to `url` in the README."""
    return _ref_re(url).sub(lambda m: f"![{markdown_caption(caption)}]({url})", readme or "")


def readme_caption(readme: str, url: str) -> str | None:
    """The bracket text of the first reference to `url`, or None if unused."""
    m = _ref_re(url).search(readme or "")
    return m.group(1).strip() if m else None


def sync_captions_from_readme(project) -> None:
    """After the README is saved, copy bracket text onto README image captions."""
    for img in project.images.filter(kind="readme"):
        caption = readme_caption(project.readme, img.image.url)
        if caption is not None and caption != img.caption:
            img.caption = caption[:300]
            img.save(update_fields=["caption"])
