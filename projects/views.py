from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from allauth.socialaccount.models import SocialAccount
from django_ratelimit.decorators import ratelimit

from .forms import (
    FIELD_SUGGESTIONS,
    PROJECT_TYPE_SUGGESTIONS,
    ROLE_SUGGESTIONS,
    ContributionFormSet,
    NewVersionForm,
    ProjectForm,
)
from .models import (
    Citation,
    Contribution,
    Project,
    ProjectAttachment,
    ProjectDeposit,
    Watch,
)
from . import claiming
from . import lineage as lineage_claims
from .models import LineageEdge, ProjectDepositVersion
from .zenodo import (
    ZenodoError,
    publish_new_version_now,
    publish_project_now,
    start_new_version_for_deposit,
    update_published_metadata,
    zenodo_configured,
    zenodo_mode_label,
)

logger = logging.getLogger(__name__)

MAX_ATTACHMENT_BYTES = 500 * 1024 * 1024  # 500 MiB per attached file

# Shown when a publish blows up in a way the Zenodo client didn't wrap.
# The user's form data is already committed by the time publishing starts,
# so the honest message is "your work is safe," not a 500 page.
PUBLISH_CRASH_MESSAGE = (
    "Something unexpected went wrong while publishing. Your work is saved "
    "as a draft; please try publishing again from the project page."
)


def _notify_project_published(project: Project) -> None:
    """Tell staff a project went public; never let it break the publish."""
    try:
        from notifications import events

        events.project_published(project)
    except Exception:
        logger.exception("project_published notification failed")


def _process_lineage(request, project: Project, defer: bool = False) -> None:
    """Create lineage claims from the form's Lineage rows.

    Rows arrive as parallel lineage_target / lineage_relation /
    lineage_version lists; empty targets are skipped. Failures become
    flash messages, never exceptions: a bad lineage row must not eat a
    project save. `defer=True` (the new-version flow) keeps claims
    dormant until that version publishes.
    """
    targets = request.POST.getlist("lineage_target")
    relations = request.POST.getlist("lineage_relation")
    versions = request.POST.getlist("lineage_version")
    rows = []
    for i, raw in enumerate(targets):
        target = raw.strip()
        if not target:
            continue
        relation = relations[i] if i < len(relations) else ""
        version = versions[i] if i < len(versions) else ""
        rows.append((target, relation, version))
    if not rows:
        return
    from django.conf import settings as django_settings
    from django_ratelimit.core import is_ratelimited

    if django_settings.RATELIMIT_ENABLE and is_ratelimited(
        request,
        group="projects.lineage_declare",
        key="user",
        rate=django_settings.RATELIMIT_LINEAGE_DECLARE,
        increment=True,
    ):
        messages.error(
            request, "Too many lineage links in a short time. Try again later."
        )
        return
    for target, relation, version in rows:
        edge, error = lineage_claims.declare(
            project,
            target,
            relation,
            request.user,
            parent_version_id=int(version) if version.isdigit() else None,
            defer=defer,
        )
        if error:
            messages.warning(request, f"Lineage link skipped: {error}")
        else:
            phrase = lineage_claims.relation_phrase(edge.relation)
            if edge.claimed_at is None:
                messages.success(
                    request,
                    f"Noted: this project {phrase} “{edge.parent.title}”. "
                    "The link goes live when you publish.",
                )
            else:
                messages.success(
                    request,
                    f"Linked: this project {phrase} “{edge.parent.title}”.",
                )


