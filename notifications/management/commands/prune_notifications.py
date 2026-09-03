"""Delete old read notifications so the table doesn't grow forever."""
from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from notifications.models import Notification, QueuedEmail

KEEP_READ_DAYS = 365
KEEP_EMAIL_DAYS = 90


class Command(BaseCommand):
    help = "Prune read notifications older than a year and settled outbox rows older than 90 days."

    def handle(self, *args, **options):
        now = timezone.now()
        notifications_deleted, _ = Notification.objects.filter(
            is_read=True, created_at__lt=now - timedelta(days=KEEP_READ_DAYS)
        ).delete()
        emails_deleted, _ = QueuedEmail.objects.filter(
            status__in=[
                QueuedEmail.STATUS_SENT,
                QueuedEmail.STATUS_CANCELLED,
                QueuedEmail.STATUS_FAILED,
            ],
            created_at__lt=now - timedelta(days=KEEP_EMAIL_DAYS),
        ).delete()
        self.stdout.write(
            f"pruned notifications={notifications_deleted} emails={emails_deleted}"
        )
