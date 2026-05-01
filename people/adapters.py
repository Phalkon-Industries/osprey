from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


class ClosedBetaAccountAdapter(DefaultAccountAdapter):
    """Keep public username/password signup closed."""

    def is_open_for_signup(self, request):
        return False


class OrcidSocialAccountAdapter(DefaultSocialAccountAdapter):
    """Allow ORCID social signup for beta onboarding."""

    def is_open_for_signup(self, request, sociallogin):
        return sociallogin.account.provider == "orcid"