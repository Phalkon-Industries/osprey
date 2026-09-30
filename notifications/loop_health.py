"""Heartbeat and alarm for the notifier loop.

The loop calls `record_ok()` after a clean iteration and `record_failure()`
after one that raised. After ALERT_AFTER failures in a row, staff get an
inbox notice and a direct email. The email deliberately bypasses the
outbox, because the outbox is drained by the very loop that is failing.
One alarm per streak; a recovery notice when it clears.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

from .models import LoopHeartbeat, send

logger = logging.getLogger(__name__)

ALERT_AFTER = 5  # iterations, i.e. about two and a half minutes at the 30 s tick


def record_ok() -> None:
    hb = LoopHeartbeat.load()
    recovered = hb.alerted_at is not None
    hb.last_ok_at = timezone.now()
    hb.consecutive_failures = 0
    hb.last_error = ""
    hb.alerted_at = None
    hb.save()
    if recovered:
        _tell_staff(
            "Notifier loop recovered",
            "The notifier loop is running again after a run of failures.",
            email=False,
        )


def record_failure(exc: BaseException) -> None:
    hb = LoopHeartbeat.load()
    hb.consecutive_failures += 1
    hb.last_failure_at = timezone.now()
    hb.last_error = str(exc)[:2000]
    if hb.consecutive_failures >= ALERT_AFTER and hb.alerted_at is None:
        hb.alerted_at = timezone.now()
        hb.save()
        _tell_staff(
            f"Notifier loop failing: {hb.consecutive_failures} iterations in a row",
            f"Last error: {hb.last_error[:500]}\n\nNothing queued (publishing, email, digests) runs until this is fixed. Check the notifier container's logs.",
            email=True,
        )
        return
    hb.save()


def _staff_users():
    from django.contrib.auth import get_user_model

    return get_user_model().objects.filter(is_staff=True, is_active=True)


def _tell_staff(title: str, body: str, *, email: bool) -> None:
    url = reverse("zenodo_jobs")
    for user in _staff_users():
        try:
            send(user, kind="notifier_failing", title=title, body=body, url=url)
        except Exception:  # noqa: BLE001 - the alarm must never raise into the loop
            logger.exception("could not write notifier alarm for %s", user)
    if not email:
        return
    from .emails import verified_address_for

    addresses = [a for a in (verified_address_for(u) for u in _staff_users()) if a]
    if not addresses:
        return
    try:
        send_mail(
            f"[OSPREY] {title}",
            body + f"\n\n{getattr(settings, 'OSPREY_PUBLIC_BASE_URL', '')}{url}",
            None,
            addresses,
            fail_silently=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not email the notifier alarm")
