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
from django.db.models import Q
from django.urls import reverse

from .models import CADENCE_OFF, Notification, send

logger = logging.getLogger(__name__)

GROUP_PROJECTS = "projects"  # activity on projects you own
GROUP_REPLIES = "replies"  # responses to things you wrote
GROUP_FOLLOWS = "follows"  # people and work you follow (phase 4)
GROUP_NEW_PROJECTS = "new_projects"  # sitewide new-project notices
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
    "feedback_user_replied": GROUP_STAFF,
    "feedback_reopen_requested": GROUP_STAFF,
    "staff_message_received": GROUP_ACCOUNT,
    "service_announcement": GROUP_ACCOUNT,
    "contributor_listed": GROUP_ACCOUNT,
    "contributor_claim_resolved": GROUP_PROJECTS,
    "editor_granted": GROUP_ACCOUNT,
    "ownership_transfer_offered": GROUP_ACCOUNT,
    "ownership_transfer_resolved": GROUP_PROJECTS,
    "content_report": GROUP_STAFF,
    "project_published": GROUP_STAFF,
    "project_hidden": GROUP_ACCOUNT,
    "followed_creator_published": GROUP_FOLLOWS,
    "new_project_published": GROUP_NEW_PROJECTS,
    "watched_version_published": GROUP_FOLLOWS,
    "deposit_published": GROUP_PROJECTS,
    "deposit_failed": GROUP_ACCOUNT,
    "license_check": GROUP_ACCOUNT,
    "watched_activity": GROUP_FOLLOWS,
    "lineage_claimed": GROUP_PROJECTS,
    "lineage_responded": GROUP_PROJECTS,
    "lineage_withdrawn": GROUP_PROJECTS,
}


def _emit(user, *, kind, title, body="", url="", dedup_key=None, email=True, **payload):
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
    notification = send(user, kind=kind, title=title, body=body, url=url, **payload)
    if notification is not None and email:
        from . import emails

        emails.enqueue_for_notification(
            user, group=EVENTS[kind], title=title, body=body, url=url
        )
    return notification


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
    what = (
        "privacy request"
        if feedback.category == feedback.CATEGORY_PRIVACY
        else "suggestion"
    )
    for user in _staff():
        if user.id == feedback.user_id:
            continue
        _emit(
            user,
            kind="feedback_submitted",
            title=f"New {what} from {_actor_name(feedback.user)}",
            body=feedback.message[:200],
            url=reverse("feedback:review"),
            feedback_id=feedback.pk,
        )


def feedback_replied(reply):
    feedback = reply.feedback
    if not feedback.user_id or feedback.user_id == reply.author_id:
        return
    what = (
        "privacy request"
        if feedback.category == feedback.CATEGORY_PRIVACY
        else "suggestion"
    )
    _emit(
        feedback.user,
        kind="feedback_reply",
        title=f"Staff replied to your {what}",
        body=reply.body[:200],
        url=reverse("feedback:mine"),
        feedback_id=feedback.pk,
    )


def feedback_user_replied(reply):
    """Tell staff a user replied on one of their message threads."""
    for user in _staff():
        if user.id == reply.author_id:
            continue
        _emit(
            user,
            kind="feedback_user_replied",
            title=f"{_actor_name(reply.author)} replied on a staff message thread",
            body=reply.body[:200],
            url=reverse("feedback:review"),
            feedback_id=reply.feedback_id,
        )


def feedback_reopen_requested(feedback, requester):
    """Tell staff a user asked to reopen a closed thread."""
    for user in _staff():
        if user.id == (requester.id if requester else None):
            continue
        _emit(
            user,
            kind="feedback_reopen_requested",
            title=f"{_actor_name(requester)} asked to reopen a closed thread",
            body=feedback.message[:200],
            url=reverse("feedback:review") + "?reopen=1",
            dedup_key=f"reopen:{feedback.pk}",
            feedback_id=feedback.pk,
        )


def staff_message_received(feedback):
    """Tell a user that staff opened a message thread with them."""
    if not feedback.user_id:
        return
    _emit(
        feedback.user,
        kind="staff_message_received",
        title="OSPREY staff sent you a message",
        body=feedback.message[:200],
        url=reverse("feedback:mine"),
        dedup_key=f"staff-msg:{feedback.pk}",
        feedback_id=feedback.pk,
    )


