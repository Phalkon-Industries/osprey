from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from allauth.socialaccount.models import SocialAccount

from .forms import (
    FIELD_SUGGESTIONS,
    PROJECT_TYPE_SUGGESTIONS,
    ROLE_SUGGESTIONS,
    ContributionFormSet,
    NewVersionForm,
    ProjectForm,
)
from .models import Citation, Contribution, Project, ProjectAttachment, ProjectDeposit
from .zenodo import (
    ZenodoError,
    publish_new_version_now,
    publish_project_now,
    start_new_version_for_deposit,
    update_published_metadata,
    zenodo_configured,
    zenodo_mode_label,
)

MAX_ATTACHMENT_BYTES = 500 * 1024 * 1024  # 500 MiB per attached file


def _looks_like_zip(upload) -> bool:
    name = (getattr(upload, "name", "") or "").lower()
    if name.endswith(".zip"):
        return True
    ctype = (getattr(upload, "content_type", "") or "").lower()
    return ctype in {
        "application/zip",
        "application/x-zip-compressed",
        "multipart/x-zip",
    }


def _process_attachments(request, project: Project) -> None:
    """Apply the project's single zip-archive upload and any draft deletes.

    Only one attachment per project is supported via the form: any
    not-yet-published draft attachment is replaced by the latest upload.
    Already-published-to-Zenodo attachments cannot be deleted here; they
    would have to be removed on Zenodo directly. Non-zip uploads are
    rejected with a flash message.
    """
    delete_ids = request.POST.getlist("attachment_delete")
    if delete_ids:
        ProjectAttachment.objects.filter(
            project=project,
            pk__in=delete_ids,
            published_to_zenodo=False,
        ).delete()
    uploads = request.FILES.getlist("attachment_files")
    if not uploads:
        return
    upload = uploads[0]
    size = getattr(upload, "size", 0)
    if size and size > MAX_ATTACHMENT_BYTES:
        messages.warning(
            request,
            f"Skipped {upload.name}: file exceeds 500 MiB limit.",
        )
        return
    if not _looks_like_zip(upload):
        messages.warning(
            request,
            f"Skipped {upload.name}: only .zip archives are accepted.",
        )
        return
    # Replace any previous draft archive so the project keeps one zip at a time.
    ProjectAttachment.objects.filter(
        project=project, published_to_zenodo=False
    ).delete()
    ProjectAttachment.objects.create(
        project=project,
        file=upload,
        filename=upload.name,
        size_bytes=size,
    )


def _visible_projects_for(user):
    """Queryset of projects the given user is allowed to see in lists."""
    qs = Project.objects.prefetch_related("tags", "images", "contributions__user")
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


@login_required
def orcid_search_json(request):
    """JSON endpoint backing the inline ORCID search on the contributor formset.

    Login-required so the public 24 req/s ORCID rate limit isn't burned by
    unauthenticated traffic. Results are cached for 5 minutes per query.
    """
    from .orcid_search import expanded_search

    query = (request.GET.get("q") or "").strip()
    if not query:
        return JsonResponse({"results": [], "error": ""})
    cache_key = f"orcid-search:{query.lower()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return JsonResponse(cached)
    data = expanded_search(query, rows=10)
    if not data.get("error"):
        cache.set(cache_key, data, 300)
    return JsonResponse(data)


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
        qs = qs.filter(institution__icontains=institution)
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
            "project_type_options": _filter_options(
                type_values, PROJECT_TYPE_SUGGESTIONS
            ),
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
    deposit = project.deposits.filter(provider=ProjectDeposit.PROVIDER_ZENODO).first()
    recent_citations = project.citations.select_related("submitted_by").order_by(
        "-added_at"
    )[:5]
    citations_count = project.citations.count()
    return render(
        request,
        "projects/detail.html",
        {
            "project": project,
            "can_edit": project.editable_by(request.user),
            "zenodo_configured": zenodo_configured(),
            "zenodo_mode_label": zenodo_mode_label(),
            "zenodo_deposit": deposit,
            "citation_text": _build_citation_text(project, deposit),
            "recent_citations": recent_citations,
            "citations_count": citations_count,
        },
    )


