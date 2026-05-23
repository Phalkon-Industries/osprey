from __future__ import annotations

from allauth.socialaccount.models import SocialAccount
from allauth.socialaccount.models import SocialLogin
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from projects.models import Contribution, Project

from .adapters import ClosedBetaAccountAdapter, OrcidSocialAccountAdapter
from .forms import ProfileForm
from .identity import clean_user_tag, extract_orcid, extract_orcid_names, generate_user_tag, user_tag_base
from .models import Profile
from .signals import _sync_orcid_profile


def orcid_extra(orcid_id: str = "0000-0001-2345-6789") -> dict:
    return {
        "orcid-identifier": {"path": orcid_id},
        "person": {
            "name": {
                "given-names": {"value": "Alice"},
                "family-name": {"value": "Researcher"},
                "credit-name": {"value": "Alice R."},
            }
        },
    }


class IdentityHelperTests(TestCase):
    def test_orcid_and_name_extraction(self):
        extra_data = orcid_extra()

        self.assertEqual(extract_orcid(extra_data), "0000-0001-2345-6789")
        self.assertEqual(
            extract_orcid_names(extra_data),
            {"display_name": "Alice R.", "first_name": "Alice", "last_name": "Researcher"},
        )

    def test_user_tag_helpers_clean_and_generate_conflicts(self):
        User = get_user_model()
        User.objects.create_user(username="alicepfeifer")

        self.assertEqual(clean_user_tag(" @Alice_Pfeifer "), "alice_pfeifer")
        self.assertEqual(user_tag_base("Alice Pfeifer, PhD"), "alicepfeiferphd")
        self.assertEqual(
            generate_user_tag("Alice Pfeifer", orcid_id="0000-0001-0002-9999"),
            "alicepfeifer9999",
        )


class ProfileSignalTests(TestCase):
    def test_user_creation_creates_profile(self):
        User = get_user_model()
        user = User.objects.create_user(username="alice")

        self.assertTrue(Profile.objects.filter(user=user).exists())

    def test_orcid_sync_updates_profile_names_and_claims_matching_contribution(self):
        User = get_user_model()
        user = User.objects.create_user(username="temporary")
        SocialAccount.objects.create(
            user=user,
            provider="orcid",
            uid="0000-0001-2345-6789",
            extra_data=orcid_extra(),
        )
        project = Project.objects.create(slug="pump", title="Pump", visibility=Project.VISIBILITY_PUBLIC)
        contribution = Contribution.objects.create(
            project=project,
            orcid_id="0000-0001-2345-6789",
            display_name="Alice R.",
            role="Project lead",
        )

        _sync_orcid_profile(user)
        contribution.refresh_from_db()
        user.refresh_from_db()
        user.profile.refresh_from_db()

        self.assertEqual(user.profile.orcid_placeholder, "0000-0001-2345-6789")
        self.assertEqual(user.profile.display_name, "Alice R.")
        self.assertEqual(user.first_name, "Alice")
        self.assertEqual(contribution.user, user)


class AccountAdapterTests(TestCase):
    def test_closed_beta_adapter_rejects_password_signup(self):
        adapter = ClosedBetaAccountAdapter()

        self.assertFalse(adapter.is_open_for_signup(request=None))

    def test_orcid_adapter_allows_only_orcid_social_signup_and_populates_user(self):
        User = get_user_model()
        adapter = OrcidSocialAccountAdapter()
        sociallogin = SocialLogin(user=User(), account=SocialAccount(provider="orcid", extra_data=orcid_extra()))

        self.assertTrue(adapter.is_open_for_signup(None, sociallogin))
        sociallogin.account.provider = "github"
        self.assertFalse(adapter.is_open_for_signup(None, sociallogin))

        sociallogin.account.provider = "orcid"
        user = adapter.populate_user(None, sociallogin, {})
        self.assertEqual(user.first_name, "Alice")
        self.assertEqual(user.last_name, "Researcher")
        self.assertEqual(user.username, "alicer")

    def test_orcid_adapter_reactivates_existing_user(self):
        User = get_user_model()
        user = User.objects.create_user(username="inactive", is_active=False)
        sociallogin = SocialLogin(account=SocialAccount(provider="orcid"), user=user)
        sociallogin.state["process"] = "connect"

        OrcidSocialAccountAdapter().pre_social_login(None, sociallogin)

        user.refresh_from_db()
        self.assertTrue(user.is_active)


class ProfileFormTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="alice")
        self.profile = self.user.profile

    def test_usertag_only_appears_during_unlocked_onboarding(self):
        onboarding_form = ProfileForm(instance=self.profile, allow_usertag=True)
        edit_form = ProfileForm(instance=self.profile)

        self.assertIn("usertag", onboarding_form.fields)
        self.assertNotIn("usertag", edit_form.fields)

    def test_onboarding_save_normalizes_and_locks_usertag(self):
        form = ProfileForm(
            data={
                "usertag": "@Alice_Researcher",
                "first_name": "Alice",
                "last_name": "Researcher",
                "display_name": "Alice R.",
                "bio": "Works on pumps.",
                "institution": "WHOI",
            },
            instance=self.profile,
            allow_usertag=True,
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        form.save()
        self.user.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertEqual(self.user.username, "alice_researcher")
        self.assertTrue(self.profile.usertag_locked)

    def test_duplicate_usertag_is_rejected_case_insensitively(self):
        User = get_user_model()
        User.objects.create_user(username="taken")
        form = ProfileForm(
            data={"usertag": "TAKEN", "first_name": "", "last_name": "", "display_name": "", "bio": "", "institution": ""},
            instance=self.profile,
            allow_usertag=True,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("usertag", form.errors)


class PeopleViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="alice")
        self.other = User.objects.create_user(username="bob")

    def test_profile_edit_redirects_until_onboarding_is_complete(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("people:edit"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("people:onboarding"))

    def test_onboarding_post_locks_profile_and_redirects_to_detail(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("people:onboarding"),
            {
                "usertag": "alice_test",
                "first_name": "Alice",
                "last_name": "Researcher",
                "display_name": "Alice Researcher",
                "bio": "",
                "institution": "WHOI",
            },
        )

        self.user.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("people:detail", args=[self.user.pk]))
        self.assertTrue(self.user.profile.usertag_locked)

    def test_locked_onboarding_redirects_to_edit_and_edit_post_updates_profile(self):
        self.user.profile.usertag_locked = True
        self.user.profile.save()
        self.client.force_login(self.user)

        response = self.client.get(reverse("people:onboarding"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("people:edit"))

        response = self.client.post(
            reverse("people:edit"),
            {
                "first_name": "Alice",
                "last_name": "Researcher",
                "display_name": "Alice R.",
                "bio": "Pump notes.",
                "institution": "WHOI",
            },
        )
        self.user.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.user.first_name, "Alice")
        self.assertEqual(self.user.profile.display_name, "Alice R.")

    def test_profile_pages_render_self_and_public_profile(self):
        Project.objects.create(
            slug="own-draft",
            title="Own Draft",
            visibility=Project.VISIBILITY_PRIVATE,
            created_by=self.user,
        )
        public_project = Project.objects.create(
            slug="public-credit",
            title="Public Credit",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.other,
        )
        Contribution.objects.create(
            project=public_project,
            user=self.other,
            display_name="Bob",
            role="Maintainer",
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("people:me"))
        self.assertEqual(response.status_code, 302)
        response = self.client.get(reverse("people:detail", args=[self.user.pk]))
        self.assertContains(response, "Own Draft")

        response = self.client.get(reverse("people:detail", args=[self.other.pk]))
        self.assertContains(response, "Public Credit")

    def test_deactivation_marks_user_inactive_and_keeps_profile(self):
        self.user.profile.usertag_locked = True
        self.user.profile.save()
        self.client.force_login(self.user)

        response = self.client.post(reverse("people:deactivate"))

        self.user.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.user.is_active)
        self.assertTrue(Profile.objects.filter(user=self.user).exists())

    def test_institution_page_matches_public_free_text_institutions(self):
        Project.objects.create(
            slug="public-whoi-pump",
            title="WHOI Pump",
            institution="WHOI",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.user,
        )
        Project.objects.create(
            slug="private-whoi-pump",
            title="Private WHOI Pump",
            institution="WHOI",
            visibility=Project.VISIBILITY_PRIVATE,
            created_by=self.user,
        )

        response = self.client.get(reverse("institutions:detail", args=["whoi"]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "WHOI Pump")
        self.assertNotContains(response, "Private WHOI Pump")


    def test_onboarding_post_with_taken_usertag_shows_error(self):
        """Submitting a usertag that another user already owns re-renders the
        onboarding form with an error and does not lock the tag."""
        # `other` is created in setUp with username "bob"; create another with
        # the tag we want to claim.
        User = get_user_model()
        User.objects.create_user(username="claimed_tag")

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("people:onboarding"),
            {
                "usertag": "claimed_tag",
                "first_name": "Alice",
                "last_name": "Researcher",
                "display_name": "Alice Researcher",
                "bio": "",
                "institution": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already taken")
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile.usertag_locked)
        self.assertNotEqual(self.user.username, "claimed_tag")

    def test_institution_page_matches_when_listed_with_others(self):
        """A project whose institution field lists multiple values still
        matches each one separately on its institution page."""
        Project.objects.create(
            slug="multi-inst-pump",
            title="Multi Inst Pump",
            institution="WHOI\nMIT",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.user,
        )

        whoi = self.client.get(reverse("institutions:detail", args=["whoi"]))
        mit = self.client.get(reverse("institutions:detail", args=["mit"]))
        self.assertEqual(whoi.status_code, 200)
        self.assertEqual(mit.status_code, 200)
        self.assertContains(whoi, "Multi Inst Pump")
        self.assertContains(mit, "Multi Inst Pump")
