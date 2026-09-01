"""The event layer: every notification OSPREY emits goes through here.

Extensibility contract (see planning/features/notifications-and-email.md):
adding a new event costs one EVENTS entry, one emitter function, and one
call site in the view that causes it. Recipient resolution lives inside
each emitter so emitters can't break each other. The email layer (phase
3) will consult EVENTS[kind] to map an event onto a preference group; a
new kind slots into an existing group and inherits behavior with no new
user settings.

Emitters never raise: a notification failure must not break the action
that caused it.
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.urls import reverse

from .models import Notification, send

logger = logging.getLogger(__name__)

GROUP_PROJECTS = "projects"  # activity on projects you own
GROUP_REPLIES = "replies"  # responses to things you wrote
GROUP_FOLLOWS = "follows"  # people and work you follow (phase 4)
GROUP_ACCOUNT = "account"  # moderation and administrative notices
GROUP_STAFF = "staff"  # staff-only operational events

# kind -> preference group. Every emitted kind must be registered here.
EVENTS = {
    "project_question": GROUP_PROJECTS,
    "use_report": GROUP_PROJECTS,
    "wiki_suggestion": GROUP_PROJECTS,
    "thread_reply": GROUP_REPLIES,
    "answer_accepted": GROUP_REPLIES,
    "use_report_moderated": GROUP_REPLIES,
    "wiki_suggestion_reviewed": GROUP_REPLIES,
    "feedback_reply": GROUP_REPLIES,
    "feedback_submitted": GROUP_STAFF,
    "content_report": GROUP_STAFF,
    "project_published": GROUP_STAFF,
    "project_hidden": GROUP_ACCOUNT,
}


def _emit(user, *, kind, title, body="", url="", dedup_key=None, **payload):
    """Create one notification row, honoring the registry and dedup guard.

    Returns the Notification or None (unknown recipient, self-notification
    already filtered by callers, or a duplicate suppressed).
    """
    if kind not in EVENTS:
        logger.error("Refusing to emit unregistered notification kind %r", kind)
        return None
    if dedup_key:
        exists = Notification.objects.filter(
            user=user,
            kind=kind,
            is_read=False,
            payload__dedup_key=dedup_key,
        ).exists()
        if exists:
            return None
        payload["dedup_key"] = dedup_key
    return send(user, kind=kind, title=title, body=body, url=url, **payload)


def _project_team(project):
    """Who counts as "the project" for activity notifications.

    v1 decision (2026-08-27): the owner only. Widening to verified
    contributors or a real editors model happens here and nowhere else.
    """
    return [project.created_by] if project.created_by_id else []


def _staff():
    return get_user_model().objects.filter(is_staff=True, is_active=True)


def _actor_name(user) -> str:
    return f"@{user.get_username()}" if user else "someone"


# --- conversations -----------------------------------------------------


def thread_created(thread):
    project = thread.project
    url = reverse("conversations:detail", args=[project.slug, thread.pk])
    for user in _project_team(project):
        if user.id == thread.author_id:
            continue
        _emit(
            user,
            kind="project_question",
            title=f"New question on {project.title}",
            body=thread.title[:200],
            url=url,
            project_slug=project.slug,
            thread_id=thread.pk,
        )


def thread_replied(reply):
    """Notify everyone following the thread, minus the person replying."""
    thread = reply.thread
    project = thread.project
    url = reverse("conversations:detail", args=[project.slug, thread.pk])
    subscriber_ids = thread.subscriptions.filter(subscribed=True).exclude(
        user_id=reply.author_id
    ).values_list("user_id", flat=True)
    users = get_user_model().objects.filter(pk__in=list(subscriber_ids))
    for user in users:
        _emit(
            user,
            kind="thread_reply",
            title=f"New reply on '{thread.title[:60]}'",
            body=reply.body[:200],
            url=url,
            project_slug=project.slug,
            thread_id=thread.pk,
        )


def answer_accepted(reply):
    thread = reply.thread
    project = thread.project
    if not reply.author_id or reply.author_id == thread.author_id:
        return
    _emit(
        reply.author,
        kind="answer_accepted",
        title=f"Your answer was accepted on '{thread.title[:60]}'",
        url=reverse("conversations:detail", args=[project.slug, thread.pk]),
        project_slug=project.slug,
        thread_id=thread.pk,
    )


# --- use reports -------------------------------------------------------


def use_report_created(report):
    project = report.project
    url = reverse("use_reports:index", args=[project.slug])
    for user in _project_team(project):
        if user.id == report.author_id:
            continue
        _emit(
            user,
            kind="use_report",
            title=f"New use report on {project.title}",
            body=f"{_actor_name(report.author)} shared how they used your project.",
            url=url,
            project_slug=project.slug,
            use_report_id=report.pk,
        )


def use_report_moderated(report, *, hidden: bool):
    if not report.author_id:
        return
    verb = "hidden by staff" if hidden else "restored"
    _emit(
        report.author,
        kind="use_report_moderated",
        title=f"Your use report on {report.project.title} was {verb}",
        url=reverse("use_reports:index", args=[report.project.slug]),
        project_slug=report.project.slug,
        use_report_id=report.pk,
    )


# --- wiki --------------------------------------------------------------


def wiki_suggestion_created(revision, project, page):
    url = reverse("wiki:review", args=[project.slug])
    for user in _project_team(project):
        if user.id == revision.author_id:
            continue
        _emit(
            user,
            kind="wiki_suggestion",
            title=f"Wiki suggestion on {project.title}",
            body=(
                f"{_actor_name(revision.author)} suggested an edit to "
                f"'{revision.title[:80]}'."
            ),
            url=url,
            # One unread notification per page with pending suggestions,
            # not one per keystroke-sized revision.
            dedup_key=f"wiki-suggestion:{page.pk}",
            project_slug=project.slug,
            page_slug=page.slug,
        )


def wiki_suggestion_reviewed(revision, project, *, approved: bool, page_slug: str):
    if not revision.author_id:
        return
    verb = "applied" if approved else "declined"
    _emit(
        revision.author,
        kind="wiki_suggestion_reviewed",
        title=f"Your wiki suggestion was {verb} on {project.title}",
        url=reverse("wiki:detail", args=[project.slug, page_slug]),
        project_slug=project.slug,
    )


# --- feedback ----------------------------------------------------------


def feedback_submitted(feedback):
    for user in _staff():
        if user.id == feedback.user_id:
            continue
        _emit(
            user,
            kind="feedback_submitted",
            title=f"New feedback from {_actor_name(feedback.user)}",
            body=feedback.message[:200],
            url=reverse("feedback:review"),
            feedback_id=feedback.pk,
        )


def feedback_replied(reply):
    feedback = reply.feedback
    if not feedback.user_id or feedback.user_id == reply.author_id:
        return
    _emit(
        feedback.user,
        kind="feedback_reply",
        title="A maintainer replied to your feedback",
        body=reply.body[:200],
        url="/feedback/mine/",
        feedback_id=feedback.pk,
    )


# --- moderation --------------------------------------------------------


def content_report_filed(report):
    for user in _staff():
        if user.id == report.reporter_id:
            continue
        _emit(
            user,
            kind="content_report",
            title=f"Content report: {report.target_repr[:100]}",
            body=report.reason[:200],
            url=reverse("moderation:report_detail", args=[report.pk]),
            report_id=report.pk,
        )


def project_hidden(project, *, hidden: bool):
    for user in _project_team(project):
        verb = "hidden by staff" if hidden else "restored by staff"
        _emit(
            user,
            kind="project_hidden",
            title=f"Your project {project.title} was {verb}",
            url=project.get_absolute_url(),
            project_slug=project.slug,
        )


# --- projects ----------------------------------------------------------


def project_published(project):
    """Staff hear about every newly published project (opt-in via the
    staff preference group once email preferences exist; in-app always)."""
    for user in _staff():
        if user.id == project.created_by_id:
            continue
        _emit(
            user,
            kind="project_published",
            title=f"New project published: {project.title}",
            body=f"by {_actor_name(project.created_by)}",
            url=project.get_absolute_url(),
            dedup_key=f"project-published:{project.pk}",
            project_slug=project.slug,
        )
