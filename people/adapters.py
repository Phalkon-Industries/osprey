from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .identity import extract_orcid, extract_orcid_names, generate_user_tag


class ClosedBetaAccountAdapter(DefaultAccountAdapter):
    """Keep password-based signup closed."""

    def is_open_for_signup(self, request):
        return False


class OrcidSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Allow ORCID social signup for beta onboarding."""

    def is_open_for_signup(self, request, sociallogin):
        return sociallogin.account.provider == "orcid"

    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        if sociallogin.account.provider != "orcid":
            return user
        extra_data = sociallogin.account.extra_data or {}
        names = extract_orcid_names(extra_data)
        display_name = names["display_name"] or data.get("name") or "OSPREY user"
        user.first_name = names["first_name"][:150]
        user.last_name = names["last_name"][:150]
        user.username = generate_user_tag(display_name, orcid_id=extract_orcid(extra_data))
        return user

    def pre_social_login(self, request, sociallogin) -> None:
        if sociallogin.account.provider == "orcid" and sociallogin.is_existing:
            user = sociallogin.user
            if user and not user.is_active:
                user.is_active = True
                user.save(update_fields=["is_active"])
        super().pre_social_login(request, sociallogin)