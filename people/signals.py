from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from allauth.account.signals import user_signed_up
from allauth.socialaccount.models import SocialAccount
from allauth.socialaccount.signals import social_account_added, social_account_updated

from .models import Profile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_profile(sender, instance, created, **kwargs):
    """Auto-create a Profile row for every new user."""
    if created:
        Profile.objects.get_or_create(user=instance)


def _extract_orcid(extra_data: dict) -> str:
    identifier = extra_data.get("orcid-identifier") or {}
    return (identifier.get("path") or "").strip()


def _extract_display_name(extra_data: dict) -> str:
    person = extra_data.get("person") or {}
    name = person.get("name") or {}
    given = ((name.get("given-names") or {}).get("value") or "").strip()
    family = ((name.get("family-name") or {}).get("value") or "").strip()
    return " ".join(part for part in [given, family] if part)


def _sync_orcid_profile(user) -> None:
    account = SocialAccount.objects.filter(user=user, provider="orcid").first()
    if account is None:
        return
    orcid_id = _extract_orcid(account.extra_data or {})
    if not orcid_id:
        return
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.orcid_placeholder = orcid_id
    if not profile.display_name:
        profile.display_name = _extract_display_name(account.extra_data or {}) or user.get_username()
    profile.save()


@receiver(user_signed_up)
def sync_orcid_after_signup(request, user, **kwargs):
    _sync_orcid_profile(user)


@receiver(social_account_added)
@receiver(social_account_updated)
def sync_orcid_after_social_account_change(request, sociallogin, **kwargs):
    if sociallogin.account.provider == "orcid":
        _sync_orcid_profile(sociallogin.user)