def _activate_lineage(project: Project) -> None:
    """Bring dormant lineage claims live after publish; never let it
    break the publish."""
    try:
        lineage_claims.activate_pending(project)
    except Exception:
        logger.exception("lineage activation failed for %s", project.slug)
    try:
        claiming.sweep_on_publish(project)
    except Exception:
        logger.exception("contributor invite sweep failed for %s", project.slug)


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
    qs = qs.filter(is_staff_hidden=False)
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
    data = cache.get(cache_key)
    if data is None:
        data = expanded_search(query, rows=10)
        if not data.get("error"):
            cache.set(cache_key, data, 300)
    # Annotate whether each iD already has an OSPREY account, computed
    # fresh (not cached) so the contributor form can decide between an
    # in-app invite and a one-time email invite.
    results = data.get("results") or []
    ids = [r.get("orcid_id") for r in results if r.get("orcid_id")]
    known = set(
        SocialAccount.objects.filter(
            provider="orcid", uid__in=ids, user__is_active=True
        ).values_list("uid", flat=True)
    )
    payload = dict(data)
    payload["results"] = [
        {**r, "on_osprey": r.get("orcid_id") in known} for r in results
    ]
    return JsonResponse(payload)


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
    is_watching = request.user.is_authenticated and Watch.objects.filter(
        user=request.user, project=project
    ).exists()
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
            "is_watching": is_watching,
            "can_new_version": bool(
                project.publishable_by(request.user)
                and deposit
                and deposit.state == ProjectDeposit.STATE_PUBLISHED
            ),
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


def _osprey_invite_rate_limited(request) -> bool:
    """1/min burst plus an hourly cap on invite-to-OSPREY emails. The
    address is never stored, so re-sending means re-entering it; these
    limits keep that from becoming a harassment channel."""
    if not settings.RATELIMIT_ENABLE:
        return False
    from django_ratelimit.core import is_ratelimited

    burst = is_ratelimited(
        request,
        group="projects.osprey_invite_burst",
        key="user",
        rate="1/m",
        increment=True,
    )
    hourly = is_ratelimited(
        request,
        group="projects.osprey_invite",
        key="user",
        rate=settings.RATELIMIT_OSPREY_INVITE,
        increment=True,
    )
    return burst or hourly


def _send_button_used(request) -> bool:
    """True when the save came from a per-row send button, so the user
    should land back on the form's Contributors tab, not the project
    page."""
    return bool(
        request.POST.get("contrib_confirm")
        or request.POST.get("contrib_send_invite")
    )


def _send_return_url(request, slug: str) -> str:
    """Edit-form URL that reopens the Contributors tab, scrolled to the
    row whose send button was pressed."""
    index = request.POST.get("contrib_confirm") or request.POST.get(
        "contrib_send_invite"
    )
    anchor = f"#contrib-{index}" if index and str(index).isdigit() else ""
    return (
        reverse("projects:edit", args=[slug]) + "?tab=contributors" + anchor
    )


_FORM_TAB_NAMES = {
    "basics",
    "description",
    "contributors",
    "files",
    "details",
    "related",
}


def _section_return_url(request, slug: str) -> str | None:
    """Save-and-continue posts come back to the form on the next tab
    instead of leaving for the project page. None for every other save."""
    if request.POST.get("action") != "save_continue":
        return None
    url = reverse("projects:edit", args=[slug])
    params = []
    tab = request.POST.get("next_tab", "")
    if tab in _FORM_TAB_NAMES:
        params.append(f"tab={tab}")
    # Comma-joined list of sections marked done before the first save;
    # anything that isn't a known tab name is dropped.
    marked = [
        m
        for m in request.POST.get("marked_section", "").split(",")
        if m in _FORM_TAB_NAMES
    ]
    if marked:
        params.append("marked=" + ",".join(marked))
    # A section saved while still incomplete keeps its red cross and
    # missing-fields note across the reload.
    attempted = request.POST.get("attempted_section", "")
    if attempted in _FORM_TAB_NAMES:
        params.append(f"attempted={attempted}")
    return url + ("?" + "&".join(params) if params else "")


