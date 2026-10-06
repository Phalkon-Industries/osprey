from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.core.validators import RegexValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone
import uuid

# 16-digit ORCID iD with hyphens, last char digit or X.
ORCID_VALIDATOR = RegexValidator(
    regex=r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$",
    message="ORCID iD must look like 0000-0000-0000-000X.",
)


import zlib

ARTIFACT_KIND_CHOICES = [
    ("github", "GitHub repository"),
    ("codeberg", "Codeberg repository"),
    ("zenodo", "Zenodo deposit"),
    ("pdf", "PDF"),
    ("cad", "CAD files"),
    ("dataset", "Dataset"),
    ("other", "Other"),
]

# Two relations, on purpose (design session 2026-09-03; see
# planning/features/lineage.md). "derived from" answers "did you start
# from their files or design?"; "uses" answers "does their project sit
# inside yours, unmodified?".
LINEAGE_RELATION_CHOICES = [
    ("derived_from", "derived from"),
    ("uses", "uses"),
]


# The project-maturity ladder. Single source of truth: the submission
# form builds its choices from this, and the project page renders the
# matching phrase next to the number. Levels are cumulative.
MATURITY_LEVELS = {
    1: "Doesn't work, kept as a record of the failure",
    2: "Only some parts work so far",
    3: "Has worked at least once, but not yet reliable",
    4: "Works reliably in the lab under test conditions",
    5: "Has worked in service conditions at least once",
    6: "Works reliably in service conditions",
    7: "In routine service as everyday equipment",
    8: "Reliable even in the hardest conditions it was built for",
    9: "Failures are rare and hold no surprises",
    10: "Trusted where failure is not an option",
}


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
    public_id = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text=(
            "Permanent OSPREY project identifier. Used in the project permalink "
            "(/p/<public_id>/) and embedded in Zenodo metadata so external records "
            "keep pointing at this project even if the slug or title changes."
        ),
    )
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
    doi = models.CharField(
        max_length=120,
        blank=True,
        help_text=(
            "Optional. The project's DOI as a bare identifier (e.g. "
            "10.5281/zenodo.1234567). If you've deposited this work on "
            "Zenodo, OSF, Figshare, or an institutional repository, paste "
            "the DOI here. Leave blank if you don't have one yet."
        ),
    )
    canonical_url = models.URLField(blank=True)
    # Where the record lives and who answers for it. Both kinds are "OSPREY
    # projects" to the public; the difference is who looks after Zenodo.
    ORIGIN_NATIVE = "native"
    ORIGIN_REGISTERED = "registered"
    ORIGIN_CHOICES = [
        (ORIGIN_NATIVE, "Native (published through OSPREY)"),
        (ORIGIN_REGISTERED, "Registered (the authors' own Zenodo record)"),
    ]
    origin = models.CharField(
        max_length=16, choices=ORIGIN_CHOICES, default=ORIGIN_NATIVE, db_index=True
    )
    # Provided as-is: a promise about support, not a verdict on quality
    # (decided 2026-09-29). Per project, editable any time.
    provided_as_is = models.BooleanField(default=False)
    # Owner's one-click mute of project activity notifications (questions,
    # use reports, wiki suggestions, lineage claims). Offered from the
    # as-is badge; works for any project.
    owner_activity_muted = models.BooleanField(default=False)
    # License audit (all kinds): OSPREY's license compared with the Zenodo
    # record and any linked GitHub repository. See projects/indexing/license_check.py.
    license_check = models.JSONField(default=dict, blank=True)
    license_checked_at = models.DateTimeField(null=True, blank=True)

    @property
    def license_findings(self) -> list:
        return list((self.license_check or {}).get("findings") or [])
    cover_image_url = models.URLField(
        blank=True,
        help_text=(
            "Optional. Link to a cover image hosted elsewhere. OSPREY will "
            "fetch and re-encode it to WebP. Prefer the upload below if you "
            "have the file locally."
        ),
    )
    cover_image = models.ImageField(
        upload_to="projects/cover/",
        blank=True,
        null=True,
        help_text=(
            "Optional. Upload a cover image (PNG/JPG/WebP). Max 10 MiB. "
            "OSPREY will downscale and re-encode it to WebP automatically."
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
    pending_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="pending_ownerships",
        help_text="Ownership transfer offered to this user; takes effect "
        "only when they accept. One pending transfer at a time.",
    )
    is_staff_hidden = models.BooleanField(
        default=False,
        help_text=(
            "Staff-only takedown flag. When true, the project is invisible to "
            "everyone except staff, regardless of the owner's visibility setting."
        ),
    )
    wiki_requires_approval = models.BooleanField(
        default=True,
        help_text=(
            "When true, edits from non-maintainers are queued as pending "
            "suggestions for review. Pending suggestions are still publicly "
            "visible on the page so others can see proposed changes."
        ),
    )
    institution = models.TextField(
        blank=True,
        help_text=(
            "Institutions or labs that this project belongs to. Free text. "
            "List multiple affiliations one per line or separated by commas."
        ),
    )
    funding = models.TextField(
        blank=True,
        help_text=(
            "Funding sources, grants, or sponsoring programs that supported "
            "this project. Free text. List multiple sources one per line."
        ),
    )
    self_rating = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text=(
            "Self-assessed maturity on the 1-10 ladder defined in "
            "MATURITY_LEVELS. Levels are cumulative: a level counts only "
            "if all lower levels also hold."
        ),
    )
    self_rating_note = models.TextField(
        blank=True,
        help_text=(
            "Optional. A sentence or two on why the project sits at its "
            "maturity level."
        ),
    )
    publications = models.TextField(
        blank=True,
        help_text=(
            "Papers, talks, or reports that used this project. Free text. "
            "One reference per line; include a URL or DOI when you have one."
        ),
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

    def save(self, *args, **kwargs):
        # Provided as-is means nobody reviews wiki suggestions: the wiki is
        # always open. Enforced here so no path can leave it in review mode.
        if self.provided_as_is and self.wiki_requires_approval:
            self.wiki_requires_approval = False
            fields = kwargs.get("update_fields")
            if fields is not None and "wiki_requires_approval" not in fields:
                kwargs["update_fields"] = list(fields) + ["wiki_requires_approval"]
        super().save(*args, **kwargs)
        fields = kwargs.get("update_fields")
        if fields is None or "readme" in fields:
            # README image captions live in the Markdown brackets.
            from .images import sync_captions_from_readme

            sync_captions_from_readme(self)

    def get_absolute_url(self) -> str:
        return reverse("projects:detail", args=[self.slug])

    def get_permalink(self) -> str:
        return reverse("project_permalink", args=[str(self.public_id)])

    @property
    def self_rating_label(self) -> str:
        """The maturity ladder phrase for this project's level, or ""."""
        return MATURITY_LEVELS.get(self.self_rating, "")

    @property
    def institutions(self) -> list[str]:
        from people.identity import split_institutions

        return split_institutions(self.institution)

    @property
    def cover_hue(self) -> int:
        """Stable 0-359 hue derived from the slug, for the generated
        cover shown when a project has no cover image."""
        return zlib.crc32(self.slug.encode("utf-8")) % 360

    @property
    def gallery_images(self) -> list:
        """Gallery images in order. Iterates the prefetched set when there is one."""
        return [i for i in self.images.all() if i.kind == "gallery"]

    @property
    def cover(self):
        """The first gallery image: the cover on cards and at the top of the page."""
        gallery = self.gallery_images
        return gallery[0] if gallery else None

    @property
    def cover_image_display_url(self) -> str:
        """URL of the cover at display size. The first gallery image; the old
        separate cover fields are a fallback for rows not yet migrated."""
        cover = self.cover
        if cover is not None:
            return cover.image.url
        if self.cover_image:
            try:
                return self.cover_image.url
            except ValueError:
                return ""
        return self.cover_image_url or ""

    @property
    def card_image_url(self) -> str:
        """Small cover for cards."""
        cover = self.cover
        if cover is not None:
            return cover.thumb_url
        return self.cover_image_display_url

    @property
    def canonical_url_label(self) -> str:
        """Short label for the canonical URL pill (GitHub, Codeberg, etc.)."""
        url = (self.canonical_url or "").lower()
        if not url:
            return ""
        if "github.com" in url:
            return "GitHub"
        if "codeberg.org" in url:
            return "Codeberg"
        if "gitlab" in url:
            return "GitLab"
        if "bitbucket" in url:
            return "Bitbucket"
        if "zenodo.org" in url:
            return "Zenodo"
        return "Repository"

    @property
    def is_public(self) -> bool:
        return self.visibility == self.VISIBILITY_PUBLIC

    def viewable_by(self, user) -> bool:
        if self.is_staff_hidden:
            return bool(user and user.is_authenticated and user.is_staff)
        if self.is_public:
            return True
        if user is None or not user.is_authenticated:
            return False
        if user.is_staff:
            return True
        if (
            self.contributions.filter(user=user).exists()
            or self.created_by_id == user.id
        ):
            return True
        # Being listed (by ORCID iD) grants quiet view access to drafts:
        # the owner deliberately attached that exact iD, and asking
        # someone to confirm credit on a draft they can't see would be
        # backwards. Declined listings give it up.
        uids = list(
            user.socialaccount_set.filter(provider="orcid").values_list(
                "uid", flat=True
            )
        )
        if uids:
            return (
                self.contributions.exclude(claim_status="declined")
                .filter(orcid_id__in=uids)
                .exists()
            )
        return False

    def editable_by(self, user) -> bool:
        """Owner, staff, or a verified contributor with the editor flag.

        Credit and access are decoupled (decided 2026-09-04): being
        listed never confers edit rights by itself; the owner grants
        them per contributor via the editor flag.
        """
        if user is None or not user.is_authenticated:
            return False
        if user.is_staff:
            return True
        if self.created_by_id == user.id:
            return True
        return self.contributions.filter(
            user=user, claim_status="verified", editor=True
        ).exists()

    def publishable_by(self, user) -> bool:
        """Publishing (and new versions) mint DOIs; only the owner (or
        staff) holds that irreversible click. Owner-only controls
        (contributor list, transfer) key off this too."""
        if user is None or not user.is_authenticated:
            return False
        return user.is_staff or self.created_by_id == user.id

    @property
    def credited_contributions(self):
        """Contributor rows that count as credit: everything except rows
        whose person declined. Declined rows stay for the owner's form and
        for staff, but never render publicly, never reach Zenodo, and
        never enter the citation."""
        return self.contributions.exclude(claim_status="declined")

    @property
    def is_registered(self) -> bool:
        return self.origin == self.ORIGIN_REGISTERED

    @property
    def accepts_publish(self) -> bool:
        """Whether OSPREY publishes this project's record. A registered project
        is published and versioned on Zenodo by its authors."""
        return self.origin == self.ORIGIN_NATIVE

    @property
    def normalized_doi(self) -> str:
        """Return the bare DOI after stripping common pasted prefixes."""
        doi = (self.doi or "").strip()
        for prefix in (
            "https://doi.org/",
            "http://doi.org/",
            "https://dx.doi.org/",
            "http://dx.doi.org/",
            "doi:",
        ):
            if doi.lower().startswith(prefix):
                return doi[len(prefix) :]
        return doi

    @property
    def doi_url(self) -> str:
        """Return https://doi.org/<doi> when a DOI is set, else empty string."""
        doi = self.normalized_doi
        if not doi:
            return ""
        return f"https://doi.org/{doi}"

    @property
    def is_zenodo_doi(self) -> bool:
        """True if the DOI looks like a Zenodo DOI (production or sandbox)."""
        doi = self.normalized_doi.lower()
        return doi.startswith("10.5281/zenodo.") or doi.startswith("10.5072/zenodo.")

    @property
    def zenodo_badge_url(self) -> str:
        """Return the right Zenodo DOI badge URL for production or sandbox."""
        doi = self.normalized_doi
        if not doi:
            return ""
        host = (
            "https://sandbox.zenodo.org"
            if doi.lower().startswith("10.5072/zenodo.")
            else "https://zenodo.org"
        )
        return f"{host}/badge/DOI/{doi}.svg"


class ProjectDeposit(models.Model):
    """A project deposit managed through an external repository API."""

    PROVIDER_ZENODO = "zenodo"
    PROVIDER_CHOICES = [(PROVIDER_ZENODO, "Zenodo")]

    STATE_DRAFT = "draft"
    STATE_PUBLISHED = "published"
    STATE_ERROR = "error"
    STATE_CHOICES = [
        (STATE_DRAFT, "Draft"),
        (STATE_PUBLISHED, "Published"),
        (STATE_ERROR, "Error"),
    ]

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="deposits"
    )
    provider = models.CharField(
        max_length=40, choices=PROVIDER_CHOICES, default=PROVIDER_ZENODO
    )
    sandbox = models.BooleanField(default=True)
    deposition_id = models.CharField(max_length=80, blank=True)
    bucket_url = models.URLField(blank=True)
    record_id = models.CharField(max_length=80, blank=True)
    concept_id = models.CharField(max_length=80, blank=True)
    doi = models.CharField(max_length=120, blank=True)
    concept_doi = models.CharField(max_length=120, blank=True)
    state = models.CharField(max_length=20, choices=STATE_CHOICES, default=STATE_DRAFT)
    pending_changelog = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Changelog text the user supplied for the in-flight new-version draft. "
            "Cleared on publish; the published value lives on ProjectDepositVersion."
        ),
    )
    repo_link = models.URLField(
        blank=True,
        default="",
        help_text=(
            "Optional per-version repository URL (e.g. a GitHub release tag) "
            "for the in-flight draft. Cleared on publish."
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_project_deposits",
    )
    last_response = models.JSONField(default=dict, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)
    # False for a registered project: the record belongs to its authors and
    # OSPREY only reads it.
    managed = models.BooleanField(default=True)
    zenodo_synced_at = models.DateTimeField(null=True, blank=True)
    in_community = models.BooleanField(null=True, blank=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "provider", "sandbox"],
                name="unique_project_deposit_per_provider_mode",
            )
        ]
        indexes = [
            models.Index(
                fields=["provider", "sandbox", "state"],
                name="projects_deposit_state_idx",
            ),
        ]

    def __str__(self) -> str:
        mode = "sandbox" if self.sandbox else "production"
        return f"{self.project} on {self.get_provider_display()} ({mode})"

    @property
    def external_url(self) -> str:
        base = "https://sandbox.zenodo.org" if self.sandbox else "https://zenodo.org"
        if self.record_id:
            return f"{base}/records/{self.record_id}"
        if self.deposition_id:
            return f"{base}/deposit/{self.deposition_id}"
        return ""

    @property
    def latest_version(self):
        return self.versions.order_by("-version_index").first()

    @property
    def has_pending_new_version(self) -> bool:
        """True when a new-version draft is in flight after a previous publish."""
        return (
            self.state == self.STATE_DRAFT
            and self.versions.exists()
            and bool(self.pending_changelog)
        )

    @property
    def next_version_index(self) -> int:
        last = self.latest_version
        return (last.version_index if last else 0) + 1


