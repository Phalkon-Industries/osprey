from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, override_settings
from django.urls import reverse

from projects.models import Project

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - dependency is installed in Docker
    PlaywrightError = Exception
    sync_playwright = None


class CoreViewTests(TestCase):
    def test_static_pages_render(self):
        for url_name in ["home", "about", "license_guide"]:
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 200, url_name)

    def test_login_redirects_authenticated_user_home(self):
        User = get_user_model()
        user = User.objects.create_user(username="alice")
        self.client.force_login(user)

        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("home"))

    def test_roadmap_page_is_retired(self):
        # The roadmap moved to the README; the old page must 404, not crash.
        response = self.client.get("/roadmap/")
        self.assertEqual(response.status_code, 404)


class SandboxBannerTests(TestCase):
    def test_banner_hidden_when_not_sandbox(self):
        with override_settings(OSPREY_IS_SANDBOX=False):
            response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "sandbox-banner")

    def test_banner_shown_when_sandbox(self):
        with override_settings(OSPREY_IS_SANDBOX=True):
            response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "sandbox-banner")
        self.assertContains(response, "OSPREY sandbox")


@override_settings(ALLOWED_HOSTS=["localhost", "127.0.0.1", "testserver"])
class BrowserSmokeTests(StaticLiveServerTestCase):
    def setUp(self):
        User = get_user_model()
        user = User.objects.create_user(username="browser-user")
        Project.objects.create(
            slug="browser-pump",
            title="Browser Pump",
            summary="A browser-visible pump.",
            field="Oceanography",
            artifact_type="Hardware",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=user,
        )

    def test_public_pages_render_in_real_browser(self):
        if sync_playwright is None:
            self.skipTest("Playwright is not installed")

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch()
            except PlaywrightError as exc:
                self.skipTest(f"Playwright Chromium is unavailable: {exc}")
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            try:
                for path, expected_text in [
                    ("/", "OSPREY"),
                    ("/projects/", "Browser Pump"),
                    ("/api/v1/projects/", "browser-pump"),
                ]:
                    response = page.goto(f"{self.live_server_url}{path}")
                    self.assertIsNotNone(response, path)
                    self.assertEqual(response.status, 200, path)
                    self.assertIn(expected_text, page.content())
            finally:
                browser.close()


class SettingsHubTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="settled")
        from people.models import Profile

        Profile.objects.filter(user=self.user).update(usertag_locked=True)

    def test_settings_url_lands_on_profile_tab(self):
        self.client.force_login(self.user)
        response = self.client.get("/settings/", follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Settings sections")
        self.assertContains(response, 'aria-current="page"')

    def test_settings_requires_login(self):
        response = self.client.get("/settings/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_both_settings_pages_share_the_tab_strip(self):
        self.client.force_login(self.user)
        profile = self.client.get(reverse("people:edit")).content.decode()
        notifications = self.client.get(
            reverse("notifications:settings")
        ).content.decode()
        for body in (profile, notifications):
            self.assertIn("Settings sections", body)
            self.assertIn(">Profile</a>", body)
            self.assertIn(">Notifications</a>", body)

    def test_header_links_settings_for_signed_in_users(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get("/"), 'href="/settings/"')
        self.client.logout()
        self.assertNotContains(self.client.get("/"), 'href="/settings/"')
