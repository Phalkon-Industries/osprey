"""Run due Zenodo jobs (publish, new version, metadata sync).

Called by the notifier loop every tick; safe to run by hand or from
cron. All state lives on `ZenodoJob` rows, so overlapping runs only
compete for row locks.
"""

from django.core.management.base import BaseCommand

from projects import zenodo_jobs


class Command(BaseCommand):
    help = "Run queued Zenodo jobs that are due."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        ran = zenodo_jobs.run_due_jobs(limit=options["limit"])
        if ran:
            self.stdout.write(f"ran {ran} Zenodo job{'s' if ran != 1 else ''}")
