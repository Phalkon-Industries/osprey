from __future__ import annotations

import difflib

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST
from django.conf import settings
from django_ratelimit.decorators import ratelimit

from projects.models import Project
from notifications import events

from .forms import WikiPageForm, WikiRevisionReviewForm
from .merge import three_way_merge
from .models import WikiPage, WikiRevision


def _get_project(slug):
    return get_object_or_404(Project, slug=slug)


def _can_view_project(project, user):
    return project.viewable_by(user)


def index(request, slug):
    project = _get_project(slug)
    if not _can_view_project(project, request.user):
        raise Http404
    pages = list(project.wiki_pages.all())
    landing = next((p for p in pages if p.is_landing), None)
    return render(
        request,
        "wiki/index.html",
        {
            "project": project,
            "pages": pages,
            "landing": landing,
            "can_edit": request.user.is_authenticated,
            "is_maintainer": project.editable_by(request.user),
        },
    )


def detail(request, slug, page_slug):
    project = _get_project(slug)
    if not _can_view_project(project, request.user):
        raise Http404
    page = get_object_or_404(WikiPage, project=project, slug=page_slug)
    pending = page.revisions.filter(status=WikiRevision.STATUS_PENDING).select_related(
        "author"
    )
    return render(
        request,
        "wiki/detail.html",
        {
            "project": project,
            "page": page,
            "pending_revisions": pending,
            "pending_count": pending.count(),
            "can_edit": request.user.is_authenticated,
            "is_maintainer": project.editable_by(request.user),
        },
    )


def history(request, slug, page_slug):
    project = _get_project(slug)
    if not _can_view_project(project, request.user):
        raise Http404
    page = get_object_or_404(WikiPage, project=project, slug=page_slug)
    revisions = page.revisions.select_related("author", "reviewed_by")
    return render(
        request,
        "wiki/history.html",
        {
            "project": project,
            "page": page,
            "revisions": revisions,
            "is_maintainer": project.editable_by(request.user),
        },
    )


@login_required
@ratelimit(key="user", rate=settings.RATELIMIT_WIKI_EDIT, method="POST", block=True)
def edit(request, slug, page_slug=None):
    project = _get_project(slug)
    if not _can_view_project(project, request.user):
        raise Http404
    page = None
    if page_slug:
        page = get_object_or_404(WikiPage, project=project, slug=page_slug)

    is_maintainer = project.editable_by(request.user)
    requires_approval = project.wiki_requires_approval and not is_maintainer

    if request.method == "POST":
        # Capture the page state BEFORE binding the form, because
        # ModelForm.is_valid() mutates the instance with the submitted
        # data and would otherwise overwrite our base snapshot.
        base_title_snapshot = page.title if page else ""
        base_body_snapshot = page.body if page else ""
        form = WikiPageForm(request.POST, instance=page)
        if form.is_valid():
            title = form.cleaned_data["title"]
            body = form.cleaned_data["body"]
            summary = form.cleaned_data["summary"]

            if requires_approval:
                target_page = page
                base_body = base_body_snapshot
                base_title = base_title_snapshot
                if target_page is None:
                    # Create the page row but leave its body blank until approved.
                    target_page = WikiPage.objects.create(
                        project=project,
                        title=title,
                        body="",
                        last_edited_by=request.user,
                    )
                revision = WikiRevision.objects.create(
                    page=target_page,
                    author=request.user,
                    title=title,
                    body=body,
                    base_title=base_title,
                    base_body=base_body,
                    summary=summary,
                    status=WikiRevision.STATUS_PENDING,
                )
                events.wiki_suggestion_created(revision, project, target_page)
                messages.success(request, "Suggestion submitted for maintainer review.")
                return redirect(
                    "wiki:detail", slug=project.slug, page_slug=target_page.slug
                )

            # Direct apply path.
            if page is None:
                page = form.save(commit=False)
                page.project = project
                page.last_edited_by = request.user
                page.save()
            else:
                page.title = title
                page.body = body
                page.last_edited_by = request.user
                page.save()
            WikiRevision.objects.create(
                page=page,
                author=request.user,
                title=title,
                body=body,
                base_title=base_title_snapshot,
                base_body=base_body_snapshot,
                summary=summary,
                status=WikiRevision.STATUS_APPLIED,
                reviewed_at=timezone.now(),
                reviewed_by=request.user,
            )
            messages.success(request, "Wiki page saved.")
            return redirect("wiki:detail", slug=project.slug, page_slug=page.slug)
    else:
        form = WikiPageForm(instance=page)

    return render(
        request,
        "wiki/edit.html",
        {
            "project": project,
            "page": page,
            "form": form,
            "requires_approval": requires_approval,
            "is_maintainer": is_maintainer,
        },
    )


