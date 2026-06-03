from __future__ import annotations

from unittest.mock import patch

from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, override_settings

from people.adapters import OrcidSocialAccountAdapter
from people.models import Profile
from people.orcid_verification import OrcidLookupError, verified_domains

User = get_user_model()

ORCID = "0000-0001-2345-6789"


def _extra(orcid_id=ORCID):
    return {"orcid-identifier": {"path": orcid_id}, "person": {}}


def _sociallogin(user, *, is_existing=True):
    if is_existing:
        sa = SocialAccount.objects.get(user=user, provider="orcid")
    else:
        sa = SocialAccount(user=None, provider="orcid", uid=ORCID, extra_data=_extra())
    sl = SocialLogin(user=user, account=sa)
    return sl


class VerifiedDomainParserTests(TestCase):
    def test_picks_third_party_verified(self):
        payload = {
            "path": ORCID,
            "email": [
                {
                    "verified": True,
                    "email": "alice@whoi.edu",
                    "source": {"source-orcid": {"path": "0000-0000-0000-0001"}},
                },
                {
                    "verified": True,
                    "email": "alice@gmail.com",
                    "source": {"source-orcid": {"path": ORCID}},
                },
                {"verified": False, "email": "alice@old.edu"},
            ],
        }
        self.assertEqual(verified_domains(payload), ["whoi.edu"])

    def test_empty_payload(self):
        self.assertEqual(verified_domains({}), [])
        self.assertEqual(verified_domains({"email": []}), [])


@override_settings(ORCID_REQUIRE_VERIFIED_DOMAIN=True, ORCID_SIGNIN_ALLOWLIST=[])
class OrcidGateTests(TestCase):
    def setUp(self):
        self.adapter = OrcidSocialAccountAdapter()
        self.rf = RequestFactory()
        self.user = User.objects.create_user(username="alice")
        SocialAccount.objects.create(
            user=self.user, provider="orcid", uid=ORCID, extra_data=_extra()
        )

    def _request(self):
        request = self.rf.get("/accounts/orcid/login/callback/")
        # Allauth's adapter uses messages framework via the request.
        from django.contrib.messages.storage.fallback import FallbackStorage

        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))
        return request

    @patch("people.adapters.has_verified_institutional_domain")
    def test_allows_user_with_verified_domain(self, mock_lookup):
        mock_lookup.return_value = (True, ["whoi.edu"])
        request = self._request()
        sl = _sociallogin(self.user)
        # Should not raise.
        self.adapter.pre_social_login(request, sl)

    @patch("people.adapters.has_verified_institutional_domain")
    def test_rejects_user_without_verified_domain(self, mock_lookup):
        mock_lookup.return_value = (False, [])
        request = self._request()
        sl = _sociallogin(self.user)
        with self.assertRaises(ImmediateHttpResponse):
            self.adapter.pre_social_login(request, sl)

    @patch("people.adapters.has_verified_institutional_domain")
    def test_fails_closed_on_lookup_error(self, mock_lookup):
        mock_lookup.side_effect = OrcidLookupError("boom")
        request = self._request()
        sl = _sociallogin(self.user)
        with self.assertRaises(ImmediateHttpResponse):
            self.adapter.pre_social_login(request, sl)

    @override_settings(ORCID_SIGNIN_ALLOWLIST=[ORCID])
    @patch("people.adapters.has_verified_institutional_domain")
    def test_allowlist_bypasses_lookup(self, mock_lookup):
        request = self._request()
        sl = _sociallogin(self.user)
        self.adapter.pre_social_login(request, sl)
        mock_lookup.assert_not_called()


class SuspendedAccountTests(TestCase):
    def setUp(self):
        self.adapter = OrcidSocialAccountAdapter()
        self.rf = RequestFactory()
        self.user = User.objects.create_user(username="bob", is_active=False)
        from django.utils import timezone

        profile, _ = Profile.objects.get_or_create(user=self.user)
        profile.suspended_at = timezone.now()
        profile.save(update_fields=["suspended_at"])
        SocialAccount.objects.create(
            user=self.user, provider="orcid", uid=ORCID, extra_data=_extra()
        )

    def _request(self):
        request = self.rf.get("/")
        from django.contrib.messages.storage.fallback import FallbackStorage

        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))
        return request

    def test_suspended_user_cannot_reactivate_on_login(self):
        sl = _sociallogin(self.user)
        with self.assertRaises(ImmediateHttpResponse):
            self.adapter.pre_social_login(self._request(), sl)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
