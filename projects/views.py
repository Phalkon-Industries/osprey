from __future__ import annotations

import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ProjectForm
from .lineage import render_lineage_svg
from .models import Contribution, Project


def _visible_projects_for(user):
    """Queryset of projects the given user is allowed to see in lists."""
    qs = Project.objects.select_related("institution").prefetch_related("tags", "images")
    if user.is_authenticated and user.is_staff:
        return qs
    public_q = Q(visibility=Project.VISIBILITY_PUBLIC)
    if user.is_authenticated:
        return qs.filter(
            public_q | Q(created_by=user) | Q(contributions__user=user)
        ).distinct()
    return qs.filter(public_q)


def project_list(request):
    qs = _visible_projects_for(request.user)

    q = request.GET.get("q", "").strip()
    field = request.GET.get("field", "").strip()
    artifact_type = request.GET.get("artifact_type", "").strip()
    institution = request.GET.get("institution", "").strip()
    tag = request.GET.get("tag", "").strip()

    if q:
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(summary__icontains=q)
            | Q(description__icontains=q)
            | Q(slug__icontains=q)
        )
    if field:
        qs = qs.filter(field=field)
    if artifact_type:
        qs = qs.filter(artifact_type=artifact_type)
    if institution:
        qs = qs.filter(institution__short_name=institution)
    if tag:
        qs = qs.filter(tags__name=tag)

    fields = (
        Project.objects.exclude(field="").values_list("field", flat=True).distinct()
    )
    artifact_types = (
        Project.objects.exclude(artifact_type="")
        .values_list("artifact_type", flat=True)
        .distinct()
    )

    return render(
        request,
        "projects/list.html",
        {
            "projects": qs.distinct(),
            "q": q,
            "fields": sorted(fields),
            "artifact_types": sorted(artifact_types),
            "active_field": field,
            "active_artifact_type": artifact_type,
        },
    )


def project_detail(request, slug: str):
    project = get_object_or_404(
        Project.objects.select_related("institution").prefetch_related(
            "artifact_links",
            "contributions__user",
            "tags",
            "images",
            "citations",
            "lineage_parents__parent",
            "lineage_children__child",
        ),
        slug=slug,
    )
    if not project.viewable_by(request.user):
        raise Http404
    lineage_svg = render_lineage_svg(project)
    return render(
        request,
        "projects/detail.html",
        {
            "project": project,
            "lineage_svg": lineage_svg,
            "can_edit": project.editable_by(request.user),
        },
    )


@login_required
def project_new(request):
    if request.method == "POST":
        form = ProjectForm(request.POST)
        if form.is_valid():
            project = form.save(commit=False)
            project.created_by = request.user
            project.save()
            form.save_m2m()
            Contribution.objects.get_or_create(
                project=project,
                user=request.user,
                role="author",
                defaults={"order": 0},
            )
            messages.success(request, "Project created.")
            return redirect(project.get_absolute_url())
    else:
        form = ProjectForm()
    return render(request, "projects/form.html", {"form": form, "mode": "new"})


@login_required
def project_edit(request, slug: str):
    project = get_object_or_404(Project, slug=slug)
    if not project.editable_by(request.user):
        raise Http404
    if request.method == "POST":
        form = ProjectForm(request.POST, instance=project)
        if form.is_valid():
            form.save()
            messages.success(request, "Project updated.")
            return redirect(project.get_absolute_url())
    else:
        form = ProjectForm(instance=project)
    return render(
        request,
        "projects/form.html",
        {"form": form, "mode": "edit", "project": project},
    )


def project_cite(request, slug: str):
    project = get_object_or_404(
        Project.objects.prefetch_related("contributions__user"),
        slug=slug,
    )
    if not project.viewable_by(request.user):
        raise Http404
    fmt = request.GET.get("format", "bibtex").lower()
    authors = [
        c.user.get_full_name() or c.user.get_username()
        for c in project.contributions.all()
        if c.role in ("author", "maintainer")
    ] or [
        c.user.get_full_name() or c.user.get_username()
        for c in project.contributions.all()
    ]
    year = project.created_at.year
    doi = project.placeholder_doi or f"10.demo/{project.slug}"
    url = project.canonical_url or request.build_absolute_uri(project.get_absolute_url())

    if fmt == "csl":
        payload = {
            "type": "article",
            "id": project.slug,
            "title": project.title,
            "author": [{"literal": a} for a in authors],
            "issued": {"date-parts": [[year]]},
            "DOI": doi,
            "URL": url,
        }
        return JsonResponse(payload, json_dumps_params={"indent": 2})

    bib_authors = " and ".join(authors) if authors else "OSPREY contributor"
    entry = (
        f"@misc{{{project.slug},\n"
        f"  title = {{{project.title}}},\n"
        f"  author = {{{bib_authors}}},\n"
        f"  year = {{{year}}},\n"
        f"  doi = {{{doi}}},\n"
        f"  url = {{{url}}},\n"
        f"  note = {{OSPREY demo record}}\n"
        f"}}\n"
    )
    return HttpResponse(entry, content_type="text/plain; charset=utf-8")
