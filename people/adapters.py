from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponseRedirect
from django.urls import reverse

from .identity import extract_orcid, extract_orcid_names, generate_user_tag
from .orcid_verification import (
    OrcidLookupError,
    has_verified_institutional_domain,
)


class ClosedBetaAccountAdapter(DefaultAccountAdapter):
    """Keep password-based signup closed."""

    def is_open_for_signup(self, request):
        return False


def _reject(request, message: str):
    messages.error(request, message)
    raise ImmediateHttpResponse(HttpResponseRedirect(reverse("login")))


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
        user.username = generate_user_tag(
            display_name, orcid_id=extract_orcid(extra_data)
        )
        return user

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form=form)
        try:
            from projects.claiming import sweep_on_signup

            sweep_on_signup(user)
        except Exception:  # noqa: BLE001 - a sweep bug must not break signup
            import logging

            logging.getLogger(__name__).exception(
                "contributor claim sweep failed at signup"
            )
        return user

    def pre_social_login(self, request, sociallogin) -> None:
        if sociallogin.account.provider == "orcid":
            self._enforce_verified_domain_gate(request, sociallogin)
            if sociallogin.is_existing:
                user = sociallogin.user
                if user and not user.is_active:
                    # Read suspended_at directly to avoid any cached reverse-OneToOne.
                    from .models import Profile

                    suspended_at = (
                        Profile.objects.filter(user=user)
                        .values_list("suspended_at", flat=True)
                        .first()
                    )
                    if suspended_at:
                        _reject(
                            request,
                            "This account is suspended. Contact OSPREY staff if you believe this is a mistake.",
                        )
                    user.is_active = True
                    user.save(update_fields=["is_active"])
        super().pre_social_login(request, sociallogin)

    def _enforce_verified_domain_gate(self, request, sociallogin) -> None:
        from .models import SignupGate

        gate = SignupGate.load()
        if not gate.enabled:
            return
        if gate.scope == SignupGate.SCOPE_NEW and sociallogin.is_existing:
            return
        extra_data = sociallogin.account.extra_data or {}
        orcid_id = extract_orcid(extra_data) or sociallogin.account.uid or ""
        if not orcid_id:
            _reject(request, "Could not read your ORCID iD. Please try again.")
        if orcid_id in gate.allowlisted:
            return
        try:
            has_domain, domains = has_verified_institutional_domain(
                orcid_id, use_sandbox=getattr(settings, "ORCID_USE_SANDBOX", False)
            )
        except OrcidLookupError:
            _reject(
                request,
                "We couldn't verify your ORCID record right now. Please try again in a few minutes.",
            )
            return
        if not has_domain:
            _reject(
                request,
                "OSPREY sign-in currently requires an ORCID record with at least one "
                "verified institutional email domain set to public visibility. "
                "Add a verified institutional email at orcid.org and set its visibility to Everyone, "
                "then try again.",
            )
            return
        # Stash the domains on the sociallogin so the signal can persist them.
        sociallogin._osprey_verified_domains = domains
