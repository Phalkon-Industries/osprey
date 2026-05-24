from pathlib import Path
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
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


# --- ORCID person-search demo ----------------------------------------------
#
# Throwaway demo of the contributor ORCID-search idea. The user types a name,
# hits a button, and we hit ORCID's public expanded-search API once. The
# explicit button (rather than as-you-type) is intentional: the public API has
# a 24 req/s per-IP rate limit, and we don't want a typeahead to chew through
# it. Gated behind @login_required so an unauthenticated visitor can't burn
# the quota for everyone.

_ORCID_SEARCH_URL = "https://pub.orcid.org/v3.0/expanded-search/"


def _orcid_expanded_search(query: str, rows: int = 10) -> dict:
    """Call ORCID's public expanded-search endpoint.

    Returns a dict with either `results` (list of normalized rows) or `error`.
    """
    if not query:
        return {"results": [], "error": ""}
    params = {"q": query, "rows": str(min(max(rows, 1), 25))}
    url = f"{_ORCID_SEARCH_URL}?{urlencode(params)}"
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "OSPREY-demo/0.1 (https://osprey.phalkon.io)",
        },
    )
    try:
        with urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return {"results": [], "error": f"ORCID API returned HTTP {exc.code}."}
    except URLError as exc:
        return {"results": [], "error": f"Could not reach ORCID: {exc.reason}."}
    except (ValueError, TimeoutError) as exc:
        return {"results": [], "error": f"Unexpected response from ORCID: {exc}."}
    results = []
    for row in payload.get("expanded-result") or []:
        given = (row.get("given-names") or "").strip()
        family = (row.get("family-names") or "").strip()
        institutions = row.get("institution-name") or []
        if not isinstance(institutions, list):
            institutions = [institutions]
        results.append(
            {
                "orcid_id": (row.get("orcid-id") or "").strip(),
                "given_names": given,
                "family_names": family,
                "display_name": " ".join(part for part in [given, family] if part),
                "institutions": [str(name) for name in institutions if name],
                "other_names": row.get("other-name") or [],
                "credit_name": (row.get("credit-name") or "").strip(),
                "email": (row.get("email") or [""])[0] if row.get("email") else "",
            }
        )
    return {"results": results, "error": ""}


@login_required
def orcid_search_demo(request):
    """Demo page for the contributor ORCID-search idea.

    Type a name, hit Search, see ORCID matches. Submit-on-button rather than
    type-ahead so the rate limit stays manageable.
    """
    query = (request.GET.get("q") or "").strip()
    rows = request.GET.get("rows") or "10"
    try:
        rows_int = int(rows)
    except (TypeError, ValueError):
        rows_int = 10
    data: dict = {"results": [], "error": ""}
    if query:
        data = _orcid_expanded_search(query, rows=rows_int)
    return render(
        request,
        "core/orcid_search_demo.html",
        {
            "query": query,
            "rows": rows_int,
            "results": data["results"],
            "error": data["error"],
            "searched": bool(query),
        },
    )
