from pathlib import Path

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db import models
from django.http import Http404
from django.shortcuts import redirect, render

from feedback.models import Feedback
from people.models import Profile
from projects.models import Project
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


@staff_member_required
def staff_dashboard(request):
    """One-page overview for staff: feedback summary plus admin links.

    Surfaces feedback counts by status and the most recent items, plus
    direct links into the Django admin and the feedback inbox.
    """
    feedback_qs = Feedback.objects.select_related("user")
    status_counts = {value: 0 for value, _ in Feedback.STATUS_CHOICES}
    for row in Feedback.objects.values("status").annotate(n=models.Count("id")):
        status_counts[row["status"]] = row["n"]
    status_summary = [
        {"value": value, "label": label, "count": status_counts.get(value, 0)}
        for value, label in Feedback.STATUS_CHOICES
    ]
    recent_feedback = list(feedback_qs.order_by("-created_at")[:10])
    return render(
        request,
        "core/staff_dashboard.html",
        {
            "status_counts": status_counts,
            "status_choices": Feedback.STATUS_CHOICES,
            "status_summary": status_summary,
            "recent_feedback": recent_feedback,
            "totals": {
                "feedback": Feedback.objects.count(),
                "projects": Project.objects.count(),
                "public_projects": Project.objects.filter(
                    visibility=Project.VISIBILITY_PUBLIC
                ).count(),
                "profiles": Profile.objects.count(),
            },
        },
    )