def _process_access(request, project: Project, formset) -> None:
    """Owner-only: apply Project Editor checkboxes, fire invites flagged
    for this save, and handle the transfer-ownership pick.

    Everything is keyed by formset index rather than pk, so it works in
    the same request that creates the rows: no save-and-reopen dance.
    Only rows actually rendered in the submitted formset are touched.
    """
    if project.created_by_id != request.user.id:
        return
    editor_indices = set(request.POST.getlist("contrib_editor"))
    confirm_indices = set(request.POST.getlist("contrib_confirm"))
    send_invite_indices = set(request.POST.getlist("contrib_send_invite"))
    if confirm_indices and settings.RATELIMIT_ENABLE:
        from django_ratelimit.core import is_ratelimited

        if is_ratelimited(
            request,
            group="projects.confirm_request",
            key="user",
            rate=settings.RATELIMIT_CONTRIBUTOR_INVITE,
            increment=True,
        ):
            messages.error(
                request,
                "Too many confirmation requests in a short time; try "
                "again later.",
            )
            confirm_indices = set()
    for i, form in enumerate(formset.forms):
        row = form.instance
        if not row.pk:
            continue
        if form.cleaned_data.get("DELETE"):
            continue
        if row.user_id and row.user_id == project.created_by_id:
            continue  # the owner needs no editor flag
        should = str(i) in editor_indices
        if row.editor != should:
            row.editor = should
            row.save(update_fields=["editor"])
            if should and row.claim_status == Contribution.CLAIM_VERIFIED:
                try:
                    from notifications import events

                    events.editor_granted(row, request.user)
                except Exception:
                    logger.exception("editor_granted notification failed")
        if str(i) in confirm_indices:
            error = claiming.request_confirmation(row, request.user)
            if error:
                messages.warning(
                    request,
                    f"Confirmation request for {row.display_name}: {error}",
                )
            else:
                messages.success(
                    request,
                    f"Confirmation requested from {row.display_name}.",
                )
        email = (request.POST.get(f"contrib_invite_email_{i}") or "").strip()
        if str(i) in send_invite_indices and not email:
            messages.warning(
                request,
                f"OSPREY invite for {row.display_name}: enter an email "
                "address first.",
            )
        if str(i) in send_invite_indices and email:
            if _osprey_invite_rate_limited(request):
                messages.error(
                    request,
                    "Too many OSPREY invitations; wait a minute and try "
                    "again.",
                )
            else:
                error = claiming.send_osprey_invite(row, request.user, email)
                if error:
                    messages.warning(
                        request,
                        f"OSPREY invite for {row.display_name}: {error}",
                    )
                else:
                    messages.success(
                        request,
                        f"One-time OSPREY invitation emailed for "
                        f"{row.display_name}. Re-enter the address later "
                        "to send it again.",
                    )
    transfer_to = request.POST.get("transfer_to", "")
    if request.POST.get("cancel_transfer") and project.pending_owner_id:
        project.pending_owner = None
        project.save(update_fields=["pending_owner"])
        messages.info(request, "Ownership transfer cancelled.")
    elif transfer_to.isdigit() and not project.pending_owner_id:
        row = project.contributions.filter(
            pk=int(transfer_to),
            claim_status=Contribution.CLAIM_VERIFIED,
            user__isnull=False,
        ).first()
        if row and row.user_id != project.created_by_id:
            project.pending_owner = row.user
            project.save(update_fields=["pending_owner"])
            try:
                from notifications import events

                events.ownership_transfer_offered(project, request.user)
            except Exception:
                logger.exception("transfer_offered notification failed")
            messages.success(
                request,
                f"Ownership transfer offered to {row.display_name}. It "
                "takes effect when they accept.",
            )


def _contributors_missing_orcid(post) -> list[str]:
    """Names of non-deleted contributor rows lacking an ORCID iD.

    Publish gate (decided 2026-09-04): every contributor needs an
    attached iD, so all credit stays claimable and verifiable. Drafts
    are lax; the check runs on the publish action only.
    """
    try:
        total = int(post.get("contributions-TOTAL_FORMS", "0"))
    except (TypeError, ValueError):
        return []
    missing = []
    for i in range(total):
        if post.get(f"contributions-{i}-DELETE"):
            continue
        name = (post.get(f"contributions-{i}-display_name") or "").strip()
        orcid = (post.get(f"contributions-{i}-orcid_id") or "").strip()
        if name and not orcid:
            missing.append(name)
    return missing


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
    # Attaching the person who is signed in and saving right now needs
    # no invite: it's self-consent by definition.
    contribution.claim_status = Contribution.CLAIM_VERIFIED
    if not contribution.display_name:
        contribution.display_name = display_name
    if not contribution.role:
        contribution.role = "Project lead"
    contribution.save()


