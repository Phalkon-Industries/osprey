from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.db import models
from django.shortcuts import redirect, render
from django_ratelimit.decorators import ratelimit

from feedback.models import Feedback
from people.models import Profile
from projects.models import Project
from projects.views import _visible_projects_for

def home(request):
    # The homepage is a storefront: public projects only, for everyone.
    # Drafts stay findable on the projects list and the owner's profile.
    qs = _visible_projects_for(request.user).filter(
        visibility=Project.VISIBILITY_PUBLIC, is_staff_hidden=False
    )
    featured = qs.order_by("-updated_at")[:6]
    recent = qs.order_by("-created_at")[:8]
    return render(request, "core/home.html", {"featured": featured, "recent": recent})


def about(request):
    return render(request, "core/about.html")


@ratelimit(key="ip", rate=settings.RATELIMIT_LOGIN_PER_IP, block=True)
def login(request):
    if request.user.is_authenticated:
        return redirect("home")
    return render(
        request,
        "auth/login.html",
        {"next_url": request.GET.get("next", "")},
    )


def how_it_works(request):
    return render(request, "core/how_it_works.html")


def license_guide(request):
    return render(request, "core/license_guide.html")


def markdown_guide(request):
    return render(request, "core/markdown_guide.html")


def lineage_guide(request):
    return render(request, "core/lineage_guide.html")


def registered_projects_guide(request):
    return render(request, "core/registered_projects_guide.html")


def contributor_guidelines(request):
    return render(request, "core/contributor_guidelines.html")


def _signup_gate_on() -> bool:
    from people.models import SignupGate

    return SignupGate.load().enabled


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
            "signup_gate_on": _signup_gate_on(),
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


# --- ORCID person-search ---------------------------------------------------
# The inline ORCID search lives on the contributor formset (see
# projects/views.py:orcid_search_json and projects/orcid_search.py).


def privacy(request):
    return render(request, "core/privacy.html")


@login_required
def settings_home(request):
    """Central settings entry point; lands on the first settings tab."""
    return redirect("people:edit")


@staff_member_required
def signup_gate(request):
    """The staff switch for the sign-up gate. Takes effect on the next sign-in."""
    from django.contrib import messages

    from people.models import SignupGate

    gate = SignupGate.load()
    if request.method == "POST":
        gate.enabled = request.POST.get("enabled") == "1"
        scope = request.POST.get("scope", SignupGate.SCOPE_NEW)
        gate.scope = scope if scope in dict(SignupGate.SCOPE_CHOICES) else SignupGate.SCOPE_NEW
        gate.allowlist = "\n".join(
            line.strip() for line in request.POST.get("allowlist", "").splitlines() if line.strip()
        )
        gate.updated_by = request.user
        gate.save()
        messages.success(request, "Sign-up gate " + ("on." if gate.enabled else "off."))
        return redirect("core:signup_gate")
    return render(
        request,
        "core/signup_gate.html",
        {"gate": gate},
    )
