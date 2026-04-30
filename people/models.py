from django.conf import settings
from django.db import models


class Institution(models.Model):
    name = models.CharField(max_length=200, unique=True)
    short_name = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.short_name or self.name


class Profile(models.Model):
    """Per-user metadata layered on top of Django's built-in auth.User."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    display_name = models.CharField(max_length=200, blank=True)
    orcid_placeholder = models.CharField(
        max_length=40,
        blank=True,
        help_text="String form of an ORCID iD; becomes the verified iD once OAuth lands.",
    )
    institution = models.ForeignKey(
        Institution,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="members",
    )

    def __str__(self) -> str:
        return self.display_name or self.user.get_username()
