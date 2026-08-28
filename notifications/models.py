from __future__ import annotations

import os

from django.conf import settings
from django.db import models


class EmailSettings(models.Model):
    """Singleton row: admin-editable email transport configuration.

    Keys and the from address live here so staff can manage email from
    /admin/ without touching .env or restarting containers. Env vars
    (MAILJET_API_KEY, MAILJET_SECRET_KEY, DEFAULT_FROM_EMAIL) seed the
    row on first load and act as fallbacks while fields are blank.

    The API key is stored in the database by design (an accepted
    trade-off, see planning notes): use a send-only Mailjet key and
    rotate it if the database is ever exposed.
    """

    enabled = models.BooleanField(
        default=False,
        help_text=(
            "Master switch. When off (or when keys are missing), outgoing "
            "mail is written to the server log instead of being sent."
        ),
    )
    from_email = models.CharField(
        max_length=254,
        blank=True,
        default="",
        help_text='Sender address, e.g. "OSPREY <notify@osprey.phalkon.io>".',
    )
    mailjet_api_key = models.CharField(max_length=128, blank=True, default="")
    mailjet_secret_key = models.CharField(max_length=128, blank=True, default="")
    test_recipient = models.EmailField(
        blank=True,
        default="",
        help_text="Where the “send test email” admin action delivers.",
    )
    last_test_at = models.DateTimeField(null=True, blank=True)
    last_test_result = models.CharField(max_length=300, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "email settings"
        verbose_name_plural = "email settings"

    def __str__(self) -> str:
        return "Email settings"

    def save(self, *args, **kwargs):
        self.pk = 1  # enforce the singleton
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "EmailSettings":
        obj, _created = cls.objects.get_or_create(
            pk=1,
            defaults={
                "from_email": os.environ.get("DEFAULT_FROM_EMAIL", ""),
                "mailjet_api_key": os.environ.get("MAILJET_API_KEY", ""),
                "mailjet_secret_key": os.environ.get("MAILJET_SECRET_KEY", ""),
            },
        )
        return obj


class Notification(models.Model):
    """A single in-app notification for one user.

    `kind` is a short string that lets templates and counters group
    related events. `payload` is a small JSON blob with whatever context
    the rendering template needs (project slug, feedback id, etc.).
    `url` is the click-through destination.
    """

    KIND_FEEDBACK_REPLY = "feedback_reply"
    KIND_PROJECT_COMMENT = "project_comment"
    KIND_USE_REPORT = "use_report"
    KIND_WIKI_SUGGESTION = "wiki_suggestion"
    KIND_GENERIC = "generic"

    KIND_CHOICES = [
        (KIND_FEEDBACK_REPLY, "Feedback reply"),
        (KIND_PROJECT_COMMENT, "Project comment"),
        (KIND_USE_REPORT, "Project use report"),
        (KIND_WIKI_SUGGESTION, "Wiki suggestion"),
        (KIND_GENERIC, "General"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    kind = models.CharField(max_length=40, choices=KIND_CHOICES, default=KIND_GENERIC)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default="")
    url = models.CharField(max_length=400, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "is_read", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.kind} for {self.user_id}: {self.title}"


def send(
    user, *, kind: str, title: str, body: str = "", url: str = "", **payload
) -> Notification | None:
    """Create a Notification row, or None if the target user can't receive one.

    Inactive users and anonymous targets are skipped silently so callers
    don't have to guard every invocation.
    """
    if (
        user is None
        or not getattr(user, "is_authenticated", False)
        or not user.is_active
    ):
        return None
    return Notification.objects.create(
        user=user,
        kind=kind,
        title=title[:200],
        body=body,
        url=url[:400],
        payload=payload or {},
    )
