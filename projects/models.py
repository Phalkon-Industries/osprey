from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.core.validators import RegexValidator
from django.db import models
from django.urls import reverse


# 16-digit ORCID iD with hyphens, last char digit or X.
ORCID_VALIDATOR = RegexValidator(
    regex=r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$",
    message="ORCID iD must look like 0000-0000-0000-000X.",
)


ARTIFACT_KIND_CHOICES = [
    ("github", "GitHub repository"),
    ("codeberg", "Codeberg repository"),
    ("zenodo", "Zenodo deposit"),
    ("pdf", "PDF"),
    ("cad", "CAD files"),
    ("dataset", "Dataset"),
    ("other", "Other"),
]

LINEAGE_RELATION_CHOICES = [
    ("derived_from", "derived from"),
    ("inspired_by", "inspired by"),
    ("forked_from", "forked from"),
    ("replaces", "replaces"),
]


# Free text on the form, but these surface as a `<datalist>` for hints.
CONTRIBUTION_ROLE_SUGGESTIONS = [
    "Author",
    "Maintainer",
    "Project lead",
    "Designer",
    "Software",
    "Firmware",
    "Hardware",
    "Documentation",
    "Data analysis",
    "Reviewer",
    "Advisor",
    "Funding",
    "Contributor",
    "Other (explain in credit statement)",
]


class Tag(models.Model):
    name = models.CharField(max_length=80, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Project(models.Model):
    VISIBILITY_PRIVATE = "private"
    VISIBILITY_PUBLIC = "public"
    VISIBILITY_CHOICES = [
        (VISIBILITY_PRIVATE, "Private (only contributors and staff)"),
        (VISIBILITY_PUBLIC, "Public (visible to everyone)"),
    ]

    slug = models.SlugField(max_length=120, unique=True)
    title = models.CharField(max_length=300)
    summary = models.CharField(
        max_length=280,
        blank=True,
        help_text="One-line plain-English description. Used for cards and search.",
    )
    description = models.TextField(
        blank=True,
        help_text="Deprecated. Kept for backwards compatibility; new content goes in summary or readme.",
    )
    readme = models.TextField(
        blank=True,
        help_text="Long-form project README. Markdown is supported.",
    )
    artifact_type = models.CharField(max_length=40, blank=True)
    field = models.CharField(max_length=80, blank=True)
    license = models.CharField(
        max_length=80,
        blank=True,
        help_text="SPDX identifier or short name (e.g. MIT, CERN-OHL-S-2.0, CC-BY-4.0).",
    )
    placeholder_doi = models.CharField(
        max_length=80,
        blank=True,
        help_text="Spoofed DOI of the form 10.demo/<slug> until DataCite is wired up.",
    )
    canonical_url = models.URLField(blank=True)
    cover_image_url = models.URLField(
        blank=True,
        help_text=(
            "Optional. Link to a cover image hosted elsewhere "
            "(GitHub raw URL, Zenodo, lab website). OSPREY does not host images."
        ),
    )
    cover_image_focal_x = models.PositiveSmallIntegerField(
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Horizontal focal point for cover-image crops, 0-100%.",
    )
    cover_image_focal_y = models.PositiveSmallIntegerField(
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Vertical focal point for cover-image crops, 0-100%.",
    )
    cover_image_zoom = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(3)],
        help_text="Display zoom for cover-image crops. 1 is no extra zoom.",
    )
    visibility = models.CharField(
        max_length=10,
        choices=VISIBILITY_CHOICES,
        default=VISIBILITY_PRIVATE,
        help_text="Drafts default to private. Flip to public when ready to share.",
    )
    institution = models.CharField(
        max_length=200,
        blank=True,
        help_text="Institution or lab name. Free text for now.",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_projects",
    )
    contributors = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="Contribution",
        related_name="projects",
    )
    tags = models.ManyToManyField(Tag, through="TagAssignment", related_name="projects")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self) -> str:
        return self.title

    def get_absolute_url(self) -> str:
        return reverse("projects:detail", args=[self.slug])

    @property
    def is_public(self) -> bool:
        return self.visibility == self.VISIBILITY_PUBLIC

    def viewable_by(self, user) -> bool:
        if self.is_public:
            return True
        if user is None or not user.is_authenticated:
            return False
        if user.is_staff:
            return True
        return self.contributions.filter(user=user).exists() or self.created_by_id == user.id

    def editable_by(self, user) -> bool:
        if user is None or not user.is_authenticated:
            return False
        if user.is_staff:
            return True
        return self.contributions.filter(user=user).exists() or self.created_by_id == user.id


