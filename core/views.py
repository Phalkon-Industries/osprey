from pathlib import Path

from django.conf import settings
from django.http import Http404
from django.shortcuts import redirect, render

from projects.views import _visible_projects_for


# Single curated public-facing roadmap file.
_ROADMAP_FILE = Path(settings.BASE_DIR) / "planning" / "roadmap-public.md"


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


def roadmap(request):
    """Public roadmap page rendered from planning/roadmap-public.md."""
    if not _ROADMAP_FILE.is_file():
        raise Http404("Roadmap not available.")
    text = _ROADMAP_FILE.read_text(encoding="utf-8")
    return render(
        request,
        "core/roadmap.html",
        {"title": "OSPREY roadmap", "body_md": text},
    )

