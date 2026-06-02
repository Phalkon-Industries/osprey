from __future__ import annotations

from django.conf import settings
from django.db import models

from projects.models import Project


class UseReport(models.Model):
    """A user's first-hand story of using a project.

    Use reports sit alongside citations. They are not peer-reviewed and
    they are not approval-gated; every visible use report is the
    author's own words. Staff can hide one if it is abusive or
    off-topic. Maintainer-side endorsement is intentionally not part
    of this model right now; see
    planning/features/reuse-attestations.md for the open social-design
    question.
    """

    VIS_PUBLIC = "public"
    VIS_HIDDEN = "hidden"
    VIS_CHOICES = [(VIS_PUBLIC, "Public"), (VIS_HIDDEN, "Hidden by staff")]

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="use_reports"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="use_reports",
    )
    narrative = models.TextField(help_text="What did you do with this project?")
    used_at = models.CharField(
        max_length=80,
        blank=True,
        default="",
        help_text="When or where you used it. Free text.",
    )
    visibility = models.CharField(
        max_length=10, choices=VIS_CHOICES, default=VIS_PUBLIC
    )
    moderator_note = models.CharField(max_length=400, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["project", "visibility", "-created_at"]),
        ]
        verbose_name = "use report"
        verbose_name_plural = "use reports"

    def __str__(self) -> str:
        author = self.author.get_username() if self.author else "anonymous"
        return f"Use report by {author} on {self.project.slug}"

    @property
    def is_visible(self) -> bool:
        return self.visibility == self.VIS_PUBLIC