def _build_citation_text(project: Project, deposit) -> str:
    """Build a single-line APA-ish citation string for the copy-to-clipboard block.

    Format follows what Zenodo emits, with both OSPREY and Zenodo named:
        Authors. (Year). Title (Version vN). OSPREY · Zenodo. https://doi.org/<doi>
    """
    contributors = list(project.contributions.all()[:5])
    if contributors:
        names = []
        for c in contributors:
            name = (c.display_name or "").strip()
            if not name:
                continue
            names.append(name)
        if len(names) == 0:
            authors = "OSPREY contributors"
        elif len(names) == 1:
            authors = names[0]
        elif len(names) == 2:
            authors = f"{names[0]} & {names[1]}"
        else:
            authors = ", ".join(names[:-1]) + f", & {names[-1]}"
    else:
        authors = "OSPREY contributors"
    when = (
        deposit.published_at if deposit and deposit.published_at else project.updated_at
    )
    year = when.year if when else ""
    title = (project.title or "").strip()
    # Resolve the version label to display in parentheses.
    version_label = ""
    if deposit is not None:
        latest = deposit.latest_version
        if latest is not None and getattr(latest, "version_index", None):
            version_label = f"v{latest.version_index}"
    # Prefer the version-specific DOI when one exists so the citation
    # pins to a specific snapshot; fall back to the concept DOI.
    doi = ""
    if deposit is not None:
        latest = deposit.latest_version
        if latest is not None and getattr(latest, "doi", ""):
            doi = latest.doi
        elif deposit.doi:
            doi = deposit.doi
        elif deposit.concept_doi:
            doi = deposit.concept_doi
    if not doi and project.doi:
        doi = project.normalized_doi
    if year:
        head = f"{authors} ({year})."
    else:
        head = f"{authors}."
    title_part = f"{title} (Version {version_label})." if version_label else f"{title}."
    parts = [head, title_part, "OSPREY \u00b7 Zenodo."]
    if doi:
        parts.append(f"https://doi.org/{doi}")
    return " ".join(p for p in parts if p)


def _resolve_visibility(project: Project, action: str, is_new: bool) -> str:
    """Pick the project's visibility based on which submit button was clicked.

    Once a project is public, the form will not flip it back to private. A
    staff user can still adjust through the Django admin.
    """
    if is_new:
        return (
            Project.VISIBILITY_PUBLIC
            if action == "publish"
            else Project.VISIBILITY_PRIVATE
        )
    # Existing project. Allow draft -> public, never public -> draft.
    if project.visibility != Project.VISIBILITY_PUBLIC and action == "publish":
        return Project.VISIBILITY_PUBLIC
    return project.visibility


