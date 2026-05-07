from __future__ import annotations

import base64
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Feedback
from .views import _coarse_ua, _decode_data_url


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


class FeedbackHelperTests(TestCase):
    def test_coarse_user_agent_parsing(self):
        browser, os_name = _coarse_ua(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
        )

        self.assertEqual(browser, "Chrome")
        self.assertEqual(os_name, "Linux")

    def test_data_url_decode_accepts_png_and_ignores_invalid_values(self):
        data_url = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")

        self.assertEqual(_decode_data_url(data_url), PNG_BYTES)
        self.assertIsNone(_decode_data_url("not-a-data-url"))
        self.assertIsNone(_decode_data_url("data:image/png;base64,not valid"))


class FeedbackViewTests(TestCase):
    def setUp(self):
        self.media_dir = tempfile.mkdtemp(prefix="osprey-feedback-tests-")
        self.settings_override = override_settings(MEDIA_ROOT=self.media_dir)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(lambda: shutil.rmtree(self.media_dir, ignore_errors=True))
        User = get_user_model()
        self.user = User.objects.create_user(username="alice")
        self.staff = User.objects.create_user(username="staff", is_staff=True)

    def test_submit_requires_login_and_message(self):
        response = self.client.post(reverse("feedback:submit"), {"message": "Hello"})
        self.assertEqual(response.status_code, 302)

        self.client.force_login(self.user)
        response = self.client.post(reverse("feedback:submit"), {"message": ""})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Feedback.objects.exists())

    def test_submit_stores_feedback_with_screenshot_and_coarse_environment(self):
        self.client.force_login(self.user)
        screenshot = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
        long_message = "x" * 4100

        response = self.client.post(
            reverse("feedback:submit"),
            {
                "message": long_message,
                "page_url": "https://osprey.phalkon.io/projects/public-pump/",
                "page_title": "Public Pump",
                "viewport_w": "1280",
                "viewport_h": "800",
                "screenshot": screenshot,
            },
            HTTP_USER_AGENT="Mozilla/5.0 (X11; Linux x86_64) Chrome/120.0 Safari/537.36",
        )

        self.assertEqual(response.status_code, 200)
        feedback = Feedback.objects.get()
        self.assertEqual(len(feedback.message), 4000)
        self.assertEqual(feedback.browser, "Chrome")
        self.assertEqual(feedback.os, "Linux")
        self.assertEqual(feedback.viewport_w, 1280)
        self.assertTrue(feedback.screenshot.name.endswith(".png"))

    def test_review_is_staff_only_and_updates_status(self):
        feedback = Feedback.objects.create(user=self.user, message="Needs a look")

        self.client.force_login(self.user)
        response = self.client.get(reverse("feedback:review"))
        self.assertEqual(response.status_code, 302)

        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("feedback:review"),
            {"feedback_id": str(feedback.pk), "status": Feedback.STATUS_TRIAGED, "admin_notes": "Checked."},
        )

        feedback.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(feedback.status, Feedback.STATUS_TRIAGED)
        self.assertEqual(feedback.admin_notes, "Checked.")
