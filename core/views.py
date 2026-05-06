from pathlib import Path
import re

from django.conf import settings
from django.http import Http404
from django.shortcuts import redirect, render

from projects.models import Project
from projects.views import _visible_projects_for


# Path to the planning/features directory at the repo root.
_FEATURES_DIR = Path(settings.BASE_DIR) / "planning" / "features"
# Slugs valid for /roadmap/<slug>/. Filename without .md.
# Allow lowercase plus the special README index name.
_SLUG_RE = re.compile(r"^(README|[a-z0-9][a-z0-9_-]*)$")


def _read_feature_doc(slug: str) -> str:
    """Read a planning/features/<slug>.md file with path-traversal guard.

    Returns the markdown content, with relative `*.md` links rewritten to
    point at `/roadmap/<other-slug>/` so the rendered HTML stays inside
    the site.
    """
    if not _SLUG_RE.match(slug):
        raise Http404("Unknown roadmap entry.")
    target = (_FEATURES_DIR / f"{slug}.md").resolve()
    # Reject anything outside the features dir (defense in depth).
    try:
        target.relative_to(_FEATURES_DIR.resolve())
    except ValueError as exc:
        raise Http404("Unknown roadmap entry.") from exc
    if not target.is_file():
        raise Http404("Unknown roadmap entry.")
    text = target.read_text(encoding="utf-8")
    # Rewrite `[label](other.md)` and `[label](other.md#frag)` to
    # `[label](/roadmap/other/)` so navigation works on the site.
    def _rewrite(match: re.Match[str]) -> str:
        label = match.group(1)
        target_slug = match.group(2)
        frag = match.group(3) or ""
        if not _SLUG_RE.match(target_slug):
            return match.group(0)
        return f"[{label}](/roadmap/{target_slug}/{frag})"
    text = re.sub(
        r"\[([^\]]+)\]\(([a-z0-9][a-z0-9_-]*)\.md(#[^)]*)?\)",
        _rewrite,
        text,
    )
    return text


def home(request):
    qs = _visible_projects_for(request.user)
    featured = qs.order_by("-updated_at")[:6]
    recent = qs.order_by("-created_at")[:8]
    return render(request, "core/home.html", {"featured": featured, "recent": recent})


def about(request):
    return render(request, "core/about.html")


def login(request):
    if request.user.is_authenticated:
        return redirect("home")
    return render(
        request,
        "auth/login.html",
        {"next_url": request.GET.get("next", "")},
    )


def license_guide(request):
    return render(request, "core/license_guide.html")


def roadmap_index(request):
    """Public roadmap page rendered from planning/features/README.md."""
    text = _read_feature_doc("README")
    return render(
        request,
        "core/roadmap.html",
        {"slug": "README", "title": "OSPREY roadmap", "body_md": text},
    )


def roadmap_entry(request, slug: str):
    """A single feature plan rendered from planning/features/<slug>.md."""
    text = _read_feature_doc(slug)
    # Pull a title from the first H1 if present.
    title_match = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else slug
    return render(
        request,
        "core/roadmap.html",
        {"slug": slug, "title": title, "body_md": text},
    )

