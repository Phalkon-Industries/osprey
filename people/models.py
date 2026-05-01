from django.conf import settings
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
    orcid_placeholder = models.CharField(
        max_length=40,
        blank=True,
        db_index=True,
        help_text="String form of an ORCID iD; becomes the verified iD once OAuth lands.",
    )
    institution = models.CharField(
        max_length=200,
        blank=True,
        help_text="Institution or lab name. Free text for now.",
    )

    def __str__(self) -> str:
        return self.display_name or self.user.get_username()

    def save(self, *args, **kwargs):
        prev_orcid = None
        if self.pk:
            prev_orcid = (
                type(self).objects.filter(pk=self.pk).values_list("orcid_placeholder", flat=True).first()
            )
        super().save(*args, **kwargs)
        # When a Profile's ORCID is set or changed, attach any existing
        # stub Contribution rows that match.
        if self.orcid_placeholder and self.orcid_placeholder != prev_orcid:
            from projects.models import Contribution
            Contribution.objects.filter(
                orcid_id=self.orcid_placeholder, user__isnull=True
            ).update(user=self.user)
