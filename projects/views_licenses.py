"""Staff page for the license audit findings."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .models import Project
from .sources import license_check


@staff_member_required
def licenses_staff(request):
    """Open license findings across native and registered projects."""
    if request.method == "POST" and request.POST.get("action") == "recheck":
        project = get_object_or_404(Project, pk=request.POST.get("project_id"))
        license_check.audit(project)
        messages.success(request, f"Re-checked “{project.title}”.")
        return redirect(reverse("licenses_staff"))
    rows = [
        p for p in Project.objects.exclude(license_check={}).select_related("created_by").order_by("-license_checked_at")
        if p.license_findings
    ]
    unchecked = license_check.auditable().filter(license_checked_at__isnull=True).count()
    return render(request, "projects/licenses_staff.html", {"rows": rows, "unchecked": unchecked})
