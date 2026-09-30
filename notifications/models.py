from __future__ import annotations

import os

from django.conf import settings
from django.db import models
from django.utils import timezone


class EmailSettings(models.Model):
    """Singleton row: admin-editable email transport configuration.

    Holds only non-secret operational settings: the master switch, the
    from address, and the test recipient. Delivery-provider credentials
    are deliberately NOT here: secrets live in env files only, never in
    the database, where backups, dumps, and admin-account compromise
    would expose them. The provider itself is env-configured too
    (settings.EMAIL_DELIVERY_BACKEND); this model knows nothing about
    which one is in use.
    """

    enabled = models.BooleanField(
        default=False,
        help_text=(
            "Master switch. When off (or when the delivery provider is "
            "not configured), outgoing mail is written to the server log "
            "instead of being sent."
        ),
    )
    from_email = models.CharField(
        max_length=254,
        blank=True,
        default="",
        help_text='Sender address, e.g. "OSPREY <notify@osprey.phalkon.io>".',
    )
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
            defaults={"from_email": os.environ.get("DEFAULT_FROM_EMAIL", "")},
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


CADENCE_OFF = "off"
CADENCE_IMMEDIATE = "immediate"
CADENCE_DAILY = "daily"
CADENCE_WEEKLY = "weekly"
CADENCE_CHOICES = [
    (CADENCE_OFF, "Off"),
    (CADENCE_IMMEDIATE, "Immediate"),
    (CADENCE_DAILY, "Daily summary"),
    (CADENCE_WEEKLY, "Weekly summary"),
]


class NotificationPreference(models.Model):
    """Per-user notification behavior and email cadence, one row per user.

    Cadence works like Discourse: a daily summary by default, immediate
    for people who want the firehose, weekly for people who want quiet.
    `email_enabled` is the master opt-out; with it off (or with no
    verified address) no notification email is ever sent, while in-app
    notifications keep working regardless.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notification_preference",
    )
    email_enabled = models.BooleanField(
        default=True,
        help_text="Master switch for all notification email to this user.",
    )
    projects = models.CharField(
        max_length=10, choices=CADENCE_CHOICES, default=CADENCE_DAILY
    )
    replies = models.CharField(
        max_length=10, choices=CADENCE_CHOICES, default=CADENCE_DAILY
    )
    follows = models.CharField(
        max_length=10, choices=CADENCE_CHOICES, default=CADENCE_DAILY
    )
    account = models.CharField(
        max_length=10,
        choices=[(CADENCE_OFF, "Off"), (CADENCE_IMMEDIATE, "Immediate")],
        default=CADENCE_IMMEDIATE,
    )
    staff = models.CharField(
        max_length=10, choices=CADENCE_CHOICES, default=CADENCE_IMMEDIATE
    )
    new_projects = models.CharField(
        max_length=10,
        choices=CADENCE_CHOICES,
        default=CADENCE_WEEKLY,
        help_text=(
            "Every new project published on OSPREY, not just ones from "
            "people you follow. Off hides them from the in-app inbox too."
        ),
    )
    auto_follow_threads = models.BooleanField(
        default=True,
        help_text="Automatically follow threads you start or reply in.",
    )
    consented_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the user added their email address (the consent act).",
    )
    consent_source = models.CharField(
        max_length=40,
        blank=True,
        default="",
        help_text="Where the address was added: welcome flow or settings.",
    )
    last_daily_digest_at = models.DateTimeField(null=True, blank=True)
    last_weekly_digest_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"notification preferences for {self.user_id}"

    @classmethod
    def for_user(cls, user) -> "NotificationPreference":
        obj, _created = cls.objects.get_or_create(user=user)
        return obj

    def cadence_for(self, group: str) -> str:
        return getattr(self, group, CADENCE_OFF)


class QueuedEmail(models.Model):
    """Outbox row: one notification email waiting for the sender command.

    The recipient address is deliberately NOT stored here; it is resolved
    from the user's verified allauth address at send time, so removing
    the address really stops everything, including queued mail.
    """

    STATUS_QUEUED = "queued"
    STATUS_SENT = "sent"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_SENT, "Sent"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELLED, "Cancelled"),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="queued_emails",
        null=True,
        blank=True,
        help_text="Null for external mail (contributor invites), which "
        "carries its own to_address instead.",
    )
    to_address = models.EmailField(
        blank=True,
        default="",
        help_text="Raw recipient for external mail only. Ephemeral rows "
        "are deleted right after sending so the address isn't kept.",
    )
    ephemeral = models.BooleanField(
        default=False,
        help_text="Delete this row after the send attempt settles, "
        "instead of keeping it as a delivery record.",
    )
    group = models.CharField(max_length=20, default="")
    subject = models.CharField(max_length=300)
    body_text = models.TextField()
    status = models.CharField(
        max_length=12, choices=STATUS_CHOICES, default=STATUS_QUEUED
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    scheduled_for = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["scheduled_for", "id"]
        indexes = [models.Index(fields=["status", "scheduled_for"])]

    def __str__(self) -> str:
        who = self.user_id or self.to_address or "?"
        return f"{self.status} email to {who}: {self.subject[:60]}"


class Announcement(models.Model):
    """A service announcement broadcast to every active user.

    Written and sent from the admin: draft, send a test to yourself,
    then send to everyone. Sending creates an in-app notification for
    each active user and queues email for everyone with email enabled
    and a verified address. Cadence groups are deliberately ignored (a
    service notice must not wait for a digest); only the master email
    toggle gates the email. A sent announcement is frozen and can't be
    sent twice; duplicate the row to send a follow-up.
    """

    subject = models.CharField(max_length=200)
    body = models.TextField(
        help_text="Plain text. Sent as written, with the standard email "
        "footer appended."
    )
    url = models.CharField(
        max_length=500,
        blank=True,
        help_text="Optional link shown with the notification, e.g. /about/.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="announcements",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    test_sent_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_count = models.PositiveIntegerField(
        default=0, help_text="Users reached in-app on the real send."
    )
    emailed_count = models.PositiveIntegerField(
        default=0, help_text="Emails queued on the real send."
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        state = "sent" if self.sent_at else "draft"
        return f"[{state}] {self.subject[:60]}"

    @property
    def is_sent(self) -> bool:
        return self.sent_at is not None


class LoopHeartbeat(models.Model):
    """Singleton: is the notifier loop alive? Written every iteration so the
    staff dashboard can show it, and so a run of failures raises an alarm
    instead of filling a log nobody reads (decided 2026-09-29)."""

    last_ok_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")
    consecutive_failures = models.PositiveIntegerField(default=0)
    alerted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "notifier heartbeat"
        verbose_name_plural = "notifier heartbeat"

    def __str__(self) -> str:
        return "Notifier heartbeat"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "LoopHeartbeat":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def is_stale(self) -> bool:
        from datetime import timedelta

        from django.utils import timezone

        if self.last_ok_at is None:
            return True
        return timezone.now() - self.last_ok_at > timedelta(minutes=5)
