"""Deliver due outbox rows. Run by the notifier loop (or cron/by hand)."""
from __future__ import annotations

from datetime import timedelta

from django.core.mail import EmailMessage
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from notifications import emails
from notifications.models import QueuedEmail

MAX_ATTEMPTS = 5
# Exponential backoff: 5, 10, 20, 40 minutes between retries.
BACKOFF_BASE_MINUTES = 5


class Command(BaseCommand):
    help = "Send queued notification emails, with retry and backoff."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit",
            type=int,
            default=50,
            help="Maximum emails to attempt in one run.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        sent = failed = cancelled = 0
        with transaction.atomic():
            due = list(
                # of=("self",): user is nullable now (external invite
                # rows), and Postgres refuses FOR UPDATE on the nullable
                # side of the join select_related would add.
                QueuedEmail.objects.select_for_update(skip_locked=True, of=("self",))
                .filter(status=QueuedEmail.STATUS_QUEUED, scheduled_for__lte=now)
                .select_related("user")[: options["limit"]]
            )
            for row in due:
                outcome = self._deliver(row)
                if outcome == "sent":
                    sent += 1
                elif outcome == "cancelled":
                    cancelled += 1
                else:
                    failed += 1
        self.stdout.write(
            f"sent={sent} retry_or_failed={failed} cancelled={cancelled}"
        )

    def _deliver(self, row: QueuedEmail) -> str:
        # Resolve the address now, never earlier: a removed address or a
        # flipped master switch cancels mail that was already queued.
        # (Cadence was checked at enqueue time; only the kill switches
        # are re-checked here, so digest rows pass through too.)
        from notifications.models import NotificationPreference

        if row.user is None:
            # External mail (contributor invites): raw address, no
            # account to unsubscribe, body carries its own explanation.
            if not row.to_address:
                if row.ephemeral:
                    row.delete()
                else:
                    row.status = QueuedEmail.STATUS_CANCELLED
                    row.save(update_fields=["status"])
                return "cancelled"
            message = EmailMessage(
                subject=row.subject,
                body=row.body_text,
                to=[row.to_address],
            )
        else:
            to_address = emails.verified_address_for(row.user)
            preference = NotificationPreference.for_user(row.user)
            if not to_address or not preference.email_enabled:
                row.status = QueuedEmail.STATUS_CANCELLED
                row.save(update_fields=["status"])
                return "cancelled"
            message = EmailMessage(
                subject=row.subject,
                body=row.body_text + emails.email_footer(row.user, row.group),
                to=[to_address],
                headers={
                    "List-Unsubscribe": "<"
                    + emails.absolute_url(
                        "/inbox/unsubscribe/"
                        + emails.make_unsubscribe_token(row.user, "all")
                        + "/"
                    )
                    + ">",
                },
            )
        try:
            message.send(fail_silently=False)
        except Exception as exc:  # noqa: BLE001 - provider/network errors
            # must leave the row retryable, not crash the loop.
            row.attempts += 1
            row.last_error = str(exc)[:1000]
            if row.attempts >= MAX_ATTEMPTS:
                if row.ephemeral:
                    # The address must not linger on a dead row.
                    row.delete()
                    return "failed"
                row.status = QueuedEmail.STATUS_FAILED
            else:
                delay = BACKOFF_BASE_MINUTES * (2 ** (row.attempts - 1))
                row.scheduled_for = timezone.now() + timedelta(minutes=delay)
            row.save(
                update_fields=["attempts", "last_error", "status", "scheduled_for"]
            )
            return "failed"
        if row.ephemeral:
            # Sent; drop the row so the raw address isn't kept anywhere.
            row.delete()
            return "sent"
        row.status = QueuedEmail.STATUS_SENT
        row.sent_at = timezone.now()
        row.save(update_fields=["status", "sent_at"])
        return "sent"
