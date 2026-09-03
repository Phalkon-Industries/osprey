"""Notification email: enqueueing, address resolution, unsubscribe tokens.

Sits between the event layer (which calls enqueue_for_notification) and
the sender/digest management commands. The transport itself lives in
email.py; this module decides who gets mail, when, and with what footer.
"""
from __future__ import annotations

import logging

from django.core import signing
from django.urls import reverse

from allauth.account.models import EmailAddress

from .models import (
    CADENCE_IMMEDIATE,
    NotificationPreference,
    QueuedEmail,
)

logger = logging.getLogger(__name__)

UNSUBSCRIBE_SALT = "osprey.notifications.unsubscribe"
# Unsubscribe links keep working for a long time; a stale one in an old
# email should still work rather than dead-end a frustrated user.
UNSUBSCRIBE_MAX_AGE = 60 * 60 * 24 * 365

SUBJECT_PREFIX = "[OSPREY] "

# Scopes an unsubscribe token may carry: the preference groups plus "all".
UNSUBSCRIBE_SCOPES = {"projects", "replies", "follows", "account", "staff", "all"}


def verified_address_for(user) -> str:
    """The user's verified primary email, or "" when there isn't one."""
    address = (
        EmailAddress.objects.filter(user=user, verified=True)
        .order_by("-primary", "pk")
        .first()
    )
    return address.email if address else ""


def wants_email(user, group: str) -> str:
    """Cadence ("immediate"/"daily"/"weekly") if this user should get
    email for this group, or "" when they shouldn't (opted out, group
    off, or no verified address)."""
    preference = NotificationPreference.for_user(user)
    if not preference.email_enabled:
        return ""
    cadence = preference.cadence_for(group)
    if cadence in ("", "off"):
        return ""
    if not verified_address_for(user):
        return ""
    return cadence


def enqueue_for_notification(user, *, group: str, title: str, body: str, url: str):
    """Called by events._emit after the in-app row is created.

    Immediate cadence queues one outbox row now; daily/weekly leave the
    notification for the digest command to sweep. Never raises.
    """
    try:
        cadence = wants_email(user, group)
        if cadence != CADENCE_IMMEDIATE:
            return None
        lines = [title]
        if body:
            lines += ["", body]
        if url:
            lines += ["", f"View it on OSPREY: {absolute_url(url)}"]
        return QueuedEmail.objects.create(
            user=user,
            group=group,
            subject=f"{SUBJECT_PREFIX}{title}"[:300],
            body_text="\n".join(lines),
        )
    except Exception:  # noqa: BLE001 - email must never break the action
        logger.exception("Failed to enqueue notification email")
        return None


def absolute_url(path: str) -> str:
    from django.conf import settings

    if path.startswith("http"):
        return path
    return f"{settings.OSPREY_PUBLIC_BASE_URL}{path}"


def make_unsubscribe_token(user, scope: str) -> str:
    """Signed token for one-click unsubscribe. Scope is a group name or
    "all". Works logged out; only this server can mint one."""
    return signing.dumps({"u": user.pk, "s": scope}, salt=UNSUBSCRIBE_SALT)


def read_unsubscribe_token(token: str) -> dict | None:
    try:
        return signing.loads(
            token, salt=UNSUBSCRIBE_SALT, max_age=UNSUBSCRIBE_MAX_AGE
        )
    except signing.BadSignature:
        return None


def email_footer(user, group: str) -> str:
    """Why-you-got-this plus unsubscribe links, appended at send time."""
    if group not in UNSUBSCRIBE_SCOPES:
        group = "all"  # digests span groups; their link stops everything
    group_token = make_unsubscribe_token(user, group)
    all_token = make_unsubscribe_token(user, "all")
    group_url = absolute_url(
        reverse("notifications:unsubscribe", args=[group_token])
    )
    all_url = absolute_url(reverse("notifications:unsubscribe", args=[all_token]))
    settings_url = absolute_url(reverse("notifications:settings"))
    return (
        "\n\n--\n"
        "You're receiving this because of activity on OSPREY.\n"
        f"Stop emails like this: {group_url}\n"
        f"Stop all OSPREY email: {all_url}\n"
        f"Notification settings: {settings_url}\n"
    )
