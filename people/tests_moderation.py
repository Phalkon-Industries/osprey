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


class OrcidGateTests(TestCase):
    """The gate on for every sign-in (the strict scope)."""

    def setUp(self):
        self.adapter = OrcidSocialAccountAdapter()
        self.rf = RequestFactory()
        self.user = User.objects.create_user(username="alice")
        SocialAccount.objects.create(
            user=self.user, provider="orcid", uid=ORCID, extra_data=_extra()
        )
        from people.models import SignupGate

        gate = SignupGate.load()
        gate.enabled = True
        gate.scope = SignupGate.SCOPE_ALL
        gate.save()

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

    @patch("people.adapters.has_verified_institutional_domain")
    def test_allowlist_bypasses_lookup(self, mock_lookup):
        from people.models import SignupGate

        gate = SignupGate.load()
        gate.allowlist = ORCID
        gate.save()
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


class SignupGateSwitchTests(TestCase):
    """The staff switch: on/off without a restart, new accounts only by default."""

    def setUp(self):
        from people.models import SignupGate

        self.user = get_user_model().objects.create_user(username="existing")
        SocialAccount.objects.create(user=self.user, provider="orcid", uid=ORCID, extra_data=_extra())
        self.adapter = OrcidSocialAccountAdapter()
        self.gate = SignupGate.load()

    def _request(self):
        from django.contrib.messages.storage.fallback import FallbackStorage

        request = RequestFactory().get("/accounts/orcid/login/callback/")
        setattr(request, "session", {})
        setattr(request, "_messages", FallbackStorage(request))
        return request

    @patch("people.adapters.has_verified_institutional_domain")
    def test_off_means_no_lookup(self, mock_lookup):
        newcomer = get_user_model()(username="newcomer")
        self.adapter.pre_social_login(self._request(), _sociallogin(newcomer, is_existing=False))
        mock_lookup.assert_not_called()

    @patch("people.adapters.has_verified_institutional_domain")
    def test_on_for_new_accounts_lets_existing_users_in(self, mock_lookup):
        mock_lookup.return_value = (False, [])
        self.gate.enabled = True
        self.gate.save()
        self.adapter.pre_social_login(self._request(), _sociallogin(self.user, is_existing=True))
        mock_lookup.assert_not_called()
        # allauth calls a login "existing" when its user row exists, so a
        # first-time sign-in carries an unsaved user.
        newcomer = get_user_model()(username="newcomer")
        with self.assertRaises(ImmediateHttpResponse):
            self.adapter.pre_social_login(self._request(), _sociallogin(newcomer, is_existing=False))

    @patch("people.adapters.has_verified_institutional_domain")
    def test_scope_all_checks_existing_users_too(self, mock_lookup):
        from people.models import SignupGate

        mock_lookup.return_value = (False, [])
        self.gate.enabled = True
        self.gate.scope = SignupGate.SCOPE_ALL
        self.gate.save()
        with self.assertRaises(ImmediateHttpResponse):
            self.adapter.pre_social_login(self._request(), _sociallogin(self.user, is_existing=True))

    @patch("people.adapters.has_verified_institutional_domain")
    def test_db_allowlist_bypasses(self, mock_lookup):
        from people.models import SignupGate

        self.gate.enabled = True
        self.gate.scope = SignupGate.SCOPE_ALL
        self.gate.allowlist = f"  {ORCID}  \n0000-0002-0000-0000"
        self.gate.save()
        self.adapter.pre_social_login(self._request(), _sociallogin(self.user, is_existing=True))
        mock_lookup.assert_not_called()

    def test_staff_page_toggles_the_gate(self):
        from django.urls import reverse

        from people.models import SignupGate

        staff = get_user_model().objects.create_user(username="staff", is_staff=True)
        url = reverse("core:signup_gate")
        self.assertIn(self.client.get(url).status_code, (302, 403))
        self.client.force_login(self.user)
        self.assertIn(self.client.get(url).status_code, (302, 403))
        self.client.force_login(staff)
        page = self.client.get(url)
        self.assertContains(page, "Status:</strong> off")
        self.client.post(url, {"enabled": "1", "scope": "all", "allowlist": f"{ORCID}\n\n"})
        gate = SignupGate.load()
        self.assertTrue(gate.enabled)
        self.assertEqual(gate.scope, SignupGate.SCOPE_ALL)
        self.assertEqual(gate.allowlist, ORCID)
        self.assertEqual(gate.updated_by, staff)
        dashboard = self.client.get(reverse("core:staff_dashboard"))
        self.assertContains(dashboard, "Sign-up gate</a> <span class=\"badge private\">on</span>")
        self.client.post(url, {"scope": "new", "allowlist": ""})
        self.assertFalse(SignupGate.load().enabled)
