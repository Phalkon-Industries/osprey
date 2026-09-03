"""Build daily/weekly digest emails from unemailed notifications.

Digests are rendered from Notification rows at send time, so nothing is
double-stored. Weekly groups sweep once at least seven days have passed
since that user's last weekly digest, which self-regulates regardless of
which day the command runs.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from notifications import emails
from notifications.events import EVENTS
from notifications.models import (
    CADENCE_DAILY,
    CADENCE_WEEKLY,
    Notification,
    NotificationPreference,
    QueuedEmail,
)

GROUPS = ("projects", "replies", "follows", "staff")
WEEK = timedelta(days=7)
# First digest for a user reaches back at most this far.
FIRST_DIGEST_WINDOW = timedelta(days=7)


class Command(BaseCommand):
    help = "Queue daily and weekly notification digests."

    def handle(self, *args, **options):
        now = timezone.now()
        queued = 0
        preferences = NotificationPreference.objects.filter(
            email_enabled=True
        ).select_related("user")
        for preference in preferences:
            if not emails.verified_address_for(preference.user):
                continue
            sections, marks = self._collect(preference, now)
            if sections:
                self._queue(preference.user, sections)
                queued += 1
            if marks:
                preference.save(update_fields=marks + ["updated_at"])
        self.stdout.write(f"digests queued: {queued}")

    def _collect(self, preference, now):
        """Gather (group, [notifications]) sections due for this user."""
        sections = []
        marks = []
        plans = [
            (CADENCE_DAILY, "last_daily_digest_at", timedelta()),
            (CADENCE_WEEKLY, "last_weekly_digest_at", WEEK),
        ]
        for cadence, stamp_field, min_gap in plans:
            groups = [g for g in GROUPS if preference.cadence_for(g) == cadence]
            if not groups:
                continue
            last = getattr(preference, stamp_field)
            if last and now - last < min_gap:
                continue
            since = last or (now - FIRST_DIGEST_WINDOW)
            kinds = [k for k, g in EVENTS.items() if g in groups]
            rows = list(
                Notification.objects.filter(
                    user=preference.user,
                    kind__in=kinds,
                    created_at__gt=since,
                    created_at__lte=now,
                ).order_by("created_at")
            )
            grouped: dict[str, list] = {}
            for row in rows:
                grouped.setdefault(EVENTS[row.kind], []).append(row)
            sections.extend(sorted(grouped.items()))
            setattr(preference, stamp_field, now)
            marks.append(stamp_field)
        return sections, marks

    def _queue(self, user, sections):
        lines = ["Here's what happened on OSPREY since your last summary:"]
        labels = {
            "projects": "Activity on your projects",
            "replies": "Replies and reviews",
            "follows": "People and work you follow",
            "staff": "Staff events",
        }
        total = 0
        for group, rows in sections:
            lines += ["", f"## {labels.get(group, group)}"]
            for row in rows:
                total += 1
                entry = f"- {row.title}"
                if row.url:
                    entry += f"\n  {emails.absolute_url(row.url)}"
                lines.append(entry)
        QueuedEmail.objects.create(
            user=user,
            group="digest",
            subject=f"{emails.SUBJECT_PREFIX}Your OSPREY summary "
            f"({total} update{'s' if total != 1 else ''})",
            body_text="\n".join(lines),
        )
