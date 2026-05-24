from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from projects.models import Project
from notifications.models import send as notify

from .forms import WikiPageForm, WikiRevisionReviewForm
from .models import WikiPage, WikiRevision


def _get_project(slug):
    return get_object_or_404(Project, slug=slug)


def _can_view_project(project, user):
    if project.visibility == Project.VISIBILITY_PUBLIC:
        return True
    return project.editable_by(user)


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
        form = WikiPageForm(request.POST, instance=page)
        if form.is_valid():
            title = form.cleaned_data["title"]
            body = form.cleaned_data["body"]
            summary = form.cleaned_data["summary"]

            if requires_approval:
                target_page = page
                if target_page is None:
                    # Create the page row but leave its body blank until approved.
                    target_page = WikiPage.objects.create(
                        project=project,
                        title=title,
                        body="",
                        last_edited_by=request.user,
                    )
                WikiRevision.objects.create(
                    page=target_page,
                    author=request.user,
                    title=title,
                    body=body,
                    summary=summary,
                    status=WikiRevision.STATUS_PENDING,
                )
                # Notify maintainers.
                for c in project.contributions.filter(
                    user__isnull=False
                ).select_related("user"):
                    if c.user_id == request.user.id:
                        continue
                    notify(
                        c.user,
                        kind="wiki_suggestion",
                        title=f"Wiki suggestion on {project.title}",
                        body=f"{request.user.get_username()} suggested an edit to '{title}'.",
                        url=reverse("wiki:review", args=[project.slug]),
                        project_slug=project.slug,
                        page_slug=target_page.slug,
                    )
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
    pending = WikiRevision.objects.filter(
        page__project=project, status=WikiRevision.STATUS_PENDING
    ).select_related("page", "author")
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
        page.title = revision.title
        page.body = revision.body
        page.last_edited_by = revision.author
        page.save()
        revision.status = WikiRevision.STATUS_APPLIED
        if revision.author and revision.author_id != request.user.id:
            notify(
                revision.author,
                kind="wiki_suggestion",
                title=f"Your wiki suggestion was applied on {project.title}",
                url=reverse("wiki:detail", args=[project.slug, page.slug]),
            )
        messages.success(request, "Suggestion applied.")
    else:
        revision.status = WikiRevision.STATUS_REJECTED
        if revision.author and revision.author_id != request.user.id:
            notify(
                revision.author,
                kind="wiki_suggestion",
                title=f"Your wiki suggestion was declined on {project.title}",
                url=reverse("wiki:detail", args=[project.slug, revision.page.slug]),
            )
        messages.info(request, "Suggestion rejected.")

    revision.save()
    return redirect("wiki:review", slug=project.slug)
