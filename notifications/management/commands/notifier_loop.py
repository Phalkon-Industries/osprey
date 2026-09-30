"""Long-running loop for the notifier sidecar container.

Runs due Zenodo jobs and processes the email outbox every INTERVAL seconds and runs the digest
pass when a day has rolled over since the last one. Deliberately boring:
no broker, no scheduler dependency, restartable at any moment because
all state lives in the database.
"""
from __future__ import annotations

import time

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

INTERVAL_SECONDS = 30
# Zenodo community inclusion requests are checked this often.
COMMUNITY_POLL_SECONDS = 600


def _heartbeat(*, ok: bool, exc: BaseException | None = None) -> None:
    """Record the iteration; raise the alarm on a streak. Never raises."""
    try:
        from notifications import loop_health

        if ok:
            loop_health.record_ok()
        else:
            loop_health.record_failure(exc or RuntimeError("unknown"))
    except Exception:  # noqa: BLE001 - if even this fails, the log line above stands
        pass


class Command(BaseCommand):
    help = "Run the notification email loop (outbox every 30s, digests daily)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single iteration and exit (for tests and cron).",
        )

    def handle(self, *args, **options):
        last_digest_date = None
        last_community_poll = 0.0
        while True:
            try:
                call_command("run_zenodo_jobs")
                call_command("send_queued_email")
                if time.monotonic() - last_community_poll >= COMMUNITY_POLL_SECONDS:
                    call_command("accept_community_requests")
                    last_community_poll = time.monotonic()
                today = timezone.localdate()
                if last_digest_date != today:
                    call_command("send_email_digests")
                    call_command("prune_notifications")
                    call_command("prune_draft_archives")
                    call_command("refresh_registered_projects")
                    call_command("license_audit")
                    last_digest_date = today
                _heartbeat(ok=True)
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                # transient DB/provider outages and try again next tick.
                self.stderr.write(f"notifier iteration failed: {exc}")
                _heartbeat(ok=False, exc=exc)
            if options["once"]:
                break
            time.sleep(INTERVAL_SECONDS)