def _initial_contributors_for(user) -> list[dict]:
    """Pre-populate the contributor formset with the submitter's row.

    Pre-fills ORCID iD and institution affiliation from the signed-in user's
    profile so the submitter does not have to retype information OSPREY
    already has on file.
    """
    try:
        profile = user.profile
    except Exception:
        profile = None
    verified_orcid = _verified_orcid_for(user)
    affiliation = (profile.institution if profile else "") or ""
    # Multi-line institution lists collapse to the first line for the row hint.
    primary_affiliation = affiliation.split("\n")[0].split(",")[0].strip()
    return [
        {
            "display_name": (
                (profile.display_name if profile else "")
                or user.get_full_name()
                or user.get_username()
            ),
            "role": "Project lead",
            "affiliation": primary_affiliation,
            "orcid_id": verified_orcid,
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
        form = ProjectForm(request.POST, request.FILES)
        formset = ContributionFormSet(request.POST, instance=Project())
        can_publish = action != "publish" or bool(verified_orcid)
        if not can_publish:
            form.add_error(None, "Sign in with ORCID before publishing a project.")
        if form.is_valid() and formset.is_valid() and can_publish:
            project = form.save(commit=False)
            project.created_by = request.user
            project.visibility = _resolve_visibility(project, action, is_new=True)
            project.save()
            form.save_m2m()
            formset.instance = project
            formset.save()
            _attach_verified_submitter(project, request.user, verified_orcid)
            _process_attachments(request, project)
            if action == "publish":
                try:
                    publish_project_now(project, request.user)
                except ZenodoError as exc:
                    project.visibility = Project.VISIBILITY_PRIVATE
                    project.save(update_fields=["visibility"])
                    messages.error(
                        request,
                        f"Could not publish on Zenodo: {exc}. Saved as a draft.",
                    )
                else:
                    messages.success(
                        request,
                        f"Project published on {zenodo_mode_label()}.",
                    )
            else:
                messages.success(request, f"Draft saved as “{project.title}.” You can find it on your profile page under Your projects.")
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
        form = ProjectForm(request.POST, request.FILES, instance=project)
        formset = ContributionFormSet(request.POST, instance=project)
        can_publish = action != "publish" or bool(verified_orcid)
        if not can_publish:
            form.add_error(None, "Sign in with ORCID before publishing a project.")
        if form.is_valid() and formset.is_valid() and can_publish:
            saved = form.save(commit=False)
            was_public = project.visibility == Project.VISIBILITY_PUBLIC
            saved.visibility = _resolve_visibility(project, action, is_new=False)
            saved.save()
            form.save_m2m()
            formset.save()
            _attach_verified_submitter(saved, request.user, verified_orcid)
            _process_attachments(request, saved)
            if action == "publish" and not was_public:
                try:
                    publish_project_now(saved, request.user)
                except ZenodoError as exc:
                    saved.visibility = Project.VISIBILITY_PRIVATE
                    saved.save(update_fields=["visibility"])
                    messages.error(
                        request,
                        f"Could not publish on Zenodo: {exc}. Saved as a draft.",
                    )
                else:
                    messages.success(
                        request,
                        f"Project published on {zenodo_mode_label()}.",
                    )
            elif was_public and zenodo_configured():
                # Existing public project: push metadata edits back to the
                # published Zenodo record so the two stay in sync.
                try:
                    update_published_metadata(saved)
                except ZenodoError as exc:
                    messages.warning(
                        request,
                        f"Saved on OSPREY, but could not update Zenodo metadata: {exc}",
                    )
                else:
                    messages.success(
                        request,
                        f"Project updated. Metadata synced to {zenodo_mode_label()}.",
                    )
            else:
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
def project_zenodo_new_version(request, slug: str):
    """Start a new Zenodo version. Supports save-as-draft and publish.

    Save draft only writes locally: the new archive is stored as a
    `ProjectAttachment(published_to_zenodo=False)` and the changelog +
    repo_link are stashed on the existing deposit row. No Zenodo call is
    made, so the request returns immediately even on flaky networks.
    Publish picks up that pending data and pushes everything to Zenodo
    in one go.
    """
    project = get_object_or_404(Project, slug=slug)
    if not project.editable_by(request.user):
        raise Http404
    deposit = project.deposits.filter(provider=ProjectDeposit.PROVIDER_ZENODO).first()
    if deposit is None or deposit.state != ProjectDeposit.STATE_PUBLISHED:
        messages.error(
            request,
            "Publish the project before starting a new version.",
        )
        return redirect(project.get_absolute_url())
    verified_orcid = _verified_orcid_for(request.user)
    if not verified_orcid:
        messages.error(request, "Sign in with ORCID before publishing a new version.")
        return redirect(project.get_absolute_url())
    pending_attachment = (
        project.attachments.filter(published_to_zenodo=False).exclude(file="").first()
    )
    if request.method == "POST":
        action = request.POST.get("action", "draft")
        form = NewVersionForm(request.POST, request.FILES)
        upload = request.FILES.get("archive")
        # An archive is required unless one is already pending locally.
        if upload is None and pending_attachment is None:
            form.add_error("archive", "Upload a new .zip archive for this version.")
        elif upload is not None and not _looks_like_zip(upload):
            form.add_error("archive", "Only .zip archives are accepted.")
        elif upload is not None and getattr(upload, "size", 0) > MAX_ATTACHMENT_BYTES:
            form.add_error("archive", "Archive exceeds 500 MiB limit.")
        if form.is_valid():
            if upload is not None:
                # Replace any prior draft attachment so only the new
                # archive is queued for the new version.
                ProjectAttachment.objects.filter(
                    project=project, published_to_zenodo=False
                ).delete()
                ProjectAttachment.objects.create(
                    project=project,
                    file=upload,
                    filename=upload.name,
                    size_bytes=getattr(upload, "size", 0),
                )
            if action == "publish":
                try:
                    publish_new_version_now(
                        deposit,
                        changelog=form.cleaned_data["changelog"],
                        repo_link=form.cleaned_data.get("repo_link", ""),
                        user=request.user,
                    )
                except ZenodoError as exc:
                    messages.error(request, f"Could not publish new version: {exc}")
                else:
                    messages.success(
                        request,
                        f"New version published on {zenodo_mode_label()}.",
                    )
                    return redirect(project.get_absolute_url())
            else:
                # Save draft: open a Zenodo new-version draft, push the
                # archive + sidecars, then leave it unpublished so the
                # user can review on Zenodo and come back to publish.
                try:
                    start_new_version_for_deposit(
                        deposit,
                        changelog=form.cleaned_data["changelog"],
                        repo_link=form.cleaned_data.get("repo_link", ""),
                        user=request.user,
                    )
                except ZenodoError as exc:
                    messages.error(request, f"Could not save new version draft: {exc}")
                else:
                    messages.success(
                        request,
                        f"New version saved as a {zenodo_mode_label()} draft. "
                        "Review on Zenodo, then come back and publish.",
                    )
                    return redirect(project.get_absolute_url())
    else:
        initial = {}
        if deposit.pending_changelog:
            initial["changelog"] = deposit.pending_changelog
        if deposit.repo_link:
            initial["repo_link"] = deposit.repo_link
        form = NewVersionForm(initial=initial)
    return render(
        request,
        "projects/zenodo_new_version.html",
        {
            "project": project,
            "form": form,
            "zenodo_deposit": deposit,
            "zenodo_mode_label": zenodo_mode_label(),
            "pending_attachment": pending_attachment,
        },
    )


def project_versions(request, slug: str):
    """Public list of every published version of a project."""
    project = get_object_or_404(Project, slug=slug)
    if not project.viewable_by(request.user):
        raise Http404
    deposit = project.deposits.filter(provider=ProjectDeposit.PROVIDER_ZENODO).first()
    versions = []
    if deposit is not None:
        versions = list(deposit.versions.all())
        # Legacy: a published deposit pre-dating the version-history feature
        # has no rows. Synthesize a v1 entry from the deposit itself so the
        # page is never empty for a published project.
        if not versions and deposit.state == ProjectDeposit.STATE_PUBLISHED:
            versions = [
                {
                    "version_index": 1,
                    "doi": deposit.doi,
                    "record_id": deposit.record_id,
                    "external_url": deposit.external_url,
                    "published_at": deposit.published_at,
                    "changelog": "",
                    "repo_link": "",
                    "synthetic": True,
                }
            ]
    return render(
        request,
        "projects/versions.html",
        {
            "project": project,
            "zenodo_deposit": deposit,
            "versions": versions,
            "can_edit": project.editable_by(request.user),
        },
    )


def project_permalink(request, public_id):
    """Permanent OSPREY project URL. Redirects to the current slug-based page."""
    project = get_object_or_404(Project, public_id=public_id)
    if not project.viewable_by(request.user):
        raise Http404
    return redirect(project.get_absolute_url())


def project_citations(request, slug: str):
    project = get_object_or_404(Project, slug=slug)
    if not project.viewable_by(request.user):
        raise Http404
    citations = list(project.citations.select_related("submitted_by", "use_report"))
    by_source = {
        Citation.SOURCE_MAINTAINER: [],
        Citation.SOURCE_USER: [],
        Citation.SOURCE_USE_REPORT: [],
    }
    for cit in citations:
        by_source.setdefault(cit.source, []).append(cit)
    return render(
        request,
        "projects/citations.html",
        {
            "project": project,
            "citations": citations,
            "maintainer_citations": by_source[Citation.SOURCE_MAINTAINER],
            "user_citations": by_source[Citation.SOURCE_USER],
            "use_report_citations": by_source[Citation.SOURCE_USE_REPORT],
            "can_edit": project.editable_by(request.user),
        },
    )


def project_lineage(request, slug: str):
    project = get_object_or_404(Project, slug=slug)
    if not project.viewable_by(request.user):
        raise Http404
    parents = list(
        project.lineage_parents.select_related("parent").order_by("relation")
    )
    children = list(
        project.lineage_children.select_related("child").order_by("relation")
    )
    return render(
        request,
        "projects/lineage.html",
        {
            "project": project,
            "parents": parents,
            "children": children,
        },
    )
