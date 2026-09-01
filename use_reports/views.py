from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from notifications import events
from projects.models import Project

from .forms import UseReportForm
from .models import UseReport


def _get_project(slug):
    project = get_object_or_404(Project, slug=slug)
    return project


def index(request, slug):
    project = _get_project(slug)
    if not project.viewable_by(request.user):
        raise Http404
    qs = project.use_reports.filter(visibility=UseReport.VIS_PUBLIC)
    return render(
        request,
        "use_reports/index.html",
        {
            "project": project,
            "use_reports": qs,
            "is_maintainer": project.editable_by(request.user),
            "can_post": request.user.is_authenticated,
        },
    )


@login_required
def new(request, slug):
    project = _get_project(slug)
    if not project.viewable_by(request.user):
        raise Http404
    if request.method == "POST":
        form = UseReportForm(request.POST)
        if form.is_valid():
            report = form.save(commit=False)
            report.project = project
            report.author = request.user
            report.save()
            from projects.models import Citation

            for line in form.citation_lines():
                Citation.objects.create(
                    project=project,
                    text=line[:600],
                    source=Citation.SOURCE_USE_REPORT,
                    submitted_by=request.user,
                    use_report=report,
                )
            events.use_report_created(report)
            messages.success(request, "Thanks for sharing how you used this project.")
            return redirect("use_reports:index", slug=project.slug)
    else:
        form = UseReportForm()
    return render(
        request,
        "use_reports/new.html",
        {"project": project, "form": form},
    )


@login_required
@require_POST
def moderate(request, slug, use_report_id):
    """Staff-only hide / unhide. Maintainers do not moderate use reports."""
    project = _get_project(slug)
    if not request.user.is_staff:
        return HttpResponseForbidden(
            "Only OSPREY staff can hide or restore a use report. "
            "To flag one for staff review, use the feedback widget."
        )
    report = get_object_or_404(UseReport, pk=use_report_id, project=project)
    action = (request.POST.get("action") or "").strip()
    if action == "hide":
        report.visibility = UseReport.VIS_HIDDEN
    elif action == "show":
        report.visibility = UseReport.VIS_PUBLIC
    else:
        return redirect("use_reports:index", slug=project.slug)
    report.save()
    events.use_report_moderated(report, hidden=(action == "hide"))
    return redirect("use_reports:index", slug=project.slug)
