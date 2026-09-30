from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


def _avatar_upload_to(instance: "Profile", filename: str) -> str:
    return f"avatars/{instance.user_id}/{filename}"


class Profile(models.Model):
    """Per-user metadata layered on top of Django's built-in auth.User."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    display_name = models.CharField(max_length=200, blank=True)
    bio = models.TextField(
        blank=True,
        help_text="Optional short bio. Markdown is supported.",
    )
    avatar = models.ImageField(
        upload_to=_avatar_upload_to,
        blank=True,
        null=True,
    )
    avatar_focal_x = models.PositiveSmallIntegerField(
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Horizontal focal point for avatar crops, 0-100%.",
    )
    avatar_focal_y = models.PositiveSmallIntegerField(
        default=50,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Vertical focal point for avatar crops, 0-100%.",
    )
    avatar_zoom = models.DecimalField(
        max_digits=3,
        decimal_places=2,
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(3)],
        help_text="Display zoom for avatar crops. 1 is no extra zoom.",
    )
    orcid_placeholder = models.CharField(
        max_length=40,
        blank=True,
        db_index=True,
        help_text="String form of an ORCID iD; becomes the verified iD once OAuth lands.",
    )
    institution = models.TextField(
        blank=True,
        help_text=(
            "Institutions or labs you are affiliated with. Free text. "
            "List multiple affiliations one per line or separated by commas."
        ),
    )
    usertag_locked = models.BooleanField(
        default=False,
        help_text="Whether the user's local @ tag has been finalized.",
    )
    suspended_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp the account was suspended by staff. The auth.User.is_active flag is the gate.",
    )
    suspended_reason = models.TextField(
        blank=True,
        help_text="Staff-only note explaining why this account is suspended.",
    )

    def __str__(self) -> str:
        return self.display_name or self.user.get_username()

    @property
    def institutions(self) -> list[str]:
        from .identity import split_institutions

        return split_institutions(self.institution)

    def save(self, *args, **kwargs):
        prev_orcid = None
        if self.pk:
            prev_orcid = (
                type(self)
                .objects.filter(pk=self.pk)
                .values_list("orcid_placeholder", flat=True)
                .first()
            )
        super().save(*args, **kwargs)
        # When a Profile's ORCID is set or changed, attach any existing
        # stub Contribution rows that match.
        if self.orcid_placeholder and self.orcid_placeholder != prev_orcid:
            from projects.models import Contribution

            Contribution.objects.filter(
                orcid_id=self.orcid_placeholder, user__isnull=True
            ).update(user=self.user)


class Follow(models.Model):
    """One researcher following another's new published work."""

    follower = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="following",
    )
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="followers",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["follower", "creator"], name="unique_follow"
            ),
            models.CheckConstraint(
                check=~models.Q(follower=models.F("creator")),
                name="no_self_follow",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.follower_id} follows {self.creator_id}"


class SignupGate(models.Model):
    """Singleton: the staff switch that limits ORCID sign-in to records with
    a verified institutional email domain. The emergency brake for a bot
    wave. Takes effect on the next sign-in; no restart, no env setting.
    """

    SCOPE_NEW = "new"
    SCOPE_ALL = "all"
    SCOPE_CHOICES = [
        (SCOPE_NEW, "New accounts only"),
        (SCOPE_ALL, "Every sign-in, existing accounts too"),
    ]

    enabled = models.BooleanField(default=False)
    scope = models.CharField(max_length=4, choices=SCOPE_CHOICES, default=SCOPE_NEW)
    allowlist = models.TextField(
        blank=True,
        default="",
        help_text="ORCID iDs that bypass the check, one per line.",
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = "sign-up gate"
        verbose_name_plural = "sign-up gate"

    def __str__(self) -> str:
        return "Sign-up gate"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "SignupGate":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def allowlisted(self) -> set[str]:
        return {line.strip() for line in self.allowlist.splitlines() if line.strip()}
