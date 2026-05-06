from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from allauth.socialaccount.models import SocialAccount

from .forms import (
    FIELD_SUGGESTIONS,
    PROJECT_TYPE_SUGGESTIONS,
    ROLE_SUGGESTIONS,
    ContributionFormSet,
    ProjectForm,
)
from .models import Contribution, Project, ProjectDeposit
from .zenodo import (
    ZenodoError,
    publish_project_deposit,
    sync_project_to_zenodo,
    zenodo_configured,
    zenodo_mode_label,
)


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
            "deposits",
        ),
        slug=slug,
    )
    if not project.viewable_by(request.user):
        raise Http404
    return render(
        request,
        "projects/detail.html",
        {
            "project": project,
            "can_edit": project.editable_by(request.user),
            "zenodo_configured": zenodo_configured(),
            "zenodo_mode_label": zenodo_mode_label(),
            "zenodo_deposit": project.deposits.filter(
                provider=ProjectDeposit.PROVIDER_ZENODO
            ).first(),
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

    ORCID is attached later from the authenticated sign-in account, not from
    this form row.
    """
    try:
        profile = user.profile
    except Exception:
        profile = None
    return [
        {
            "display_name": (
                (profile.display_name if profile else "")
                or user.get_full_name()
                or user.get_username()
            ),
            "role": "Project lead",
            "order": 0,
        }
    ]


def _verified_orcid_for(user) -> str:
    if user is None or not user.is_authenticated:
        return ""
    account = SocialAccount.objects.filter(user=user, provider="orcid").first()
    if account is None:
        return ""
    identifier = (account.extra_data or {}).get("orcid-identifier") or {}
    account_orcid = (identifier.get("path") or "").strip()
    if account_orcid:
        return account_orcid
    try:
        profile_orcid = user.profile.orcid_placeholder
    except Exception:
        profile_orcid = ""
    if profile_orcid:
        return profile_orcid
    return ""


def _display_name_for(user) -> str:
    try:
        profile_name = user.profile.display_name
    except Exception:
        profile_name = ""
    return profile_name or user.get_full_name() or user.get_username()


def _attach_verified_submitter(project: Project, user, orcid_id: str) -> None:
    if not orcid_id:
        return
    display_name = _display_name_for(user)
    contribution = project.contributions.filter(user=user).first()
    if contribution is None:
        contribution = project.contributions.filter(orcid_id=orcid_id).first()
    if contribution is None:
        contribution = project.contributions.filter(
            user__isnull=True,
            display_name__iexact=display_name,
        ).first()
    if contribution is None:
        contribution = Contribution(
            project=project,
            display_name=display_name,
            role="Project lead",
            order=project.contributions.count(),
        )
    contribution.user = user
    contribution.orcid_id = orcid_id
    if not contribution.display_name:
        contribution.display_name = display_name
    if not contribution.role:
        contribution.role = "Project lead"
    contribution.save()


@login_required
def project_new(request):
    if request.method == "POST":
        action = request.POST.get("action", "draft")
        verified_orcid = _verified_orcid_for(request.user)
        form = ProjectForm(request.POST)
        formset = ContributionFormSet(request.POST, instance=Project())
        can_publish = action != "publish" or bool(verified_orcid)
        if not can_publish:
            form.add_error(None, "Sign in with ORCID before publishing a project.")
        if form.is_valid() and formset.is_valid() and can_publish:
            project = form.save(commit=False)
            project.created_by = request.user
            project.visibility = _resolve_visibility(
                project, action, is_new=True
            )
            project.save()
            form.save_m2m()
            formset.instance = project
            formset.save()
            _attach_verified_submitter(project, request.user, verified_orcid)
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
            "role_suggestions": ROLE_SUGGESTIONS,
        },
    )


@login_required
def project_edit(request, slug: str):
    project = get_object_or_404(Project, slug=slug)
    if not project.editable_by(request.user):
        raise Http404
    if request.method == "POST":
        action = request.POST.get("action", "save")
        verified_orcid = _verified_orcid_for(request.user)
        form = ProjectForm(request.POST, instance=project)
        formset = ContributionFormSet(request.POST, instance=project)
        can_publish = action != "publish" or bool(verified_orcid)
        if not can_publish:
            form.add_error(None, "Sign in with ORCID before publishing a project.")
        if form.is_valid() and formset.is_valid() and can_publish:
            saved = form.save(commit=False)
            saved.visibility = _resolve_visibility(
                project, action, is_new=False
            )
            saved.save()
            form.save_m2m()
            formset.save()
            _attach_verified_submitter(saved, request.user, verified_orcid)
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
            "role_suggestions": ROLE_SUGGESTIONS,
        },
    )


@login_required
@require_POST
def project_zenodo_sync(request, slug: str):
    project = get_object_or_404(Project, slug=slug)
    if not project.editable_by(request.user):
        raise Http404
    try:
        deposit = sync_project_to_zenodo(project, request.user)
    except ZenodoError as exc:
        messages.error(request, f"Zenodo sync failed: {exc}")
    else:
        messages.success(
            request,
            f"{zenodo_mode_label()} draft synced. Reserved DOI: {deposit.doi or 'not returned yet'}.",
        )
    return redirect(project.get_absolute_url())


@login_required
@require_POST
def project_zenodo_publish(request, slug: str):
    project = get_object_or_404(Project, slug=slug)
    if not project.editable_by(request.user):
        raise Http404
    deposit = project.deposits.filter(provider=ProjectDeposit.PROVIDER_ZENODO).first()
    if deposit is None:
        messages.error(request, "Create a Zenodo draft before publishing.")
        return redirect(project.get_absolute_url())
    try:
        deposit = publish_project_deposit(deposit)
    except ZenodoError as exc:
        messages.error(request, f"Zenodo publish failed: {exc}")
    else:
        messages.success(request, f"{zenodo_mode_label()} record published: {deposit.doi}.")
    return redirect(project.get_absolute_url())