@login_required
@ratelimit(
    key="user", rate=settings.RATELIMIT_PROJECT_CREATE, method="POST", block=True
)
def project_new(request):
    if request.method == "POST":
        action = request.POST.get("action", "draft")
        verified_orcid = _verified_orcid_for(request.user)
        form = ProjectForm(request.POST, request.FILES)
        formset = ContributionFormSet(request.POST, instance=Project())
        can_publish = action != "publish" or bool(verified_orcid)
        if not can_publish:
            form.add_error(None, "Sign in with ORCID before publishing a project.")
        if action == "publish" and can_publish:
            missing = _contributors_missing_orcid(request.POST)
            if missing:
                can_publish = False
                form.add_error(
                    None,
                    "Every contributor needs an attached ORCID iD before "
                    f"publishing. Missing for: {', '.join(missing)}.",
                )
        if form.is_valid() and formset.is_valid() and can_publish:
            project = form.save(commit=False)
            project.created_by = request.user
            project.visibility = _resolve_visibility(project, action, is_new=True)
            project.save()
            form.save_m2m()
            formset.instance = project
            formset.save()
            _attach_verified_submitter(project, request.user, verified_orcid)
            _process_access(request, project, formset)
            _process_attachments(request, project)
            _process_lineage(request, project)
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
                except Exception:  # noqa: BLE001 - the draft is already saved;
                    # a publish crash must never become a 500 that eats it.
                    logger.exception(
                        "Unexpected error publishing new project %s", project.slug
                    )
                    project.visibility = Project.VISIBILITY_PRIVATE
                    project.save(update_fields=["visibility"])
                    messages.error(request, PUBLISH_CRASH_MESSAGE)
                else:
                    _activate_lineage(project)
                    _notify_project_published(project)
                    messages.success(
                        request,
                        f"Project published on {zenodo_mode_label()}.",
                    )
            else:
                messages.success(
                    request,
                    f"Draft saved as “{project.title}.” You can find it on your profile page under Your projects.",
                )
            if _send_button_used(request):
                return redirect(_send_return_url(request, project.slug))
            section_url = _section_return_url(request, project.slug)
            if section_url:
                return redirect(section_url)
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
            "can_publish_project": True,
        },
    )


