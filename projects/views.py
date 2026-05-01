from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .forms import (
    FIELD_SUGGESTIONS,
    PROJECT_TYPE_SUGGESTIONS,
    ContributionFormSet,
    ProjectForm,
)
from .lineage import render_lineage_svg
from .models import Project


def _visible_projects_for(user):
    """Queryset of projects the given user is allowed to see in lists."""
    qs = Project.objects.prefetch_related(
        "tags", "images", "contributions__user"
    )
    if user.is_authenticated and user.is_staff:
        return qs
    public_q = Q(visibility=Project.VISIBILITY_PUBLIC)
    if user.is_authenticated:
        return qs.filter(
            public_q | Q(created_by=user) | Q(contributions__user=user)
        ).distinct()
    return qs.filter(public_q)


def _filter_options(values_qs, suggestions):
    """Merge suggestions and existing values into a deduped, sorted list."""
    existing = [v for v in values_qs if v]
    merged = sorted({*existing, *suggestions}, key=lambda s: s.lower())
    return merged


def project_list(request):
    qs = _visible_projects_for(request.user)

    q = request.GET.get("q", "").strip()
    field = request.GET.get("field", "").strip()
    project_type = request.GET.get("project_type", "").strip()
    institution = request.GET.get("institution", "").strip()
    tag = request.GET.get("tag", "").strip()

    if q:
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(summary__icontains=q)
            | Q(readme__icontains=q)
            | Q(slug__icontains=q)
        )
    if field:
        qs = qs.filter(field__iexact=field)
    if project_type:
        qs = qs.filter(artifact_type__iexact=project_type)
    if institution:
        qs = qs.filter(institution__iexact=institution)
    if tag:
        qs = qs.filter(tags__name=tag)

    field_values = (
        Project.objects.exclude(field="").values_list("field", flat=True).distinct()
    )
    type_values = (
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
            "active_field": field,
            "active_project_type": project_type,
            "field_options": _filter_options(field_values, FIELD_SUGGESTIONS),
            "project_type_options": _filter_options(type_values, PROJECT_TYPE_SUGGESTIONS),
        },
    )


def project_detail(request, slug: str):
    project = get_object_or_404(
        Project.objects.prefetch_related(
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


def _resolve_visibility(project: Project, action: str, is_new: bool) -> str:
    """Pick the project's visibility based on which submit button was clicked.

    Once a project is public, the form will not flip it back to private. A
    staff user can still adjust through the Django admin.
    """
    if is_new:
        return Project.VISIBILITY_PUBLIC if action == "publish" else Project.VISIBILITY_PRIVATE
    # Existing project. Allow draft -> public, never public -> draft.
    if project.visibility != Project.VISIBILITY_PUBLIC and action == "publish":
        return Project.VISIBILITY_PUBLIC
    return project.visibility


def _initial_contributors_for(user) -> list[dict]:
    """Pre-populate the contributor formset with the submitter's row.

    If the submitter has an ORCID on their profile, that row is fully filled
    in. Otherwise the row is left mostly blank so the form forces them to
    enter an ORCID before saving.
    """
    try:
        profile = user.profile
    except Exception:
        profile = None
    return [
        {
            "orcid_id": (profile.orcid_placeholder if profile else "") or "",
            "display_name": (
                (profile.display_name if profile else "")
                or user.get_full_name()
                or user.get_username()
            ),
            "role": "Author",
            "order": 0,
        }
    ]


@login_required
def project_new(request):
    if request.method == "POST":
        form = ProjectForm(request.POST)
        formset = ContributionFormSet(request.POST, instance=Project())
        if form.is_valid() and formset.is_valid():
            project = form.save(commit=False)
            project.created_by = request.user
            project.visibility = _resolve_visibility(
                project, request.POST.get("action", "draft"), is_new=True
            )
            project.save()
            form.save_m2m()
            formset.instance = project
            formset.save()
            if project.is_public:
                messages.success(request, "Project published.")
            else:
                messages.success(request, "Draft saved.")
            return redirect(project.get_absolute_url())
    else:
        form = ProjectForm()
        formset = ContributionFormSet(
            instance=Project(), initial=_initial_contributors_for(request.user)
        )
    return render(
        request,
        "projects/form.html",
        {
            "form": form,
            "formset": formset,
            "mode": "new",
            "project": None,
        },
    )


@login_required
def project_edit(request, slug: str):
    project = get_object_or_404(Project, slug=slug)
    if not project.editable_by(request.user):
        raise Http404
    if request.method == "POST":
        form = ProjectForm(request.POST, instance=project)
        formset = ContributionFormSet(request.POST, instance=project)
        if form.is_valid() and formset.is_valid():
            saved = form.save(commit=False)
            saved.visibility = _resolve_visibility(
                project, request.POST.get("action", "save"), is_new=False
            )
            saved.save()
            form.save_m2m()
            formset.save()
            messages.success(request, "Project updated.")
            return redirect(saved.get_absolute_url())
    else:
        form = ProjectForm(instance=project)
        formset = ContributionFormSet(instance=project)
    return render(
        request,
        "projects/form.html",
        {
            "form": form,
            "formset": formset,
            "mode": "edit",
            "project": project,
        },
    )


def project_cite(request, slug: str):
    project = get_object_or_404(
        Project.objects.prefetch_related("contributions__user"),
        slug=slug,
    )
    if not project.viewable_by(request.user):
        raise Http404
    fmt = request.GET.get("format", "bibtex").lower()
    contribs = list(project.contributions.all())
    primary = [c for c in contribs if c.role.lower() in ("author", "maintainer")]
    authors = [c.display_name for c in (primary or contribs)]
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