class ZenodoJob(models.Model):
    """One unit of Zenodo work run outside the request by the notifier loop.

    Publishing, publishing a new version, and syncing edited metadata all
    used to run inside the user's request, so a Zenodo outage meant error
    screens and stuck saves. Now the request records a job and returns;
    `projects.zenodo_jobs` runs it with retries and tells the owner how it
    ended. All state is here so the loop can restart at any moment.
    """

    KIND_PUBLISH = "publish"
    KIND_NEW_VERSION = "new_version"
    KIND_METADATA_SYNC = "metadata_sync"
    KIND_CHOICES = [
        (KIND_PUBLISH, "Publish"),
        (KIND_NEW_VERSION, "Publish new version"),
        (KIND_METADATA_SYNC, "Sync metadata"),
    ]
    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_DONE, "Done"),
        (STATUS_FAILED, "Failed"),
    ]
    PENDING_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="zenodo_jobs"
    )
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=STATUS_QUEUED
    )
    payload = models.JSONField(default=dict, blank=True)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="zenodo_jobs",
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="projects_zjob_due_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} for {self.project} ({self.status})"

    @property
    def is_pending(self) -> bool:
        return self.status in self.PENDING_STATUSES


class ProjectDepositVersion(models.Model):
    """A published version of a `ProjectDeposit`.

    Each new-version publish records one row here. The current `ProjectDeposit`
    always tracks the most recent draft/published deposition for a project;
    the version history lives here.
    """

    deposit = models.ForeignKey(
        ProjectDeposit, on_delete=models.CASCADE, related_name="versions"
    )
    version_index = models.PositiveIntegerField()
    deposition_id = models.CharField(max_length=80, blank=True)
    record_id = models.CharField(max_length=80, blank=True)
    doi = models.CharField(max_length=120, blank=True)
    changelog = models.TextField(
        blank=True,
        default="",
        help_text="What changed in this version. Plain text or Markdown.",
    )
    repo_link = models.URLField(
        blank=True,
        default="",
        help_text="Optional repository URL pointing at the release for this version.",
    )
    published_at = models.DateTimeField()
    last_response = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-version_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["deposit", "version_index"],
                name="unique_deposit_version_index",
            )
        ]
        indexes = [
            models.Index(
                fields=["deposit", "version_index"], name="projects_dep_version_idx"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.deposit.project} v{self.version_index} ({self.doi or 'no DOI'})"

    @property
    def external_url(self) -> str:
        base = (
            "https://sandbox.zenodo.org"
            if self.deposit.sandbox
            else "https://zenodo.org"
        )
        if self.record_id:
            return f"{base}/records/{self.record_id}"
        if self.deposition_id:
            return f"{base}/deposit/{self.deposition_id}"
        return ""


class Contribution(models.Model):
    """A credit row on a project.

    Listing is a claim about a person, so it needs their consent before
    it links anywhere: a row starts unclaimed, the person is invited
    (manually at draft stage or automatically at publish), and only
    their acceptance sets `user` and shows the row as verified. The
    `editor` flag is the project's whole access model: edit rights are
    owner + verified rows with editor on, nothing else. Pre-granting is
    fine; the flag has no effect until the row verifies.
    """

    CLAIM_UNCLAIMED = "unclaimed"
    CLAIM_INVITED = "invited"
    CLAIM_VERIFIED = "verified"
    CLAIM_DECLINED = "declined"
    CLAIM_DISPUTED = "disputed"
    CLAIM_CHOICES = [
        (CLAIM_UNCLAIMED, "Not invited"),
        (CLAIM_INVITED, "Confirmation requested"),
        (CLAIM_VERIFIED, "Accepted"),
        (CLAIM_DECLINED, "Declined"),
        (CLAIM_DISPUTED, "Removal requested"),
    ]

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="contributions"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="contributions",
        help_text="Filled in when a named contributor row is claimed or attached.",
    )
    orcid_id = models.CharField(
        max_length=19,
        validators=[ORCID_VALIDATOR],
        blank=True,
        default="",
        help_text="Verified ORCID iD, populated from ORCID sign-in rather than manual entry.",
    )
    display_name = models.CharField(
        max_length=200,
        help_text="Name as it should appear on the project page.",
    )
    role = models.CharField(
        max_length=80,
        default="Project lead",
        help_text="Free text. Pick from the suggestions or write your own.",
    )
    affiliation = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text=(
            "Institution this person was affiliated with for this project. "
            "Free text. Different contributors can list different institutions."
        ),
    )
    claim_status = models.CharField(
        max_length=12, choices=CLAIM_CHOICES, default=CLAIM_UNCLAIMED
    )
    editor = models.BooleanField(
        default=False,
        help_text="Owner-granted edit access. Takes effect only once the "
        "row is verified.",
    )
    invited_at = models.DateTimeField(null=True, blank=True)
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
                condition=~models.Q(orcid_id=""),
            )
        ]
        indexes = [
            models.Index(fields=["orcid_id"], name="projects_co_orcid_i_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.role}) on {self.project}"

    @property
    def verified_orcid_id(self) -> str:
        if not self.orcid_id or not self.user_id:
            return ""
        account = self.user.socialaccount_set.filter(provider="orcid").first()
        if account is None:
            return ""
        identifier = (account.extra_data or {}).get("orcid-identifier") or {}
        account_orcid = (identifier.get("path") or "").strip()
        if account_orcid == self.orcid_id:
            return self.orcid_id
        return ""

    # Note: rows are never auto-registered to users anymore. Registering happens
    # only through the claim flow (invite -> the person accepts), so a
    # listing can't attach to someone's profile without their consent.


class ArtifactLink(models.Model):
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="artifact_links"
    )
    kind = models.CharField(
        max_length=40, choices=ARTIFACT_KIND_CHOICES, default="github"
    )
    url = models.URLField()
    label = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["kind", "label"]

    def __str__(self) -> str:
        return self.label or self.url


def _attachment_upload_path(instance, filename: str) -> str:
    return f"projects/{instance.project.slug}/files/{filename}"


class ProjectAttachment(models.Model):
    """A file the user attached to a project.

    Files live on OSPREY's media volume only while the project is a draft.
    On publish, OSPREY uploads each attachment to the Zenodo deposit's file
    bucket. After a successful Zenodo upload `published_to_zenodo` is set
    True and the local file is cleared, since Zenodo becomes the host.
    """

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="attachments"
    )
    file = models.FileField(upload_to=_attachment_upload_path, blank=True)
    filename = models.CharField(max_length=255, blank=True)
    size_bytes = models.BigIntegerField(default=0)
    label = models.CharField(max_length=200, blank=True)
    published_to_zenodo = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return self.filename or self.label or f"attachment-{self.pk}"

    def save(self, *args, **kwargs):
        if self.file and not self.filename:
            # Strip directory components in case the FileField returns a
            # path on later edits.
            self.filename = self.file.name.rsplit("/", 1)[-1]
        if self.file and not self.size_bytes:
            try:
                self.size_bytes = self.file.size
            except (OSError, ValueError):
                self.size_bytes = 0
        super().save(*args, **kwargs)


