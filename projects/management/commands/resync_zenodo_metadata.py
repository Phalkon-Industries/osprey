"""Queue a metadata sync for every published Zenodo record.

For changes that alter what OSPREY writes into a record (the OSPREY
contributor roles, the upload type, the permalink host after a domain
move): each published project gets one metadata_sync job, run by the
notifier loop with the usual retries. Nothing talks to Zenodo here.
"""

from django.core.management.base import BaseCommand

from projects import zenodo_jobs
from projects.models import Project, ProjectDeposit


class Command(BaseCommand):
    help = "Enqueue a Zenodo metadata sync for every published project (or one --slug)."

    def add_arguments(self, parser):
        parser.add_argument("--slug", help="Only this project")

    def handle(self, *args, **options):
        qs = Project.objects.filter(
            deposits__provider=ProjectDeposit.PROVIDER_ZENODO,
            deposits__state=ProjectDeposit.STATE_PUBLISHED,
        ).distinct()
        if options.get("slug"):
            qs = qs.filter(slug=options["slug"])
        queued = 0
        for project in qs.order_by("slug"):
            job = zenodo_jobs.enqueue_metadata_sync(project)
            queued += 1
            self.stdout.write(f"queued sync for {project.slug} (job {job.pk}, {job.status})")
        self.stdout.write(f"{queued} project{'s' if queued != 1 else ''} queued")