@login_required
def project_edit(request, slug: str):
    project = get_object_or_404(Project, slug=slug)
    if not project.editable_by(request.user):
        raise Http404
    # The contributor list (and everything riding on it: credit, invites,
    # editor grants, transfer) is owner-only. Editors edit content; the
    # formset from their POST is ignored entirely.
    manage_contributors = project.publishable_by(request.user)
    if request.method == "POST":
        action = request.POST.get("action", "save")
        verified_orcid = _verified_orcid_for(request.user)
        form = ProjectForm(request.POST, request.FILES, instance=project)
        if manage_contributors:
            formset = ContributionFormSet(request.POST, instance=project)
        else:
            formset = ContributionFormSet(instance=project)
        can_publish = action != "publish" or bool(verified_orcid)
        if not can_publish:
            form.add_error(None, "Sign in with ORCID before publishing a project.")
        if action == "publish" and not project.publishable_by(request.user):
            can_publish = False
            form.add_error(
                None,
                "Only the project owner can publish. Your changes can "
                "still be saved as a draft.",
            )
        if action == "publish" and can_publish:
            missing = _contributors_missing_orcid(request.POST)
            if missing:
                can_publish = False
                form.add_error(
                    None,
                    "Every contributor needs an attached ORCID iD before "
                    f"publishing. Missing for: {', '.join(missing)}.",
                )
        if (
            form.is_valid()
            and (not manage_contributors or formset.is_valid())
            and can_publish
        ):
            saved = form.save(commit=False)
            was_public = project.visibility == Project.VISIBILITY_PUBLIC
            saved.visibility = _resolve_visibility(project, action, is_new=False)
            saved.save()
            form.save_m2m()
            if manage_contributors:
                formset.save()
                _attach_verified_submitter(saved, request.user, verified_orcid)
                _process_access(request, saved, formset)
            _process_attachments(request, saved)
            _process_lineage(request, saved)
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
                except Exception:  # noqa: BLE001 - edits are already saved;
                    # a publish crash must never become a 500 that eats them.
                    logger.exception(
                        "Unexpected error publishing project %s", saved.slug
                    )
                    saved.visibility = Project.VISIBILITY_PRIVATE
                    saved.save(update_fields=["visibility"])
                    messages.error(request, PUBLISH_CRASH_MESSAGE)
                else:
                    _activate_lineage(saved)
                    _notify_project_published(saved)
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
                except Exception:  # noqa: BLE001 - same rule: never 500 after save.
                    logger.exception(
                        "Unexpected error syncing metadata for %s", saved.slug
                    )
                    messages.warning(
                        request,
                        "Saved on OSPREY, but the Zenodo metadata sync failed "
                        "unexpectedly. Edit and save again to retry the sync.",
                    )
                else:
                    messages.success(
                        request,
                        f"Project updated. Metadata synced to {zenodo_mode_label()}.",
                    )
            else:
                messages.success(request, "Project updated.")
            if _send_button_used(request):
                return redirect(_send_return_url(request, saved.slug))
            section_url = _section_return_url(request, saved.slug)
            if section_url:
                return redirect(section_url)
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
            "can_publish_project": project.publishable_by(request.user),
            "known_orcids": set(
                SocialAccount.objects.filter(
                    provider="orcid",
                    uid__in=[
                        c.orcid_id
                        for c in project.contributions.all()
                        if c.orcid_id
                    ],
                    user__is_active=True,
                ).values_list("uid", flat=True)
            ),
            "verified_contributors": list(
                project.contributions.filter(
                    claim_status=Contribution.CLAIM_VERIFIED,
                    user__isnull=False,
                )
                .exclude(user=project.created_by)
                .select_related("user")
            ),
            "existing_lineage": list(
                project.lineage_parents.exclude(
                    status=LineageEdge.STATUS_WITHDRAWN
                ).select_related("parent", "parent_version", "child_version")
            ),
            "existing_lineage_heading": (
                "Previous version edges"
                if ProjectDepositVersion.objects.filter(
                    deposit__project=project
                ).count() > 1
                else "Existing links"
            ),
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
    if not project.publishable_by(request.user):
        messages.error(
            request, "Only the project owner can publish new versions."
        )
        return redirect(project.get_absolute_url())
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
            # Claims made here belong to the version being built: they
            # stay dormant and go live pinned to it when it publishes.
            _process_lineage(request, project, defer=True)
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
                except Exception:  # noqa: BLE001 - changelog and archive are
                    # already stored; re-render instead of a data-eating 500.
                    logger.exception(
                        "Unexpected error publishing new version of %s", project.slug
                    )
                    messages.error(
                        request,
                        "Something unexpected went wrong while publishing the new "
                        "version. Your changelog and archive are saved; try again.",
                    )
                else:
                    _activate_lineage(project)
                    try:
                        from notifications import events

                        events.new_version_published(project, actor=request.user)
                    except Exception:
                        logger.exception(
                            "new_version_published notification failed"
                        )
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
                except Exception:  # noqa: BLE001 - same rule as the publish branch.
                    logger.exception(
                        "Unexpected error saving new version draft of %s", project.slug
                    )
                    messages.error(
                        request,
                        "Something unexpected went wrong while saving the version "
                        "draft. Your changelog and archive are saved; try again.",
                    )
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
            "lineage_links": list(
                project.lineage_parents.exclude(
                    status=LineageEdge.STATUS_WITHDRAWN
                ).select_related("parent", "parent_version", "child_version")
            ),
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