class LineageEdge(models.Model):
    """A lineage claim, declared from the child side.

    Edges are immutable facts once made: both ends carry version pins
    (attached at claim time for the parent, at publish for the child),
    corrections happen by withdrawing and re-declaring, and a withdrawn
    edge stays in the table forever with its label. A claim is live from
    the moment it's made (dispute-only model, decided 2026-09-04): the
    parent project's team is notified and can dispute it at any time,
    and retract the dispute. Edges declared while the child is still a
    draft stay dormant (claimed_at null) and go live at publish.
    """

    STATUS_ACTIVE = "active"
    STATUS_DISPUTED = "disputed"
    STATUS_WITHDRAWN = "withdrawn"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_DISPUTED, "Disputed by the parent project"),
        (STATUS_WITHDRAWN, "Withdrawn"),
    ]

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

    parent_version = models.ForeignKey(
        "ProjectDepositVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lineage_as_parent",
        help_text="Which published version of the parent the claim points at.",
    )
    child_version = models.ForeignKey(
        "ProjectDepositVersion",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lineage_as_child",
        help_text="Which version of the child made the claim. Set at publish.",
    )

    declared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lineage_claims",
    )
    declared_at = models.DateTimeField(default=timezone.now)
    claimed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When the claim went live and the parent was notified. "
        "Null while the child is still a draft.",
    )
    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE
    )
    dispute_reason = models.CharField(max_length=500, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-declared_at"]
        constraints = [
            models.CheckConstraint(
                check=~models.Q(parent=models.F("child")),
                name="lineage_no_self_edge",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.child} {self.get_relation_display()} {self.parent}"

    @property
    def is_withdrawn(self) -> bool:
        return self.status == self.STATUS_WITHDRAWN


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
    return f"projects/{instance.project_id}/images/{filename}"


class ProjectImage(models.Model):
    """One image in a project's set. Gallery images show at the top of the
    project page, and the first one is the cover everywhere. README images
    are inserted into the README and kept out of the gallery. Three WebP
    sizes; see projects/images.py."""

    KIND_GALLERY = "gallery"
    KIND_README = "readme"
    KIND_CHOICES = [(KIND_GALLERY, "Gallery"), (KIND_README, "README")]

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="images"
    )
    kind = models.CharField(max_length=8, choices=KIND_CHOICES, default=KIND_GALLERY, db_index=True)
    # Display size (1600 px long edge). Kept as `image` for older code paths.
    image = models.ImageField(upload_to=project_image_upload_to)
    thumb = models.ImageField(upload_to=project_image_upload_to, blank=True)
    full = models.ImageField(upload_to=project_image_upload_to, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    caption = models.CharField(max_length=300, blank=True)
    order = models.PositiveIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    @property
    def thumb_url(self) -> str:
        return (self.thumb or self.image).url

    @property
    def full_url(self) -> str:
        return (self.full or self.image).url

    class Meta:
        ordering = ["order", "id"]

    def __str__(self) -> str:
        return self.caption or f"Image #{self.pk} for {self.project}"


class Citation(models.Model):
    """A paper, talk, or other work that cites this project.

    For the demo these are entered manually. A future federation pass will
    pull citations from upstream sources (Crossref, ORCID works, user
    use reports from §planning/features/reuse-attestations.md).
    """

    SOURCE_MAINTAINER = "maintainer"
    SOURCE_USER = "user"
    SOURCE_USE_REPORT = "use_report"
    SOURCE_CHOICES = [
        (SOURCE_MAINTAINER, "Maintainer-added"),
        (SOURCE_USER, "User-submitted"),
        (SOURCE_USE_REPORT, "From use report"),
    ]

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="citations"
    )
    text = models.CharField(
        max_length=600,
        help_text="Plain-text citation as it would appear in a references list.",
    )
    url = models.URLField(blank=True)
    doi = models.CharField(max_length=120, blank=True)
    year = models.PositiveIntegerField(null=True, blank=True)
    order = models.PositiveIntegerField(default=0)
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default=SOURCE_MAINTAINER
    )
    submitted_by = models.ForeignKey(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_citations",
    )
    use_report = models.ForeignKey(
        "use_reports.UseReport",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="citations",
    )
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "-year", "id"]

    def __str__(self) -> str:
        return self.text[:80]


class Watch(models.Model):
    """Subscribes a user to a project's activity: new versions, new wiki
    pages, and new use reports. A bookmark is a different (future) thing;
    this is a notification subscription."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="watching",
    )
    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="watchers"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "project"], name="unique_watch"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id} watches {self.project_id}"
