from __future__ import annotations

import logging
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

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
from . import search
from . import zenodo_jobs
from . import zenodo_register
from .models import LineageEdge, ProjectDepositVersion, ZenodoJob
from .zenodo import (
    ZenodoError,
    publish_new_version_now,
    publish_project_now,
    start_new_version_for_deposit,
    update_published_metadata,
    discard_draft_depositions,
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


# Publish side effects live with the job runner so they fire when the
# queued job succeeds, not when the request returns.
_notify_project_published = zenodo_jobs.notify_project_published
_activate_lineage = zenodo_jobs.activate_lineage


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


_LICENSE_NOT_SENDABLE = "Choose a license from the list before publishing."


def _license_blocks_publish(form) -> bool:
    """Add a License error when the chosen license can't go to Zenodo.
    Zenodo relabels an open record sent without a license as CC BY 4.0."""
    from .zenodo import zenodo_license_id

    if zenodo_license_id(form.cleaned_data.get("resolved_license", "")):
        return False
    form.add_error("license_choice", _LICENSE_NOT_SENDABLE)
    return True


def _archive_license_conflict(request, project, chosen_license: str, *, field: str = "attachment_files") -> str:
    """Block message when the archive being published carries a license
    that disagrees with the chosen one. Looks at the file in this request
    first, else the pending draft archive."""
    from .licensing import archive_license_conflict

    uploads = request.FILES.getlist(field) if field == "attachment_files" else ([request.FILES[field]] if field in request.FILES else [])
    if uploads:
        return archive_license_conflict(uploads[0], chosen_license)
    if project is not None and project.pk:
        pending = project.attachments.filter(published_to_zenodo=False).exclude(file="").first()
        if pending is not None:
            try:
                pending.file.open("rb")
                return archive_license_conflict(pending.file, chosen_license)
            finally:
                try:
                    pending.file.close()
                except Exception:  # noqa: BLE001
                    pass
    return ""


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
    support = request.GET.get("support", "").strip()
    if support == "as_is":
        qs = qs.filter(provided_as_is=True)
    elif support == "maintained":
        qs = qs.filter(provided_as_is=False)

    if field:
        qs = qs.filter(field__iexact=field)
    if project_type:
        qs = qs.filter(artifact_type__iexact=project_type)
    if institution:
        qs = qs.filter(institution__icontains=institution)
    if tag:
        qs = qs.filter(tags__name=tag)
    qs = search.search(qs.distinct(), q)

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
            "projects": qs,
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
    zenodo_job = None
    zenodo_sync_pending = False
    if project.editable_by(request.user):
        candidate = zenodo_jobs.latest_job(project)
        if candidate is not None and candidate.kind == ZenodoJob.KIND_METADATA_SYNC:
            zenodo_sync_pending = candidate.is_pending
        else:
            zenodo_job = candidate
    return render(
        request,
        "projects/detail.html",
        {
            "project": project,
            "can_edit": project.editable_by(request.user),
            "is_owner": project.publishable_by(request.user),
            "zenodo_configured": zenodo_configured(),
            "zenodo_mode_label": zenodo_mode_label(),
            "zenodo_deposit": deposit,
            "citation_text": _build_citation_text(project, deposit),
            "citation_bibtex": _build_bibtex(project, deposit),
            "osprey_permalink": _osprey_permalink_for(project),
            "registered_deposit": deposit if project.is_registered else None,
            "recent_citations": recent_citations,
            "citations_count": citations_count,
            "is_watching": is_watching,
            "zenodo_job": zenodo_job,
            "zenodo_job_slow": bool(
                zenodo_job and zenodo_job.is_pending
                and (timezone.now() - zenodo_job.created_at).total_seconds() > 600
            ),
            "zenodo_sync_pending": zenodo_sync_pending,
            "can_new_version": bool(
                project.publishable_by(request.user)
                and deposit
                and deposit.state == ProjectDeposit.STATE_PUBLISHED
                and not (zenodo_job and zenodo_job.is_pending)
            ),
        },
    )


def _build_citation_text(project: Project, deposit) -> str:
    """Build a single-line APA-ish citation string for the copy-to-clipboard block.

    Format follows what Zenodo emits, with both OSPREY and Zenodo named:
        Authors. (Year). Title (Version vN). OSPREY · Zenodo. https://doi.org/<doi>
    """
    contributors = list(project.credited_contributions.all()[:5])
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
    # APA lists joint publishers separated by semicolons: OSPREY publishes
    # the record, Zenodo archives it and issues the DOI. No bracketed
    # work-type descriptor: an OSPREY project is usually hardware plus
    # firmware plus docs, no single label fits, and the DOI identifies it.
    parts = [head, title_part, "OSPREY; Zenodo."]
    if doi:
        parts.append(f"https://doi.org/{doi}")
    return " ".join(p for p in parts if p)


def _build_bibtex(project: Project, deposit) -> str:
    """BibTeX for the same record the plain citation describes."""
    contributors = [
        (c.display_name or "").strip()
        for c in project.credited_contributions.all()[:5]
        if (c.display_name or "").strip()
    ]
    authors = " and ".join(contributors) or "OSPREY contributors"
    when = deposit.published_at if deposit and deposit.published_at else project.updated_at
    year = when.year if when else ""
    doi = ""
    version = ""
    if deposit is not None:
        latest = deposit.latest_version
        if latest is not None:
            doi = getattr(latest, "doi", "") or ""
            if getattr(latest, "version_index", None):
                version = f"v{latest.version_index}"
        doi = doi or deposit.doi or deposit.concept_doi or ""
    if not doi and project.doi:
        doi = project.normalized_doi
    key_base = re.sub(r"[^a-z0-9]", "", (contributors[0].split()[-1] if contributors else "osprey").lower()) or "osprey"
    key = f"{key_base}{year}{re.sub(r'[^a-z0-9]', '', project.slug)[:12]}"
    fields = [
        ("author", authors),
        ("title", project.title),
        ("year", str(year) if year else ""),
        ("publisher", "OSPREY; Zenodo"),
        ("version", version),
        ("doi", doi),
        ("url", f"https://doi.org/{doi}" if doi else ""),
        ("note", f"Record on OSPREY: {_osprey_permalink_for(project)}"),
    ]
    body = ",\n".join(f"  {name} = {{{value}}}" for name, value in fields if value)
    return f"@misc{{{key},\n{body}\n}}"


def _osprey_permalink_for(project: Project) -> str:
    base = getattr(settings, "OSPREY_PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}{project.get_permalink()}"


def _resolve_visibility(project: Project, action: str, is_new: bool) -> str:
    """Visibility to store on save.

    Publishing does not flip a project public here any more: the queued
    Zenodo job does that once the DOI exists, so a project is never public
    without its record. A public project never goes back to private
    through the form; staff can adjust in the admin.
    """
    if is_new:
        return Project.VISIBILITY_PRIVATE
    return project.visibility


def _owner_row_indexes(formset, owner, owner_orcid: str) -> set[int]:
    """Positions in the contributor formset that are the owner's own row.

    The owner must stay listed (ownership and credit meet on that row), so
    the form renders those rows without Remove controls instead of quietly
    re-adding the row after save.
    """
    indexes = set()
    for index, form in enumerate(formset.forms):
        instance = getattr(form, "instance", None)
        if instance is not None and instance.pk and owner is not None and instance.user_id == owner.pk:
            indexes.add(index)
            continue
        try:
            value = (form["orcid_id"].value() or "").strip()
        except KeyError:
            value = ""
        if owner_orcid and value == owner_orcid:
            indexes.add(index)
    return indexes


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
    "images",
    "contributors",
    "files",
    "details",
    "related",
}


def _image_context(project) -> dict:
    from . import images as project_images

    return {
        "image_max": project_images.MAX_IMAGES,
        "image_limit_text": project_images.LIMIT_TEXT,
        "image_accept": project_images.ACCEPT_ATTR,
        "project_readme_images": list(project.images.filter(kind="readme")) if project is not None and project.pk else [],
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
        if action == "publish" and can_publish and form.is_valid():
            conflict = _archive_license_conflict(request, None, form.cleaned_data.get("resolved_license", ""))
            if conflict:
                form.add_error(None, conflict)
                can_publish = False
            elif _license_blocks_publish(form):
                can_publish = False
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
                zenodo_jobs.enqueue_publish(project, request.user)
                messages.success(
                    request,
                    f"Publishing \u201c{project.title}.\u201d OSPREY will mint the DOI as "
                    "soon as Zenodo accepts the deposit, usually within a minute. "
                    "You'll get a notification when it's live.",
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
            **_image_context(None),
            "owner_row_indexes": _owner_row_indexes(
                formset, request.user, _verified_orcid_for(request.user)
            ),
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
    manage_contributors = project.publishable_by(request.user) and not project.is_registered
    # The bound form mutates `project` in place, so remember the
    # Zenodo-owned values before it does.
    linked_title, linked_license = project.title, project.license
    if request.method == "POST":
        action = request.POST.get("action", "save")
        if project.is_registered and action == "publish":
            action = "save"
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
        if action == "publish" and can_publish and form.is_valid():
            conflict = _archive_license_conflict(request, project, form.cleaned_data.get("resolved_license", ""))
            if conflict:
                form.add_error(None, conflict)
                can_publish = False
            elif _license_blocks_publish(form):
                can_publish = False
        if (
            form.is_valid()
            and (not manage_contributors or formset.is_valid())
            and can_publish
        ):
            saved = form.save(commit=False)
            was_public = project.visibility == Project.VISIBILITY_PUBLIC
            saved.visibility = _resolve_visibility(project, action, is_new=False)
            if project.is_registered:
                # Title and license belong to the Zenodo record; the form
                # shows them read-only and the record stays the source.
                saved.title = linked_title
                saved.license = linked_license
            saved.save()
            form.save_m2m()
            if manage_contributors:
                formset.save()
                _attach_verified_submitter(saved, request.user, verified_orcid)
                _process_access(request, saved, formset)
            _process_attachments(request, saved)
            _process_lineage(request, saved)
            if action == "publish" and not was_public:
                project = saved
                zenodo_jobs.enqueue_publish(saved, request.user)
                messages.success(
                    request,
                    f"Publishing \u201c{project.title}.\u201d OSPREY will mint the DOI as "
                    "soon as Zenodo accepts the deposit, usually within a minute. "
                    "You'll get a notification when it's live.",
                )
            elif saved.is_registered:
                # The record is theirs; a save here never touches Zenodo.
                messages.success(request, "Project updated.")
            elif was_public and zenodo_configured():
                # Existing public project: the metadata edit is pushed to
                # the Zenodo record by a queued job, so a Zenodo outage
                # can't stall the save.
                zenodo_jobs.enqueue_metadata_sync(saved, request.user)
                messages.success(
                    request,
                    f"Project updated. The {zenodo_mode_label()} record will "
                    "sync within a minute or so.",
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
            "can_publish_project": project.publishable_by(request.user) and project.accepts_publish,
            **_image_context(project),
            "owner_row_indexes": _owner_row_indexes(
                formset, project.created_by, _verified_orcid_for(project.created_by)
            ),
            "zenodo_job_pending": zenodo_jobs.pending_job(project) is not None,
            "registered_deposit": (
                project.deposits.filter(provider=ProjectDeposit.PROVIDER_ZENODO).first()
                if project.is_registered
                else None
            ),
            "zenodo_community": settings.ZENODO_DEFAULT_COMMUNITY,
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


def _new_version_stalled(project, deposit) -> bool:
    """A new version that failed partway: the deposit is off its last
    published version and no job is working on it."""
    return (
        deposit.state in (ProjectDeposit.STATE_DRAFT, ProjectDeposit.STATE_ERROR)
        and deposit.versions.exists()
        and zenodo_jobs.pending_job(project) is None
    )


@login_required
def project_zenodo_new_version_discard(request, slug: str):
    """Drop the unpublished next version and go back to the last published
    one. Nothing is sent to Zenodo: a draft it still holds open is handed
    back, emptied, by the next new version."""
    project = get_object_or_404(Project, slug=slug)
    if not project.publishable_by(request.user):
        raise Http404
    if request.method != "POST":
        return redirect("projects:zenodo_new_version", slug=project.slug)
    deposit = project.deposits.filter(provider=ProjectDeposit.PROVIDER_ZENODO).first()
    latest = deposit.latest_version if deposit else None
    if latest is None:
        raise Http404
    if zenodo_jobs.pending_job(project) is not None:
        messages.error(request, "The new version is being published. Wait for it to finish.")
        return redirect(project.get_absolute_url())
    for attachment in project.attachments.filter(published_to_zenodo=False):
        attachment.file.delete(save=False)
        attachment.delete()
    deposit.deposition_id = latest.deposition_id
    deposit.record_id = latest.record_id
    deposit.doi = latest.doi
    deposit.bucket_url = ""
    deposit.state = ProjectDeposit.STATE_PUBLISHED
    deposit.published_at = latest.published_at
    deposit.last_response = latest.last_response
    deposit.last_error = ""
    deposit.pending_changelog = ""
    deposit.repo_link = ""
    deposit.save()
    project.zenodo_jobs.filter(kind=ZenodoJob.KIND_NEW_VERSION, status=ZenodoJob.STATUS_FAILED).update(
        status=ZenodoJob.STATUS_CANCELLED
    )
    messages.success(request, "Draft version discarded.")
    return redirect(project.get_absolute_url())


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
    if not project.accepts_publish:
        messages.info(
            request,
            "New versions of a registered project are published on Zenodo. "
            "Publish there, then hit Refresh from Zenodo on the edit page.",
        )
        return redirect(project.get_absolute_url())
    deposit = project.deposits.filter(provider=ProjectDeposit.PROVIDER_ZENODO).first()
    if deposit is None or not (
        deposit.state == ProjectDeposit.STATE_PUBLISHED or _new_version_stalled(project, deposit)
    ):
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
        form = NewVersionForm(request.POST, request.FILES, current_license=project.license)
        upload = request.FILES.get("archive")
        # An archive is required unless one is already pending locally.
        if upload is None and pending_attachment is None:
            form.add_error("archive", "Upload a new .zip archive for this version.")
        elif upload is not None and not _looks_like_zip(upload):
            form.add_error("archive", "Only .zip archives are accepted.")
        elif upload is not None and getattr(upload, "size", 0) > MAX_ATTACHMENT_BYTES:
            form.add_error("archive", "Archive exceeds 500 MiB limit.")
        chosen_license = form.data.get("license_choice") or project.license
        if action == "publish" and not form.errors:
            from .zenodo import zenodo_license_id

            conflict = _archive_license_conflict(request, project, chosen_license, field="archive")
            if conflict:
                form.add_error("archive", conflict)
            elif not zenodo_license_id(chosen_license):
                # A legacy license kept as "Current license" has no Zenodo id.
                form.add_error("license_choice", _LICENSE_NOT_SENDABLE)
        if form.is_valid():
            if chosen_license and chosen_license != project.license:
                project.license = chosen_license
                project.save(update_fields=["license"])
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
                zenodo_jobs.enqueue_new_version(
                    project,
                    request.user,
                    changelog=form.cleaned_data["changelog"],
                    repo_link=form.cleaned_data.get("repo_link", ""),
                )
                messages.success(
                    request,
                    "Publishing the new version. OSPREY will mint its DOI as "
                    "soon as Zenodo accepts the deposit, usually within a "
                    "minute. You'll get a notification when it's live.",
                )
                return redirect(project.get_absolute_url())
            # Save draft keeps everything on OSPREY: changelog, repo link,
            # and archive wait here until Publish. Zenodo is not involved
            # until then.
            deposit.pending_changelog = form.cleaned_data["changelog"]
            deposit.repo_link = form.cleaned_data.get("repo_link", "") or ""
            deposit.save(update_fields=["pending_changelog", "repo_link", "updated_at"])
            messages.success(
                request,
                "New version draft saved. Come back to this page to publish it.",
            )
            return redirect(project.get_absolute_url())
    else:
        initial = {}
        if deposit.pending_changelog:
            initial["changelog"] = deposit.pending_changelog
        if deposit.repo_link:
            initial["repo_link"] = deposit.repo_link
        form = NewVersionForm(initial=initial, current_license=project.license)
    return render(
        request,
        "projects/zenodo_new_version.html",
        {
            "project": project,
            "form": form,
            "zenodo_deposit": deposit,
            "zenodo_mode_label": zenodo_mode_label(),
            "pending_attachment": pending_attachment,
            "can_discard": _new_version_stalled(project, deposit) or bool(pending_attachment or deposit.pending_changelog),
            "lineage_links": list(
                project.lineage_parents.exclude(
                    status=LineageEdge.STATUS_WITHDRAWN
                ).select_related("parent", "parent_version", "child_version")
            ),
        },
    )


def _files_from_response(response: dict, record_url: str) -> list[dict]:
    """Normalize Zenodo file entries from either API shape.

    Deposit API (native publishes): filename / filesize / links.download.
    Records API (registered reads): key / size / links.self.
    """
    out = []
    for f in (response or {}).get("files") or []:
        name = f.get("key") or f.get("filename") or ""
        if not name:
            continue
        links = f.get("links") or {}
        url = links.get("download") or (
            f"{record_url}/files/{name}?download=1" if record_url else links.get("self", "")
        )
        out.append({"name": name, "size": f.get("size") or f.get("filesize") or 0, "url": url})
    return out


def _file_groups(project: Project, deposit) -> list[dict]:
    """Per-version file lists for the Files tab, latest first."""
    groups = []
    if deposit is None:
        return groups
    versions = list(deposit.versions.all())
    for i, version in enumerate(versions):
        record_url = version.external_url
        files = _files_from_response(version.last_response, record_url)
        if not files and i == 0:
            # Legacy native publishes stored the response on the deposit only.
            files = _files_from_response(deposit.last_response, record_url)
        if not files and i == 0 and project.attachments.exists():
            files = [{"name": a.filename, "size": a.size_bytes, "url": record_url} for a in project.attachments.all()]
        groups.append({"version": version, "record_url": record_url, "files": files})
    if not versions and deposit.state == ProjectDeposit.STATE_PUBLISHED:
        record_url = deposit.external_url
        files = _files_from_response(deposit.last_response, record_url) or [
            {"name": a.filename, "size": a.size_bytes, "url": record_url} for a in project.attachments.all()
        ]
        groups.append({"version": None, "record_url": record_url, "files": files})
    return groups


def project_files(request, slug: str):
    """Files tab: where the downloads are, for every kind of project."""
    project = get_object_or_404(Project, slug=slug)
    if not project.viewable_by(request.user):
        raise Http404
    deposit = project.deposits.filter(provider=ProjectDeposit.PROVIDER_ZENODO).first()
    published = bool(deposit and deposit.state == ProjectDeposit.STATE_PUBLISHED) or bool(
        deposit and deposit.versions.exists()
    )
    draft_attachments = [] if published else list(project.attachments.all())
    return render(
        request,
        "projects/files.html",
        {
            "project": project,
            "zenodo_deposit": deposit,
            "file_groups": _file_groups(project, deposit) if published else [],
            "draft_attachments": draft_attachments,
            "can_edit": project.editable_by(request.user),
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


@login_required
def zenodo_job_retry(request, slug: str):
    """Owner's Try again on a failed publish or new-version job."""
    project = get_object_or_404(Project, slug=slug)
    if not project.publishable_by(request.user):
        raise Http404
    if request.method != "POST":
        return redirect(project.get_absolute_url())
    job = project.zenodo_jobs.filter(status=ZenodoJob.STATUS_FAILED).order_by("-created_at").first()
    if job is None:
        messages.info(request, "Nothing to retry.")
    else:
        zenodo_jobs.retry(job)
        messages.success(request, "Queued again. You'll get a notification when it goes through.")
    return redirect(project.get_absolute_url())


@staff_member_required
def zenodo_jobs_staff(request):
    """Staff view of the Zenodo queue with retry."""
    if request.method == "POST":
        job = get_object_or_404(ZenodoJob, pk=request.POST.get("job_id"))
        if job.status == ZenodoJob.STATUS_FAILED:
            zenodo_jobs.retry(job)
            messages.success(request, f"Retrying {job}.")
        return redirect("zenodo_jobs")
    jobs = ZenodoJob.objects.select_related("project", "requested_by").order_by(
        "-created_at"
    )[:100]
    from notifications.models import LoopHeartbeat

    return render(
        request,
        "projects/zenodo_jobs.html",
        {"jobs": jobs, "health": zenodo_jobs.health(), "heartbeat": LoopHeartbeat.load()},
    )


@login_required
def contributor_claims_manage(request):
    """One page for leaving drafts and asking staff for removal, acting on
    a selection instead of one row at a time."""
    if request.method == "POST":
        action = request.POST.get("action", "")
        reason = (request.POST.get("reason") or "").strip()[:500]
        ids = [i for i in request.POST.getlist("contribution_ids") if str(i).isdigit()]
        rows = list(
            Contribution.objects.filter(pk__in=ids, user=request.user).select_related("project")
        )
        done, errors = 0, []
        for row in rows:
            if action == "leave":
                if row.project.is_public:
                    continue  # published rows go through staff
                error = claiming.leave_draft(row, request.user)
            elif action == "request_removal":
                if not row.project.is_public:
                    error = claiming.leave_draft(row, request.user)  # drafts need no staff
                else:
                    error = claiming.request_removal(row, request.user, reason)
            else:
                error = "Unknown action."
            if error:
                errors.append(f"{row.project.title}: {error}")
            else:
                done += 1
        if not rows:
            messages.info(request, "Select at least one project.")
        elif action == "leave":
            messages.success(request, f"Left {done} draft{'s' if done != 1 else ''}.")
        else:
            messages.success(
                request,
                f"Removal requested for {done} project{'s' if done != 1 else ''}. "
                "Staff will work with the project team and let you know.",
            )
        for error in errors:
            messages.error(request, error)
        return redirect("contributor_claims")
    return render(
        request,
        "projects/contributor_claims_manage.html",
        {"accepted": list(claiming.verified_for(request.user))},
    )


@login_required
@ratelimit(
    key="user", rate=settings.RATELIMIT_PROJECT_CREATE, method="POST", block=True
)
def project_register(request):
    """Register an existing Zenodo record as an OSPREY project."""
    verified_orcid = _verified_orcid_for(request.user)
    error = ""
    doi = ""
    if request.method == "POST":
        doi = (request.POST.get("doi") or "").strip()
        try:
            project = zenodo_register.register_record(doi, request.user)
        except zenodo_register.RegistrationError as exc:
            error = str(exc)
        else:
            messages.success(
                request,
                f"\u201c{project.title}\u201d is now on OSPREY. Fill in what Zenodo "
                "doesn't have, like the field, type and maturity, then save.",
            )
            return redirect(reverse("projects:edit", args=[project.slug]) + "?tab=basics")
    return render(
        request,
        "projects/register.html",
        {"error": error, "doi": doi, "verified_orcid": verified_orcid},
    )


@login_required
def mute_toggle(request, slug: str):
    """Owner's one-click mute of activity notifications on their project."""
    project = get_object_or_404(Project, slug=slug)
    if not project.publishable_by(request.user):
        raise Http404
    if request.method == "POST":
        project.owner_activity_muted = not project.owner_activity_muted
        project.save(update_fields=["owner_activity_muted"])
        messages.success(request, "Activity muted." if project.owner_activity_muted else "Activity unmuted.")
    return redirect(project.get_absolute_url())


@login_required
def project_delete(request, slug: str):
    """Owner deletes a draft. Published projects have a DOI and stay."""
    project = get_object_or_404(Project, slug=slug)
    if not project.publishable_by(request.user):
        raise Http404
    if project.is_public:
        messages.error(request, "Published projects can't be deleted.")
        return redirect(project.get_absolute_url())
    if ZenodoJob.objects.filter(
        project=project, status__in=ZenodoJob.PENDING_STATUSES
    ).exists():
        messages.error(request, "This draft is being published and can't be deleted right now.")
        return redirect(reverse("projects:edit", args=[project.slug]))
    if request.method == "POST":
        title = project.title
        # CASCADE removes the rows; the files on the media volume need
        # deleting by hand.
        discard_draft_depositions(project)
        for attachment in project.attachments.exclude(file=""):
            attachment.file.delete(save=False)
        from . import images as project_images

        for image in project.images.all():
            project_images.delete_files(image)
        if project.cover_image:
            project.cover_image.delete(save=False)
        project.delete()
        messages.success(request, f"Deleted the draft \u201c{title}\u201d.")
        return redirect(reverse("people:me"))
    return render(request, "projects/delete.html", {"project": project})


@login_required
def zenodo_refresh(request, slug: str):
    """Owner's Refresh from Zenodo on a registered project."""
    project = get_object_or_404(Project, slug=slug)
    if not project.publishable_by(request.user) or not project.is_registered:
        raise Http404
    if request.method != "POST":
        return redirect(reverse("projects:edit", args=[project.slug]))
    try:
        changes = zenodo_register.refresh_registered(project)
    except zenodo_register.RegistrationError as exc:
        messages.error(request, f"Couldn't refresh: {exc}")
    else:
        bits = []
        if changes["versions_added"]:
            bits.append(f"{changes['versions_added']} new version{'s' if changes['versions_added'] != 1 else ''}")
        if changes["creators_added"]:
            bits.append(f"{changes['creators_added']} new author{'s' if changes['creators_added'] != 1 else ''}")
        if changes["fields"]:
            bits.append("updated " + ", ".join(changes["fields"]))
        messages.success(request, "Refreshed from Zenodo" + (": " + "; ".join(bits) + "." if bits else ". Nothing changed."))
    return redirect(reverse("projects:edit", args=[project.slug]))