def _lineage_diagram(project):
    """Layout data for the lineage diagram: project-to-project only.

    Version pins are collapsed (all edges between the same pair in the
    same direction become one line, labeled with each relation present),
    and the view reaches one generation further each way: parents of
    parents and children of children. Cycles are allowed; a project can
    appear in more than one tier. Interim viewer until the real lineage
    graph gets built.
    """
    width, node_w, node_h, tier_gap = 800, 160, 60, 130

    def live_edges(**filt):
        return (
            LineageEdge.objects.filter(claimed_at__isnull=False, **filt)
            .exclude(status=LineageEdge.STATUS_WITHDRAWN)
            .select_related("parent", "child")
        )

    def collapse(edges, side):
        groups = {}
        for edge in edges:
            other = getattr(edge, side)
            group = groups.setdefault(
                other.pk,
                {"project": other, "relations": set(), "disputed": False},
            )
            group["relations"].add(edge.relation)
            if edge.status == LineageEdge.STATUS_DISPUTED:
                group["disputed"] = True
        return list(groups.values())

    def label_for(relations):
        parts = []
        if "derived_from" in relations:
            parts.append("modified into")
        if "uses" in relations:
            parts.append("used in")
        return " · ".join(parts)

    parents = collapse(live_edges(child=project), "parent")
    children = collapse(live_edges(parent=project), "child")

    # One generation further out. Links remember which middle node they
    # attach to; the center project is skipped so a two-step cycle
    # doesn't redraw this project in an outer tier.
    gp_links, gc_links = [], []
    for pnode in parents:
        for group in collapse(live_edges(child=pnode["project"]), "parent"):
            if group["project"].pk != project.pk:
                gp_links.append((group, pnode))
    for cnode in children:
        for group in collapse(live_edges(parent=cnode["project"]), "child"):
            if group["project"].pk != project.pk:
                gc_links.append((group, cnode))

    def dedupe(links):
        row = {}
        for group, _via in links:
            row.setdefault(group["project"].pk, {"project": group["project"]})
        return list(row.values())

    tiers = []
    grandparents = dedupe(gp_links)
    grandchildren = dedupe(gc_links)
    if grandparents:
        tiers.append(("gp", grandparents))
    if parents:
        tiers.append(("p", parents))
    tiers.append(("cur", [{"project": project}]))
    if children:
        tiers.append(("c", children))
    if grandchildren:
        tiers.append(("gc", grandchildren))

    nodes, centers = [], {}
    for tier_index, (tier_name, row) in enumerate(tiers):
        y = 20 + tier_index * tier_gap
        for i, node in enumerate(row):
            cx = round((i + 1) * width / (len(row) + 1))
            centers[(tier_name, node["project"].pk)] = (cx, y)
            nodes.append(
                {
                    "x": cx - node_w // 2,
                    "cx": cx,
                    "y": y,
                    "text_y": y + 25,
                    "sub_y": y + 45,
                    "project": node["project"],
                    "is_current": tier_name == "cur",
                }
            )

    edges = []

    def add_edge(top_key, bottom_key, relations, disputed):
        if top_key not in centers or bottom_key not in centers:
            return
        (x1, y1), (x2, y2) = centers[top_key], centers[bottom_key]
        edges.append(
            {
                "x1": x1,
                "y1": y1 + node_h,
                "x2": x2,
                "y2": y2 - 2,
                "label_x": round((x1 + x2) / 2),
                "label_y": round((y1 + node_h + y2) / 2),
                "label": label_for(relations),
                "disputed": disputed,
            }
        )

    for group, pnode in gp_links:
        add_edge(
            ("gp", group["project"].pk),
            ("p", pnode["project"].pk),
            group["relations"],
            group["disputed"],
        )
    for pnode in parents:
        add_edge(
            ("p", pnode["project"].pk),
            ("cur", project.pk),
            pnode["relations"],
            pnode["disputed"],
        )
    for cnode in children:
        add_edge(
            ("cur", project.pk),
            ("c", cnode["project"].pk),
            cnode["relations"],
            cnode["disputed"],
        )
    for group, cnode in gc_links:
        add_edge(
            ("c", cnode["project"].pk),
            ("gc", group["project"].pk),
            group["relations"],
            group["disputed"],
        )

    return {
        "width": width,
        "height": 20 + (len(tiers) - 1) * tier_gap + node_h + 20,
        "nodes": nodes,
        "edges": edges,
        "has_relatives": bool(parents or children),
    }


