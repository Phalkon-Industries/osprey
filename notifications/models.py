from __future__ import annotations

from django.conf import settings
from django.db import models


class Notification(models.Model):
    """A single in-app notification for one user.

    `kind` is a short string that lets templates and counters group
    related events. `payload` is a small JSON blob with whatever context
    the rendering template needs (project slug, feedback id, etc.).
    `url` is the click-through destination.
    """

    KIND_FEEDBACK_REPLY = "feedback_reply"
    KIND_PROJECT_COMMENT = "project_comment"
    KIND_ATTESTATION = "attestation"
    KIND_WIKI_SUGGESTION = "wiki_suggestion"
    KIND_GENERIC = "generic"

    KIND_CHOICES = [
        (KIND_FEEDBACK_REPLY, "Feedback reply"),
        (KIND_PROJECT_COMMENT, "Project comment"),
        (KIND_ATTESTATION, "Project attestation"),
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
