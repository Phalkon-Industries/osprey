"""Compare each public native and registered project's license with its
Zenodo record and linked GitHub repositories.

    manage.py license_audit [--project slug] [--pause 0.5] [--no-notify]

Runs daily from the notifier loop. Owners hear about new findings in the
inbox; staff see all open findings at /staff/licenses/.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from projects.sources import license_check
from projects.models import Project


class Command(BaseCommand):
    help = "Audit project licenses against Zenodo and GitHub."

    def add_arguments(self, parser):
        parser.add_argument("--project", help="Slug of one project to check.")
        parser.add_argument("--pause", type=float, default=0.5)
        parser.add_argument("--no-notify", action="store_true")

    def handle(self, *args, **options):
        if options["project"]:
            try:
                projects = [Project.objects.get(slug=options["project"])]
            except Project.DoesNotExist as exc:
                raise CommandError("No such project.") from exc
        else:
            projects = list(license_check.auditable())
        counts = license_check.run_audit(projects, pause=options["pause"], notify=not options["no_notify"])
        self.stdout.write(f"license audit: checked {counts['checked']}, with findings {counts['with_findings']}")
        # Only what this run checked; other projects keep their last result.
        for p in Project.objects.filter(pk__in=[p.pk for p in projects]).order_by("pk"):
            for f in p.license_findings:
                self.stdout.write(f"  {p.slug}: {f['text']}")
