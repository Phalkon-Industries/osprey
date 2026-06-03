from __future__ import annotations

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

REPORT_CATEGORY_CHOICES = [
    ("spam", "Spam or junk"),
    ("abuse", "Abuse or harassment"),
    ("copyright", "Copyright or licensing concern"),
    ("off_topic", "Off-topic for OSPREY"),
    ("other", "Other"),
]


class Report(models.Model):
    STATUS_OPEN = "open"
    STATUS_ACTIONED = "actioned"
    STATUS_DISMISSED = "dismissed"
    STATUS_CHOICES = [
        (STATUS_OPEN, "Open"),
        (STATUS_ACTIONED, "Actioned"),
        (STATUS_DISMISSED, "Dismissed"),
    ]

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="reports_filed",
    )
    target_ct = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    target_id = models.PositiveIntegerField()
    target = GenericForeignKey("target_ct", "target_id")
    target_repr = models.CharField(
        max_length=300,
        blank=True,
        help_text="Snapshot of target title/url at the time of report.",
    )
    context_url = models.CharField(
        max_length=500,
        blank=True,
        help_text="URL the reporter was on when filing the report.",
    )
    category = models.CharField(max_length=20, choices=REPORT_CATEGORY_CHOICES)
    reason = models.TextField(blank=True)
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_OPEN
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reports_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["target_ct", "target_id"]),
        ]

    def __str__(self) -> str:
        return f"Report #{self.pk} ({self.get_category_display()})"


class ModerationLog(models.Model):
    """Append-only record of staff moderation actions."""

    ACTION_CHOICES = [
        ("user_suspend", "Suspend user"),
        ("user_reinstate", "Reinstate user"),
        ("user_purge_content", "Purge user content"),
        ("project_hide", "Hide project"),
        ("project_unhide", "Unhide project"),
        ("report_action", "Action report"),
        ("report_dismiss", "Dismiss report"),
        ("other", "Other"),
    ]

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="moderation_actions",
    )
    action = models.CharField(max_length=40, choices=ACTION_CHOICES)
    target_ct = models.ForeignKey(
        ContentType, on_delete=models.SET_NULL, null=True, blank=True
    )
    target_id = models.PositiveIntegerField(null=True, blank=True)
    target = GenericForeignKey("target_ct", "target_id")
    target_repr = models.CharField(max_length=300, blank=True)
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["target_ct", "target_id"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_action_display()} by {self.actor_id} at {self.created_at:%Y-%m-%d %H:%M}"