def service_announcement(user, announcement):
    """In-app half of a service announcement. Email is queued separately
    by notifications.announcements, which deliberately ignores cadence
    groups (only the master toggle gates it)."""
    return _emit(
        user,
        kind="service_announcement",
        title=announcement.subject,
        body=announcement.body[:200],
        url=announcement.url or "",
        email=False,
        dedup_key=f"announcement:{announcement.pk}",
        announcement_id=announcement.pk,
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
    """A project went public: staff hear about it, followers of its
    creator hear about it, and so does anyone who opted into sitewide
    new-project notices. Each person gets at most one row: the staff
    row wins over the follower row, which wins over the sitewide one."""
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
    if not project.created_by_id:
        return
    followers = (
        get_user_model()
        .objects.filter(
            following__creator_id=project.created_by_id,
            is_active=True,
            is_staff=False,
        )
        .exclude(pk=project.created_by_id)
    )
    follower_ids = set()
    for user in followers:
        follower_ids.add(user.pk)
        _emit(
            user,
            kind="followed_creator_published",
            title=(
                f"{_actor_name(project.created_by)} published a new "
                f"project: {project.title}"
            ),
            url=project.get_absolute_url(),
            dedup_key=f"creator-published:{project.pk}",
            project_slug=project.slug,
        )
    # Sitewide notices default on (weekly), so users who never opened
    # notification settings have no preference row and are included.
    opted_in = (
        get_user_model()
        .objects.filter(is_active=True, is_staff=False)
        .filter(
            Q(notification_preference__isnull=True)
            | ~Q(notification_preference__new_projects=CADENCE_OFF)
        )
        .exclude(pk__in=follower_ids | {project.created_by_id})
    )
    for user in opted_in:
        _emit(
            user,
            kind="new_project_published",
            title=f"New on OSPREY: {project.title}",
            body=f"by {_actor_name(project.created_by)}",
            url=project.get_absolute_url(),
            dedup_key=f"new-project:{project.pk}",
            project_slug=project.slug,
        )


# --- follows and watches ------------------------------------------------


def _watchers(project, exclude_ids):
    return (
        get_user_model()
        .objects.filter(watching__project=project, is_active=True)
        .exclude(pk__in=[pk for pk in exclude_ids if pk])
    )


def new_version_published(project, actor=None):
    actor_id = actor.pk if actor else None
    for user in _watchers(project, [actor_id]):
        _emit(
            user,
            kind="watched_version_published",
            title=f"{project.title} published a new version",
            url=project.get_absolute_url(),
            dedup_key=f"watched-version:{project.pk}",
            project_slug=project.slug,
        )


def watched_wiki_page_created(page, project, author):
    for user in _watchers(project, [author.pk if author else None]):
        _emit(
            user,
            kind="watched_activity",
            title=f"New wiki page on {project.title}: {page.title[:80]}",
            url=reverse("wiki:detail", args=[project.slug, page.slug]),
            dedup_key=f"watched-wiki:{page.pk}",
            project_slug=project.slug,
        )


# --- contributor claiming and project access ---------------------------


def contributor_listed(contribution, by_user):
    """Invite the person behind the row's ORCID iD to claim it."""
    target = contribution.user
    if target is None:
        from projects.claiming import user_for_orcid

        target = user_for_orcid(contribution.orcid_id)
    if target is None or (by_user and target == by_user):
        return
    _emit(
        target,
        kind="contributor_listed",
        title=(
            f"{_actor_name(by_user)} listed you as a contributor on "
            f"“{contribution.project.title}”"
        ),
        body=f"Role: {contribution.role}. Review it and accept or decline.",
        url=reverse("contributor_claims"),
        dedup_key=f"contrib-invite:{contribution.pk}",
        contribution_id=contribution.pk,
    )


def contributor_claim_resolved(contribution, accepted):
    """Tell the owner the invitee answered."""
    recipient = contribution.project.created_by
    if recipient is None or (
        contribution.user_id and recipient.id == contribution.user_id
    ):
        return
    verb = "accepted" if accepted else "declined"
    _emit(
        recipient,
        kind="contributor_claim_resolved",
        title=(
            f"{contribution.display_name} {verb} the contributor listing "
            f"on “{contribution.project.title}”"
        ),
        url=contribution.project.get_absolute_url(),
        dedup_key=f"contrib-resolved:{contribution.pk}:{verb}",
        contribution_id=contribution.pk,
    )


def editor_granted(contribution, by_user):
    """Tell a verified contributor they can now edit the project."""
    if contribution.user is None or (by_user and contribution.user == by_user):
        return
    _emit(
        contribution.user,
        kind="editor_granted",
        title=f"You can now edit “{contribution.project.title}”",
        body=f"{_actor_name(by_user)} made you an editor.",
        url=contribution.project.get_absolute_url(),
        dedup_key=f"editor:{contribution.pk}",
        contribution_id=contribution.pk,
    )


def ownership_transfer_offered(project, by_user):
    """Tell the intended new owner; nothing changes until they accept."""
    recipient = project.pending_owner
    if recipient is None or (by_user and recipient == by_user):
        return
    _emit(
        recipient,
        kind="ownership_transfer_offered",
        title=(
            f"{_actor_name(by_user)} wants to transfer ownership of "
            f"“{project.title}” to you"
        ),
        body="Accept or decline from the project page.",
        url=project.get_absolute_url(),
        dedup_key=f"transfer:{project.pk}",
        project_slug=project.slug,
    )


def ownership_transfer_resolved(project, old_owner, accepted, actor=None):
    """Tell the previous owner how the transfer ended."""
    if old_owner is None or (actor and old_owner == actor):
        return
    verb = "accepted" if accepted else "declined"
    _emit(
        old_owner,
        kind="ownership_transfer_resolved",
        title=f"Ownership transfer of “{project.title}” was {verb}",
        url=project.get_absolute_url(),
        dedup_key=f"transfer-resolved:{project.pk}:{verb}",
        project_slug=project.slug,
    )


# --- lineage claims ----------------------------------------------------


def _lineage_phrase(edge):
    return "is derived from" if edge.relation == "derived_from" else "uses"


def lineage_claimed(edge):
    """Tell the parent project's team a lineage link to their project
    went live. Informational: no action needed unless it's wrong."""
    recipient = edge.parent.created_by
    if recipient is None or (edge.declared_by and recipient == edge.declared_by):
        return
    phrase = _lineage_phrase(edge)
    _emit(
        recipient,
        kind="lineage_claimed",
        title=(
            f"“{edge.child.title}” {phrase} your project "
            f"“{edge.parent.title}”"
        ),
        body=(
            "No action needed. If the claim is wrong, you can dispute it "
            "on your project's lineage page at any time."
        ),
        url=reverse("projects:lineage", args=[edge.parent.slug]),
        dedup_key=f"lineage-claim:{edge.pk}",
        edge_id=edge.pk,
    )


def lineage_responded(edge, actor=None):
    """Tell the claiming side the parent disputed the link, or retracted
    a dispute."""
    recipient = edge.declared_by or edge.child.created_by
    if recipient is None or (actor and recipient == actor):
        return
    phrase = _lineage_phrase(edge)
    from projects.models import LineageEdge

    if edge.status == LineageEdge.STATUS_DISPUTED:
        title = (
            f"“{edge.parent.title}” disputed the claim that "
            f"“{edge.child.title}” {phrase} it"
        )
        body = edge.dispute_reason[:200] if edge.dispute_reason else ""
    else:
        title = (
            f"“{edge.parent.title}” retracted its dispute of the link "
            f"from “{edge.child.title}”"
        )
        body = ""
    _emit(
        recipient,
        kind="lineage_responded",
        title=title,
        body=body,
        url=reverse("projects:lineage", args=[edge.child.slug]),
        dedup_key=f"lineage-response:{edge.pk}:{edge.status}",
        edge_id=edge.pk,
    )


def lineage_withdrawn(edge, actor=None):
    """Tell the parent's team a previously confirmed link was withdrawn."""
    recipient = edge.parent.created_by
    if recipient is None or (actor and recipient == actor):
        return
    _emit(
        recipient,
        kind="lineage_withdrawn",
        title=(
            f"“{edge.child.title}” withdrew its link to your project "
            f"“{edge.parent.title}”"
        ),
        body="The claim stays on record, labeled as withdrawn.",
        url=reverse("projects:lineage", args=[edge.parent.slug]),
        dedup_key=f"lineage-withdrawn:{edge.pk}",
        edge_id=edge.pk,
    )


def watched_use_report_created(report):
    project = report.project
    exclude = [report.author_id, project.created_by_id]  # owner got their own
    for user in _watchers(project, exclude):
        _emit(
            user,
            kind="watched_activity",
            title=f"New use report on {project.title}",
            url=reverse("use_reports:index", args=[project.slug]),
            dedup_key=f"watched-report:{report.pk}",
            project_slug=project.slug,
        )


# --- Zenodo jobs -----------------------------------------------------------


def deposit_published(project, deposit=None, *, new_version: bool = False):
    """Tell the owner their publish (or new version) went through on Zenodo."""
    owner = project.created_by
    if owner is None:
        return
    doi = (deposit.doi if deposit is not None else "") or project.doi or ""
    if new_version:
        title = f"New version of {project.title} is live"
    else:
        title = f"{project.title} is published"
    body = f"DOI {doi}" if doi else "Zenodo accepted the deposit."
    _emit(
        owner,
        kind="deposit_published",
        title=title,
        body=body,
        url=project.get_absolute_url(),
        dedup_key=f"deposit-published:{project.pk}:{doi}",
        project_slug=project.slug,
    )


def deposit_failed(project, job):
    """Tell the owner a queued Zenodo job gave up, and where to retry."""
    owner = project.created_by
    if owner is None:
        return
    what = "New version of " + project.title if job.kind == "new_version" else project.title
    _emit(
        owner,
        kind="deposit_failed",
        title=f"{what} could not be published",
        body=(
            f"Zenodo did not accept the deposit after {job.attempts} "
            f"attempt{'s' if job.attempts != 1 else ''}: {job.last_error[:200]} "
            "Your draft is safe. Retry from the project page."
        ),
        url=project.get_absolute_url(),
        dedup_key=f"deposit-failed:{job.pk}",
        project_slug=project.slug,
    )
