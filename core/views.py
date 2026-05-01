from django.shortcuts import render

from projects.models import Project
from projects.views import _visible_projects_for


def home(request):
    qs = _visible_projects_for(request.user)
    featured = qs.order_by("-updated_at")[:6]
    recent = qs.order_by("-created_at")[:8]
    return render(request, "core/home.html", {"featured": featured, "recent": recent})


def about(request):
    return render(request, "core/about.html")


def license_guide(request):
    return render(request, "core/license_guide.html")
