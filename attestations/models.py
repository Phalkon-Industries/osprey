from __future__ import annotations

from django.conf import settings
from django.db import models

from projects.models import Project


class Attestation(models.Model):
    """A user's first-hand story of using a project.

    Attestations sit alongside citations. They are not peer-reviewed and
    they are not approval-gated; maintainers can endorse, feature, or
    hide them, but every visible attestation is the author's own words.
    """

    VIS_PUBLIC = "public"
    VIS_HIDDEN = "hidden"
    VIS_CHOICES = [(VIS_PUBLIC, "Public"), (VIS_HIDDEN, "Hidden by maintainer")]

    ENDORSE_NONE = ""
    ENDORSE_ACKNOWLEDGED = "acknowledged"
    ENDORSE_FEATURED = "featured"
    ENDORSE_CHOICES = [
        (ENDORSE_NONE, "Not endorsed"),
        (ENDORSE_ACKNOWLEDGED, "Acknowledged"),
        (ENDORSE_FEATURED, "Featured"),
    ]

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="attestations"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="attestations",
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
    endorsement = models.CharField(
        max_length=20, choices=ENDORSE_CHOICES, default=ENDORSE_NONE, blank=True
    )
    endorsed_at = models.DateTimeField(null=True, blank=True)
    endorsed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    moderator_note = models.CharField(max_length=400, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["project", "visibility", "-created_at"]),
        ]

    def __str__(self) -> str:
        author = self.author.get_username() if self.author else "anonymous"
        return f"Attestation by {author} on {self.project.slug}"

    @property
    def is_visible(self) -> bool:
        return self.visibility == self.VIS_PUBLIC