def project_lineage(request, slug: str):
    project = get_object_or_404(Project, slug=slug)
    if not project.viewable_by(request.user):
        raise Http404
    show_withdrawn = request.GET.get("withdrawn") == "1"
    outbound = project.lineage_parents.select_related(
        "parent", "parent_version", "child_version", "declared_by"
    ).order_by("-declared_at")
    # Inbound claims from drafts stay invisible until the child publishes.
    inbound = (
        project.lineage_children.filter(claimed_at__isnull=False)
        .select_related("child", "parent_version", "child_version", "declared_by")
        .order_by("-claimed_at")
    )
    has_withdrawn = (
        outbound.filter(status=LineageEdge.STATUS_WITHDRAWN).exists()
        or inbound.filter(status=LineageEdge.STATUS_WITHDRAWN).exists()
    )
    if not show_withdrawn:
        outbound = outbound.exclude(status=LineageEdge.STATUS_WITHDRAWN)
        inbound = inbound.exclude(status=LineageEdge.STATUS_WITHDRAWN)
    parents = list(outbound)
    children = list(inbound)
    can_edit = project.editable_by(request.user)
    return render(
        request,
        "projects/lineage.html",
        {
            "project": project,
            "parents": parents,
            "children": children,
            "diagram": _lineage_diagram(project),
            "can_edit": can_edit,
            "manage": can_edit and request.GET.get("manage") == "1",
            "show_withdrawn": show_withdrawn,
            "has_withdrawn": has_withdrawn,
        },
    )


@login_required
def lineage_lookup(request):
    """Resolve a pasted project link/slug for the form's Lineage tab.

    Returns the project's identity (so the user can confirm they grabbed
    the right one) plus its published versions for the pin dropdown.
    """
    target = request.GET.get("q", "")
    project = lineage_claims.resolve_target(target)
    if project is None or not project.is_public:
        return JsonResponse(
            {
                "found": False,
                "error": "No published OSPREY project found for that link.",
            }
        )
    versions = [
        {
            "id": v.pk,
            "label": f"v{v.version_index}" + (" (latest)" if i == 0 else ""),
        }
        for i, v in enumerate(
            ProjectDepositVersion.objects.filter(
                deposit__project=project
            ).order_by("-version_index")
        )
    ]
    return JsonResponse(
        {
            "found": True,
            "slug": project.slug,
            "title": project.title,
            "summary": (project.summary or "")[:160],
            "versions": versions,
        }
    )


@login_required
def lineage_respond(request, slug: str, edge_id: int):
    """Parent-side accept/dispute on an inbound lineage claim."""
    if request.method != "POST":
        return redirect("projects:lineage", slug=slug)
    project = get_object_or_404(Project, slug=slug)
    edge = get_object_or_404(LineageEdge, pk=edge_id, parent=project)
    error = lineage_claims.respond(
        edge,
        request.user,
        request.POST.get("action", ""),
        request.POST.get("reason", ""),
    )
    if error:
        messages.error(request, error)
    elif edge.status == LineageEdge.STATUS_DISPUTED:
        messages.success(
            request,
            "Dispute recorded. The link stays visible, marked as disputed.",
        )
    else:
        messages.success(request, "Dispute retracted.")
    return redirect("projects:lineage", slug=slug)


@login_required
def lineage_withdraw(request, slug: str, edge_id: int):
    """Child-side withdrawal, behind its own confirmation page.

    Withdrawing notifies the other team and permanently labels a public
    record, so it deliberately isn't a one-click button anywhere.
    """
    project = get_object_or_404(Project, slug=slug)
    edge = get_object_or_404(LineageEdge, pk=edge_id, child=project)
    if not project.editable_by(request.user):
        raise Http404
    if request.method == "POST":
        error = lineage_claims.withdraw(edge, request.user)
        if error:
            messages.error(request, error)
        else:
            messages.success(
                request,
                "Link withdrawn. It stays on record, labeled as withdrawn.",
            )
        return redirect("projects:lineage", slug=slug)
    return render(
        request,
        "projects/lineage_withdraw.html",
        {"project": project, "edge": edge},
    )


