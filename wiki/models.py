from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.text import slugify

from projects.models import Project


class WikiPage(models.Model):
    """A single page in a project's wiki.

    The wiki is open by default: any signed-in user can publish edits
    directly. When `Project.wiki_requires_approval` is true, edits from
    non-maintainers land as `WikiRevision` rows with status=`pending`
    and don't update the page body until a maintainer accepts them.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="wiki_pages"
    )
    slug = models.SlugField(max_length=120)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default="")
    is_landing = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        unique_together = [("project", "slug")]
        ordering = ["title"]

    def __str__(self) -> str:
        return f"{self.project.slug}:{self.slug}"

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title) or "page"
            slug = base
            n = 2
            while (
                WikiPage.objects.filter(project=self.project, slug=slug)
                .exclude(pk=self.pk)
                .exists()
            ):
                slug = f"{base}-{n}"
                n += 1
            self.slug = slug
        super().save(*args, **kwargs)


class WikiRevision(models.Model):
    """A revision proposed for or applied to a WikiPage.

    Open-default wikis still store every accepted revision so the
    history is preserved and survivable through `/api/v1/export/`.
    """

    STATUS_APPLIED = "applied"
    STATUS_PENDING = "pending"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_APPLIED, "Applied"),
        (STATUS_PENDING, "Pending review"),
        (STATUS_REJECTED, "Rejected"),
    ]

    page = models.ForeignKey(
        WikiPage, on_delete=models.CASCADE, related_name="revisions"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="wiki_revisions",
    )
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default="")
    base_title = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Page title at the moment this revision was started.",
    )
    base_body = models.TextField(
        blank=True,
        default="",
        help_text="Page body at the moment this revision was started.",
    )
    summary = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="One-line note on what changed.",
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_APPLIED
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["page", "status"]),
        ]

    def __str__(self) -> str:
        return f"rev {self.pk} ({self.status}) on {self.page}"
