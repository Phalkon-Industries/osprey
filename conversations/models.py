from __future__ import annotations

from django.conf import settings
from django.db import models

from projects.models import Project


class ProjectThread(models.Model):
    """A question or discussion thread tied to a single project."""

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="threads"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="threads_started",
    )
    title = models.CharField(max_length=200)
    body = models.TextField()
    is_closed = models.BooleanField(default=False)
    answer = models.ForeignKey(
        "ProjectReply",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="answer_to",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["project", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.project.slug}: {self.title[:60]}"


class ProjectReply(models.Model):
    thread = models.ForeignKey(
        ProjectThread, on_delete=models.CASCADE, related_name="replies"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="thread_replies",
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_by_asker_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]

    def __str__(self) -> str:
        return f"reply {self.pk} on thread {self.thread_id}"


class ThreadSubscription(models.Model):
    """Follows a thread. Contributing to a thread creates one by default.

    An explicit unfollow is a persistent row with subscribed=False, so
    replying again later never silently re-subscribes someone who muted
    the thread.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="thread_subscriptions",
    )
    thread = models.ForeignKey(
        ProjectThread, on_delete=models.CASCADE, related_name="subscriptions"
    )
    subscribed = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "thread"], name="unique_thread_subscription"
            )
        ]

    def __str__(self) -> str:
        state = "follows" if self.subscribed else "muted"
        return f"{self.user_id} {state} thread {self.thread_id}"


def ensure_subscribed(user, thread) -> None:
    """Auto-follow on contribution; never overrides an explicit unfollow."""
    if user is None or not getattr(user, "is_authenticated", False):
        return
    ThreadSubscription.objects.get_or_create(user=user, thread=thread)