@login_required
def review(request, slug):
    project = _get_project(slug)
    if not project.editable_by(request.user):
        return HttpResponseForbidden(
            "Only project maintainers can review wiki suggestions."
        )
    pending_qs = WikiRevision.objects.filter(
        page__project=project, status=WikiRevision.STATUS_PENDING
    ).select_related("page", "author")
    pending = []
    for rev in pending_qs:
        base_body = rev.base_body or ""
        proposed_body = rev.body or ""
        current_body = rev.page.body or ""
        # Diff the suggester actually made: from the body they saw
        # (base) to their proposal. This is the right thing to show a
        # reviewer even when the page has moved on since.
        diff_lines = list(
            difflib.unified_diff(
                base_body.splitlines(),
                proposed_body.splitlines(),
                fromfile="before",
                tofile="after",
                lineterm="",
            )
        )
        is_new_page = not base_body.strip() and not current_body.strip()
        is_rebased = base_body == current_body
        merged_body, had_conflict = three_way_merge(
            base_body, current_body, proposed_body
        )
        # Title: if current title differs from base, prefer current; otherwise use proposed.
        if (rev.base_title or "") == rev.page.title:
            merged_title = rev.title
        else:
            merged_title = rev.page.title
            if rev.title and rev.title != (rev.base_title or ""):
                had_conflict = True
        pending.append(
            {
                "rev": rev,
                "diff_lines": diff_lines,
                "is_new_page": is_new_page,
                "is_rebased": is_rebased,
                "has_conflict": had_conflict,
                "merged_body": merged_body,
                "merged_title": merged_title,
            }
        )
    return render(
        request,
        "wiki/review.html",
        {"project": project, "pending": pending},
    )


@login_required
@require_POST
def review_action(request, slug, revision_id):
    project = _get_project(slug)
    if not project.editable_by(request.user):
        return HttpResponseForbidden(
            "Only project maintainers can review wiki suggestions."
        )
    revision = get_object_or_404(
        WikiRevision,
        pk=revision_id,
        page__project=project,
        status=WikiRevision.STATUS_PENDING,
    )
    form = WikiRevisionReviewForm(request.POST)
    if not form.is_valid():
        return redirect("wiki:review", slug=project.slug)

    action = form.cleaned_data["action"]
    revision.reviewed_at = timezone.now()
    revision.reviewed_by = request.user

    if action == WikiRevisionReviewForm.ACTION_APPLY:
        page = revision.page
        # The reviewer may have tweaked the wording on the accept screen.
        # If they didn't, fall back to the 3-way merged result so other
        # already-applied edits on this page aren't clobbered.
        merged_body, _ = three_way_merge(
            revision.base_body or "", page.body or "", revision.body or ""
        )
        edited_title = form.cleaned_data.get("edited_title") or revision.title
        edited_body = form.cleaned_data.get("edited_body")
        if edited_body is None or edited_body == "":
            edited_body = merged_body
        # Persist the (possibly-edited) text on the revision so the history
        # records what was actually applied, not what was originally suggested.
        revision.title = edited_title
        revision.body = edited_body
        page.title = edited_title
        page.body = edited_body
        page.last_edited_by = revision.author
        page.save()
        revision.status = WikiRevision.STATUS_APPLIED
        if revision.author and revision.author_id != request.user.id:
            events.wiki_suggestion_reviewed(
                revision, project, approved=True, page_slug=page.slug
            )
        messages.success(request, "Suggestion applied.")
    else:
        revision.status = WikiRevision.STATUS_REJECTED
        if revision.author and revision.author_id != request.user.id:
            events.wiki_suggestion_reviewed(
                revision, project, approved=False, page_slug=revision.page.slug
            )
        messages.info(request, "Suggestion rejected.")

    revision.save()
    return redirect("wiki:review", slug=project.slug)
