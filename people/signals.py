from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from allauth.account.signals import user_signed_up
from allauth.socialaccount.signals import social_account_added, social_account_updated
from allauth.socialaccount.models import SocialAccount

from .identity import extract_orcid, extract_orcid_names, generate_user_tag
from .models import Profile


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def ensure_profile(sender, instance, created, **kwargs):
    """Auto-create a Profile row for every new user."""
    if created:
        Profile.objects.get_or_create(user=instance)


def _sync_orcid_profile(user) -> None:
    account = SocialAccount.objects.filter(user=user, provider="orcid").first()
    if account is None:
        return
    extra_data = account.extra_data or {}
    orcid_id = extract_orcid(extra_data)
    if not orcid_id:
        return
    names = extract_orcid_names(extra_data)
    profile, _ = Profile.objects.get_or_create(user=user)
    previous_username = user.get_username()
    profile.orcid_placeholder = orcid_id
    if names["first_name"] and not user.first_name:
        user.first_name = names["first_name"][:150]
    if names["last_name"] and not user.last_name:
        user.last_name = names["last_name"][:150]
    if not profile.usertag_locked:
        user.username = generate_user_tag(
            names["display_name"] or user.get_full_name() or user.get_username(),
            orcid_id=orcid_id,
            user_id=user.pk,
        )
    user.save()
    if not profile.usertag_locked and names["display_name"]:
        profile.display_name = names["display_name"]
    elif not profile.display_name or profile.display_name in {previous_username, user.get_username()}:
        profile.display_name = names["display_name"] or user.get_full_name() or user.get_username()
    profile.save()


@receiver(user_signed_up)
def sync_orcid_after_signup(request, user, **kwargs):
    _sync_orcid_profile(user)


@receiver(social_account_added)
@receiver(social_account_updated)
def sync_orcid_after_social_account_change(request, sociallogin, **kwargs):
    if sociallogin.account.provider == "orcid":
        _sync_orcid_profile(sociallogin.user)