@login_required
def contributor_claims(request):
    """The claims page under Settings: pending invites plus accepted
    listings, with the leave/removal actions."""
    if request.method == "POST":
        contribution = get_object_or_404(
            Contribution, pk=request.POST.get("contribution_id")
        )
        action = request.POST.get("action", "")
        error = None
        if action == "accept":
            error = claiming.accept(contribution, request.user)
            if error is None:
                extra = (
                    " You can edit the project."
                    if contribution.editor
                    else ""
                )
                messages.success(
                    request,
                    f"You've accepted the contributor listing on "
                    f"“{contribution.project.title}”.{extra}",
                )
        elif action == "decline":
            report = request.POST.get("report") == "1"
            reason = request.POST.get("reason", "") if report else None
            error = claiming.decline(contribution, request.user, reason)
            if error is None:
                messages.success(
                    request,
                    "Listing declined."
                    + (" Staff have been notified." if report else ""),
                )
        elif action == "leave":
            error = claiming.leave_draft(contribution, request.user)
            if error is None:
                messages.success(request, "You've left the draft.")
        elif action == "request_removal":
            error = claiming.request_removal(
                contribution, request.user, request.POST.get("reason", "")
            )
            if error is None:
                messages.success(
                    request,
                    "Removal requested. Staff will review it and be in touch.",
                )
        else:
            error = "Unknown action."
        if error:
            messages.error(request, error)
        return redirect("contributor_claims")
    return render(
        request,
        "projects/contributor_claims.html",
        {
            "pending": list(claiming.pending_for(request.user)),
            "accepted": list(claiming.verified_for(request.user)),
            "settings_tab": "claims",
        },
    )


@login_required
def ownership_transfer_respond(request, slug: str):
    """The intended new owner accepts or declines a pending transfer."""
    if request.method != "POST":
        return redirect("projects:detail", slug=slug)
    project = get_object_or_404(Project, slug=slug)
    if project.pending_owner_id != request.user.id:
        raise Http404
    old_owner = project.created_by
    accepted = request.POST.get("action") == "accept"
    if accepted:
        project.created_by = request.user
        project.pending_owner = None
        project.save(update_fields=["created_by", "pending_owner"])
        # The previous owner keeps working access as a verified editor;
        # create their row if they never listed themselves.
        if old_owner is not None:
            row = project.contributions.filter(user=old_owner).first()
            if row is None:
                project.contributions.create(
                    user=old_owner,
                    display_name=old_owner.get_full_name()
                    or old_owner.get_username(),
                    role="Previous owner",
                    orcid_id=claiming.orcid_for(old_owner),
                    claim_status=Contribution.CLAIM_VERIFIED,
                    editor=True,
                    order=project.contributions.count(),
                )
            else:
                row.editor = True
                if row.claim_status != Contribution.CLAIM_VERIFIED:
                    row.claim_status = Contribution.CLAIM_VERIFIED
                row.save(update_fields=["editor", "claim_status"])
        messages.success(request, "You now own this project.")
    else:
        project.pending_owner = None
        project.save(update_fields=["pending_owner"])
        messages.info(request, "Transfer declined.")
    try:
        from notifications import events

        events.ownership_transfer_resolved(
            project, old_owner, accepted, actor=request.user
        )
    except Exception:
        logger.exception("transfer_resolved notification failed")
    return redirect("projects:detail", slug=project.slug)


@login_required
def watch_toggle(request, slug: str):
    """Follow or unfollow a project's activity (new versions, wiki pages,
    use reports). Only public projects can be followed, and not by their
    own editors, who already get owner notifications."""
    if request.method != "POST":
        return redirect("projects:detail", slug=slug)
    project = get_object_or_404(Project, slug=slug)
    if not project.viewable_by(request.user):
        raise Http404
    if not project.is_public or project.editable_by(request.user):
        return redirect("projects:detail", slug=project.slug)
    existing = Watch.objects.filter(user=request.user, project=project).first()
    if existing:
        existing.delete()
    else:
        Watch.objects.create(user=request.user, project=project)
    return redirect("projects:detail", slug=project.slug)
