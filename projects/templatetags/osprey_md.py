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
