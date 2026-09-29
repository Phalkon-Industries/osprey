"""Accept pending Zenodo community inclusion requests for records that
belong to projects on OSPREY. Run every few minutes by the notifier
loop. Requests for unknown records are left for staff on Zenodo."""

from django.core.management.base import BaseCommand

from projects import zenodo_link


class Command(BaseCommand):
    help = "Accept OSPREY-community inclusion requests for known records."

    def handle(self, *args, **options):
        accepted = zenodo_link.accept_community_requests()
        if accepted:
            self.stdout.write(f"accepted {accepted} community request{'s' if accepted != 1 else ''}")
