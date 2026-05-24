from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from projects.models import Project

from .models import ProjectReply, ProjectThread


def _get_project(request, slug: str) -> Project:
    project = get_object_or_404(Project, slug=slug)
    if not project.viewable_by(request.user):
        raise Http404
    return project


def thread_index(request, slug: str):
    project = _get_project(request, slug)
    threads = project.threads.select_related("author").prefetch_related("replies")
    return render(
        request,
        "conversations/index.html",
        {
            "project": project,
            "threads": threads,
            "is_maintainer": project.editable_by(request.user),
        },
    )


@login_required
def thread_new(request, slug: str):
    project = _get_project(request, slug)
    if request.method == "POST":
        title = (request.POST.get("title") or "").strip()[:200]
        body = (request.POST.get("body") or "").strip()
        if title and body:
            t = ProjectThread.objects.create(
                project=project, author=request.user, title=title, body=body
            )
            return redirect("conversations:detail", slug=project.slug, thread_id=t.pk)
    return render(
        request,
        "conversations/new.html",
        {"project": project},
    )


def thread_detail(request, slug: str, thread_id: int):
    project = _get_project(request, slug)
    thread = get_object_or_404(ProjectThread, pk=thread_id, project=project)
    if request.method == "POST":
        if not request.user.is_authenticated:
            return redirect("login")
        if thread.is_closed:
            return HttpResponseForbidden("Thread is closed.")
        body = (request.POST.get("body") or "").strip()
        if body:
            ProjectReply.objects.create(thread=thread, author=request.user, body=body)
            try:
                from notifications.models import send as notify

                if thread.author_id and thread.author_id != request.user.id:
                    notify(
                        thread.author,
                        kind="thread_reply",
                        title=f"New reply on '{thread.title[:60]}'",
                        body=body[:200],
                        url=f"/projects/{project.slug}/discussion/{thread.pk}/",
                    )
            except Exception:
                pass
        return redirect("conversations:detail", slug=project.slug, thread_id=thread.pk)
    return render(
        request,
        "conversations/detail.html",
        {
            "project": project,
            "thread": thread,
            "replies": thread.replies.select_related("author"),
            "is_maintainer": project.editable_by(request.user),
        },
    )


@login_required
@require_POST
def mark_answer(request, slug: str, thread_id: int):
    project = _get_project(request, slug)
    if not project.editable_by(request.user):
        return HttpResponseForbidden()
    thread = get_object_or_404(ProjectThread, pk=thread_id, project=project)
    reply_id = request.POST.get("reply_id")
    if reply_id:
        try:
            reply = thread.replies.get(pk=reply_id)
        except ProjectReply.DoesNotExist:
            reply = None
        thread.answer = reply
    else:
        thread.answer = None
    thread.save(update_fields=["answer", "updated_at"])
    return redirect("conversations:detail", slug=project.slug, thread_id=thread.pk)


@login_required
@require_POST
def close_thread(request, slug: str, thread_id: int):
    project = _get_project(request, slug)
    if not project.editable_by(request.user):
        return HttpResponseForbidden()
    thread = get_object_or_404(ProjectThread, pk=thread_id, project=project)
    thread.is_closed = not thread.is_closed
    thread.save(update_fields=["is_closed", "updated_at"])
    return redirect("conversations:detail", slug=project.slug, thread_id=thread.pk)
