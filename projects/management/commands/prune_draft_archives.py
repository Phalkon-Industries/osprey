"""Delete not-yet-published project archives after 30 days.

Draft archives live on OSPREY's media volume only until publish, so an
abandoned draft would otherwise hold up to 500 MiB forever. Pruning
removes the stale file and its attachment row; the draft itself keeps
all typed content, and the owner can re-upload the zip whenever they
come back. Published attachments are never touched (Zenodo hosts those;
the local file was already cleared at publish).

Runs daily from the notifier loop.
"""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from projects.models import ProjectAttachment

MAX_DRAFT_ARCHIVE_AGE = timedelta(days=30)


class Command(BaseCommand):
    help = "Delete unpublished project archives older than 30 days."

    def handle(self, *args, **options):
        cutoff = timezone.now() - MAX_DRAFT_ARCHIVE_AGE
        stale = ProjectAttachment.objects.filter(
            published_to_zenodo=False,
            created_at__lt=cutoff,
        ).exclude(file="")
        pruned = 0
        for attachment in stale:
            attachment.file.delete(save=False)
            attachment.delete()
            pruned += 1
        self.stdout.write(f"draft archives pruned: {pruned}")
