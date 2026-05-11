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
        for url_name in ["home", "about", "license_guide", "roadmap"]:
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, 200, url_name)

    def test_login_redirects_authenticated_user_home(self):
        User = get_user_model()
        user = User.objects.create_user(username="alice")
        self.client.force_login(user)

        response = self.client.get(reverse("login"))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], reverse("home"))

    def test_roadmap_renders_curated_markdown(self):
        response = self.client.get(reverse("roadmap"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "OSPREY roadmap")

    def test_old_roadmap_entry_url_is_gone(self):
        response = self.client.get("/roadmap/anything/")
        self.assertEqual(response.status_code, 404)


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
                    ("/roadmap/", "OSPREY roadmap"),
                    ("/api/v1/export/", "browser-pump"),
                ]:
                    response = page.goto(f"{self.live_server_url}{path}")
                    self.assertIsNotNone(response, path)
                    self.assertEqual(response.status, 200, path)
                    self.assertIn(expected_text, page.content())
            finally:
                browser.close()
