"""Create/update (and optionally publish) a Zenodo sandbox deposit."""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from projects.models import Project, ProjectDeposit
from projects.zenodo import ZenodoError, publish_project_deposit, sync_project_to_zenodo


class Command(BaseCommand):
    help = "Smoke-test the Zenodo sandbox integration for a project."

    def add_arguments(self, parser):
        parser.add_argument("slug", nargs="?", help="Project slug. Defaults to the newest public project.")
        parser.add_argument(
            "--publish",
            action="store_true",
            help="Publish the sandbox draft after syncing it. Sandbox only.",
        )

    def handle(self, *args, **options):
        if not settings.ZENODO_USE_SANDBOX:
            raise CommandError("This smoke command only runs when ZENODO_USE_SANDBOX=1.")
        if not settings.ZENODO_ACCESS_TOKEN:
            raise CommandError("ZENODO_ACCESS_TOKEN is not configured.")

        slug = options.get("slug")
        if slug:
            project = Project.objects.get(slug=slug)
        else:
            project = Project.objects.filter(visibility=Project.VISIBILITY_PUBLIC).order_by("-updated_at").first()
            if project is None:
                project = Project.objects.order_by("-updated_at").first()
        if project is None:
            raise CommandError("No project exists to deposit.")

        try:
            deposit = sync_project_to_zenodo(project, project.created_by)
        except ZenodoError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(f"Synced Zenodo sandbox draft for {project.slug}"))
        self.stdout.write(f"deposition_id={deposit.deposition_id}")
        self.stdout.write(f"state={deposit.state}")
        self.stdout.write(f"doi={deposit.doi or '(none yet)'}")
        if deposit.external_url:
            self.stdout.write(f"url={deposit.external_url}")

        if options["publish"]:
            if deposit.state != ProjectDeposit.STATE_DRAFT:
                raise CommandError("Only draft deposits can be published.")
            try:
                deposit = publish_project_deposit(deposit)
            except ZenodoError as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write(self.style.SUCCESS("Published Zenodo sandbox record"))
            self.stdout.write(f"record_id={deposit.record_id}")
            self.stdout.write(f"doi={deposit.doi or '(none returned)'}")
            if deposit.external_url:
                self.stdout.write(f"url={deposit.external_url}")