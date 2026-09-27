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
    """A single submission from the suggestion box or the privacy request form.

    Both entry points are shown only to logged-in users, so `user` is required.
    Captures the page URL, viewport size, and a coarse browser/OS string
    derived from the User-Agent header so the founder can reproduce
    issues without asking.
    """

    CATEGORY_SUGGESTION = "suggestion"
    CATEGORY_PRIVACY = "privacy"
    CATEGORY_OUTREACH = "outreach"
    CATEGORY_CHOICES = [
        (CATEGORY_SUGGESTION, "Suggestion"),
        (CATEGORY_PRIVACY, "Privacy request"),
        (CATEGORY_OUTREACH, "Staff outreach"),
    ]

    OPEN_STATUSES = ["new", "triaged"]
    ARCHIVED_STATUSES = ["resolved", "wontfix"]

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
    category = models.CharField(
        max_length=12,
        choices=CATEGORY_CHOICES,
        default=CATEGORY_SUGGESTION,
        help_text="Suggestions come from the suggestion box; privacy "
        "requests come from the form linked in the Privacy Policy.",
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
    client_errors = models.TextField(
        blank=True,
        help_text="Recent uncaught JS errors on the page, captured by the widget.",
    )
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
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="opened_staff_threads",
        help_text="Staff member who opened this thread, for outreach "
        "threads. Null when the user opened it themselves.",
    )
    reopen_requested = models.BooleanField(
        default=False,
        help_text="The user asked to reopen this closed thread. Staff "
        "reopen (set an open status) or dismiss the request.",
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

    @property
    def is_closed(self) -> bool:
        return self.status in self.ARCHIVED_STATUSES


class FeedbackReply(models.Model):
    """A staff reply to a feedback submission.

    Replies are visible to the original submitter on their feedback page
    and trigger a notification at creation time.
    """

    feedback = models.ForeignKey(
        Feedback, on_delete=models.CASCADE, related_name="replies"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="feedback_replies",
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"reply {self.pk} to feedback {self.feedback_id}"
