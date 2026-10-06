"""Template filters for OSPREY projects."""
from __future__ import annotations

import bleach
import markdown as md
from django import template
from django.utils.safestring import mark_safe

register = template.Library()


# A conservative allowlist. Enough for a real README, nothing scary.
_ALLOWED_TAGS = [
    "a", "abbr", "blockquote", "br", "code", "div", "em", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "img", "li", "ol", "p", "pre", "span", "strong",
    "table", "tbody", "td", "th", "thead", "tr", "ul",
]
_ALLOWED_ATTRS = {
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title"],
    "abbr": ["title"],
    "code": ["class"],
    "span": ["class"],
    "div": ["class"],
}
_ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


@register.filter(name="markdown")
def render_markdown(value: str) -> str:
    """Render a Markdown string as sanitized HTML."""
    if not value:
        return ""
    html = md.markdown(
        value,
        extensions=["extra", "sane_lists", "tables", "fenced_code", "nl2br"],
        output_format="html5",
    )
    cleaned = bleach.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        protocols=_ALLOWED_PROTOCOLS,
        strip=True,
    )
    # Force rel="noopener" on outbound links.
    cleaned = bleach.linkify(
        cleaned,
        callbacks=[
            lambda attrs, new=False: {**attrs, (None, "rel"): "noopener"},
        ],
        skip_tags=["pre", "code"],
    )
    return mark_safe(cleaned)


def _project_image_prefixes(project) -> tuple[str, ...]:
    from django.conf import settings

    base = settings.MEDIA_URL.rstrip("/")
    # Current layout keys by id; images uploaded before 2026-10-06 used the slug.
    return (f"{base}/projects/{project.pk}/images/", f"{base}/projects/{project.slug}/images/")


@register.filter(name="project_markdown")
def render_project_markdown(value: str, project) -> str:
    """Markdown for a project README. Images must be the project's own
    uploads; anything else is dropped, so a README can't hotlink a tracking
    pixel or another site's image (decided 2026-10-06)."""
    if not value:
        return ""
    prefixes = _project_image_prefixes(project)

    def img_attr(tag, name, val):
        if name == "src":
            return any(val.startswith(p) for p in prefixes)
        return name in ("alt", "title")

    html = md.markdown(
        value,
        extensions=["extra", "sane_lists", "tables", "fenced_code", "nl2br"],
        output_format="html5",
    )
    attrs = dict(_ALLOWED_ATTRS)
    attrs["img"] = img_attr
    cleaned = bleach.clean(html, tags=_ALLOWED_TAGS, attributes=attrs, protocols=_ALLOWED_PROTOCOLS, strip=True)
    # An <img> whose src was refused is left with no src; drop it.
    import re

    cleaned = re.sub(r"<img(?![^>]*\ssrc=)[^>]*>", "", cleaned)
    cleaned = bleach.linkify(
        cleaned,
        callbacks=[lambda attrs, new=False: {**attrs, (None, "rel"): "noopener"}],
        skip_tags=["pre", "code"],
    )
    return mark_safe(cleaned)
