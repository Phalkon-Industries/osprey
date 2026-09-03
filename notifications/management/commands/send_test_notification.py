"""Fire a synthetic notification through the real event pipeline.

For testing events and email end to end without touching real projects:
creates an in-app notification for the named user via the same _emit
path real events use, which also queues email per their preferences.
With --deliver, the outbox is drained immediately afterward, so one
command exercises event -> inbox -> queue -> Mailjet.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from notifications import emails, events
from notifications.models import QueuedEmail


class Command(BaseCommand):
    help = "Send a test notification (and optionally its email) to a user."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument(
            "--group",
            default="projects",
            choices=sorted(set(events.EVENTS.values())),
            help="Preference group to emit under (default: projects).",
        )
        parser.add_argument(
            "--deliver",
            action="store_true",
            help="Run the outbox sender immediately after queueing.",
        )

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist as exc:
            raise CommandError(f"No user named {options['username']!r}.") from exc

        group = options["group"]
        kind = next(k for k, g in events.EVENTS.items() if g == group)
        stamp = timezone.now().strftime("%H:%M:%S")
        notification = events._emit(
            user,
            kind=kind,
            title=f"Test notification ({group} group, {stamp})",
            body="Sent by manage.py send_test_notification to exercise the pipeline.",
            url="/inbox/",
        )
        if notification is None:
            raise CommandError(
                "No notification was created (inactive user?)."
            )
        self.stdout.write(f"In-app notification created (kind={kind}).")

        queued = QueuedEmail.objects.filter(
            user=user, status=QueuedEmail.STATUS_QUEUED
        ).count()
        cadence = emails.wants_email(user, group)
        if queued:
            self.stdout.write(f"Outbox: {queued} email(s) queued.")
        elif cadence in ("daily", "weekly"):
            self.stdout.write(
                f"No immediate email: their {group} cadence is {cadence}, so "
                "it waits for the digest."
            )
        else:
            self.stdout.write(
                "No email queued: email disabled, group off, or no verified "
                "address."
            )
        if options["deliver"]:
            call_command("send_queued_email")
