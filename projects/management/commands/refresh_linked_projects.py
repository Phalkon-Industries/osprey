"""Re-read every linked project's Zenodo record: new versions, changed
title or license, new creators, community membership, edit grant. Run
daily by the notifier loop; `--slug` for one project."""

from django.core.management.base import BaseCommand

from projects import zenodo_link
from projects.models import Project


class Command(BaseCommand):
    help = "Refresh linked projects from their Zenodo records."

    def add_arguments(self, parser):
        parser.add_argument("--slug")

    def handle(self, *args, **options):
        qs = Project.objects.filter(origin=Project.ORIGIN_LINKED)
        if options.get("slug"):
            qs = qs.filter(slug=options["slug"])
        refreshed = failed = 0
        for project in qs.order_by("slug"):
            try:
                changes = zenodo_link.refresh_linked(project)
            except Exception as exc:  # noqa: BLE001 - one bad record must not stop the sweep
                failed += 1
                self.stderr.write(f"{project.slug}: {exc}")
                continue
            refreshed += 1
            if changes["versions_added"] or changes["creators_added"] or changes["fields"]:
                self.stdout.write(f"{project.slug}: {changes}")
        self.stdout.write(f"refreshed {refreshed}, failed {failed}")