class Contribution(models.Model):
    """A credit row on a project. ORCID is mandatory; user FK fills in
    automatically when a Profile with the matching ORCID exists or is
    created later.
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="contributions")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contributions",
        help_text="Filled in once a user with this ORCID iD exists on OSPREY.",
    )
    orcid_id = models.CharField(
        max_length=19,
        validators=[ORCID_VALIDATOR],
        help_text="ORCID iD of the contributor. Required.",
    )
    display_name = models.CharField(
        max_length=200,
        help_text="Name as it should appear on the project page.",
    )
    role = models.CharField(
        max_length=80,
        default="Author",
        help_text="Free text. Pick from the suggestions or write your own.",
    )
    credit_statement = models.CharField(
        max_length=400,
        blank=True,
        default="",
        help_text="Optional. A sentence describing what this person did.",
    )
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "orcid_id"],
                name="unique_contribution_per_project",
            )
        ]
        indexes = [
            models.Index(fields=["orcid_id"], name="projects_co_orcid_i_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.role}) on {self.project}"

    def save(self, *args, **kwargs):
        if self.orcid_id and self.user_id is None:
            # Try to attach an existing user with this ORCID iD.
            from people.models import Profile  # avoid circular import at module load
            profile = (
                Profile.objects.filter(orcid_placeholder=self.orcid_id)
                .select_related("user")
                .first()
            )
            if profile is not None:
                self.user = profile.user
        super().save(*args, **kwargs)


class ArtifactLink(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="artifact_links")
    kind = models.CharField(max_length=40, choices=ARTIFACT_KIND_CHOICES, default="github")
    url = models.URLField()
    label = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["kind", "label"]

    def __str__(self) -> str:
        return self.label or self.url


class LineageEdge(models.Model):
    """Directed edge from a child project to one of its parents."""

    parent = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="lineage_children",
    )
    child = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="lineage_parents",
    )
    relation = models.CharField(
        max_length=40,
        choices=LINEAGE_RELATION_CHOICES,
        default="derived_from",
    )
    note = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["parent", "child", "relation"],
                name="unique_lineage_edge",
            ),
            models.CheckConstraint(
                check=~models.Q(parent=models.F("child")),
                name="lineage_no_self_edge",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.child} {self.get_relation_display()} {self.parent}"


class TagAssignment(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "tag"],
                name="unique_tag_assignment",
            )
        ]

    def __str__(self) -> str:
        return f"{self.tag} on {self.project}"


def project_image_upload_to(instance: "ProjectImage", filename: str) -> str:
    return f"projects/{instance.project.slug}/images/{filename}"


class ProjectImage(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="images")
    image = models.ImageField(upload_to=project_image_upload_to)
    caption = models.CharField(max_length=300, blank=True)
    order = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return self.caption or f"Image #{self.pk} for {self.project}"


class Citation(models.Model):
    """A paper, talk, or other work that cites this project.

    For the demo these are entered manually. A future federation pass will
    pull citations from upstream sources (Crossref, ORCID works, manual
    attestations from §planning/features/reuse-attestations.md).
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="citations")
    text = models.CharField(
        max_length=600,
        help_text="Plain-text citation as it would appear in a references list.",
    )
    url = models.URLField(blank=True)
    doi = models.CharField(max_length=120, blank=True)
    year = models.PositiveIntegerField(null=True, blank=True)
    order = models.PositiveIntegerField(default=0)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "-year", "id"]

    def __str__(self) -> str:
        return self.text[:80]
