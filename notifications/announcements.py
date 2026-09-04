"""Service announcement fan-out: in-app to every active user, email to
everyone with email enabled and a verified address.

Cadence groups are deliberately ignored here. A service notice (an
outage, a policy change, a breaking update) must not sit in a weekly
digest, and the privacy policy already tells users the address is used
for "the occasional service notice that affects your account". The
master email toggle still rules: email disabled means no email, and the
standard footer (appended at send time) carries the stop-all link.
"""
from __future__ import annotations

import logging

from django.contrib.auth import get_user_model

from . import events
from .emails import SUBJECT_PREFIX, absolute_url, verified_address_for
from .models import NotificationPreference, QueuedEmail

logger = logging.getLogger(__name__)


def send_announcement(announcement, recipients=None):
    """Deliver to `recipients` (default: all active users).

    Returns (delivered, emailed): in-app rows created and emails queued.
    Per-user failures are logged and skipped; one bad row must not stop
    a broadcast.
    """
    if recipients is None:
        recipients = get_user_model().objects.filter(is_active=True)
    delivered = emailed = 0
    for user in recipients:
        try:
            if events.service_announcement(user, announcement) is not None:
                delivered += 1
            emailed += _queue_email(user, announcement)
        except Exception:  # noqa: BLE001 - keep the broadcast going
            logger.exception(
                "announcement %s failed for user %s", announcement.pk, user.pk
            )
    return delivered, emailed


def _queue_email(user, announcement) -> int:
    preference = NotificationPreference.for_user(user)
    if not preference.email_enabled or not verified_address_for(user):
        return 0
    lines = [announcement.body]
    if announcement.url:
        lines += ["", absolute_url(announcement.url)]
    QueuedEmail.objects.create(
        user=user,
        group="announcement",
        subject=f"{SUBJECT_PREFIX}{announcement.subject}"[:300],
        body_text="\n".join(lines),
    )
    return 1
