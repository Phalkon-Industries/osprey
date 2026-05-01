"""Models for in-page user feedback."""
from __future__ import annotations

import uuid
from datetime import datetime

from django.conf import settings
from django.db import models


def _screenshot_upload_to(instance: "Feedback", filename: str) -> str:
    now = datetime.utcnow()
    ext = "png"
    if "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()[:5] or "png"
    return f"feedback/{now.year:04d}-{now.month:02d}/{uuid.uuid4().hex}.{ext}"


class Feedback(models.Model):
    """A single feedback submission from the floating widget.

    The widget is shown only to logged-in users, so `user` is required.
    Captures the page URL, viewport size, and a coarse browser/OS string
    derived from the User-Agent header so the founder can reproduce
    issues without asking.
    """

    STATUS_NEW = "new"
    STATUS_TRIAGED = "triaged"
    STATUS_RESOLVED = "resolved"
    STATUS_WONTFIX = "wontfix"
    STATUS_CHOICES = [
        (STATUS_NEW, "New"),
        (STATUS_TRIAGED, "Triaged"),
        (STATUS_RESOLVED, "Resolved"),
        (STATUS_WONTFIX, "Won't fix"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feedback_submissions",
    )
    message = models.TextField(
        help_text="What the user wrote. Limited to 4000 characters by the form.",
    )
    page_url = models.URLField(
        max_length=500,
        blank=True,
        help_text="The page the widget was opened on.",
    )
    page_title = models.CharField(max_length=200, blank=True)
    user_agent = models.CharField(max_length=400, blank=True)
    browser = models.CharField(max_length=80, blank=True)
    os = models.CharField(max_length=80, blank=True)
    viewport_w = models.PositiveIntegerField(null=True, blank=True)
    viewport_h = models.PositiveIntegerField(null=True, blank=True)
    screenshot = models.ImageField(
        upload_to=_screenshot_upload_to,
        blank=True,
        null=True,
        help_text="Optional. Captured client-side and may include user annotations.",
    )

    status = models.CharField(
        max_length=12,
        choices=STATUS_CHOICES,
        default=STATUS_NEW,
    )
    admin_notes = models.TextField(
        blank=True,
        help_text="Triage notes. Not shown to the submitter.",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Feedback"
        verbose_name_plural = "Feedback"

    def __str__(self) -> str:
        who = self.user.get_username() if self.user_id else "anon"
        return f"[{self.status}] {who}: {self.message[:60]}"
