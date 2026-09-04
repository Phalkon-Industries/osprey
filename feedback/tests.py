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

    def test_widget_submission_defaults_to_suggestion_category(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("feedback:submit"), {"message": "Add dark mode everywhere"}
        )

        self.assertEqual(response.status_code, 200)
        feedback = Feedback.objects.get()
        self.assertEqual(feedback.category, Feedback.CATEGORY_SUGGESTION)

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

    def test_review_defaults_to_open_tab_and_archived_tab_shows_the_rest(self):
        open_item = Feedback.objects.create(user=self.user, message="Still open")
        archived_item = Feedback.objects.create(
            user=self.user, message="Handled", status=Feedback.STATUS_RESOLVED
        )

        self.client.force_login(self.staff)
        response = self.client.get(reverse("feedback:review"))
        items = list(response.context["feedback_items"])
        self.assertEqual([i.pk for i in items], [open_item.pk])
        self.assertEqual(response.context["open_count"], 1)
        self.assertEqual(response.context["archived_count"], 1)

        response = self.client.get(reverse("feedback:review"), {"tab": "archived"})
        items = list(response.context["feedback_items"])
        self.assertEqual([i.pk for i in items], [archived_item.pk])

        response = self.client.get(reverse("feedback:review"), {"tab": "all"})
        self.assertEqual(len(response.context["feedback_items"]), 2)

    def test_archive_button_marks_resolved(self):
        feedback = Feedback.objects.create(user=self.user, message="Done deal")

        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("feedback:review"),
            {"feedback_id": str(feedback.pk), "action": "archive"},
        )

        feedback.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(feedback.status, Feedback.STATUS_RESOLVED)

    def test_review_filters_by_category(self):
        Feedback.objects.create(user=self.user, message="An idea")
        Feedback.objects.create(
            user=self.user,
            message="Please delete my data",
            category=Feedback.CATEGORY_PRIVACY,
        )

        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("feedback:review"), {"category": "privacy"}
        )

        self.assertEqual(response.status_code, 200)
        items = list(response.context["feedback_items"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].category, Feedback.CATEGORY_PRIVACY)


class PrivacyRequestTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="alice")
        self.staff = User.objects.create_user(username="staff", is_staff=True)

    def test_form_requires_login(self):
        response = self.client.get(reverse("privacy_request"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_form_renders_request_types(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("privacy_request"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delete my account and personal data")

    def test_submission_stores_privacy_category_and_notifies_staff(self):
        from notifications.models import Notification

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("privacy_request"),
            {"request_type": "delete_account", "message": "Everything, please."},
        )

        self.assertEqual(response.status_code, 302)
        feedback = Feedback.objects.get()
        self.assertEqual(feedback.category, Feedback.CATEGORY_PRIVACY)
        self.assertIn("Delete my account and personal data", feedback.message)
        self.assertIn("Everything, please.", feedback.message)
        self.assertEqual(feedback.page_url, "")
        self.assertEqual(feedback.user_agent, "")
        note = Notification.objects.get(user=self.staff)
        self.assertIn("privacy request", note.title)

    def test_empty_submission_is_rejected(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("privacy_request"), {"message": ""})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Feedback.objects.exists())

    def test_type_only_submission_is_accepted(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("privacy_request"), {"request_type": "delete_account"}
        )
        self.assertEqual(response.status_code, 302)
        feedback = Feedback.objects.get()
        self.assertEqual(feedback.category, Feedback.CATEGORY_PRIVACY)

    def test_privacy_policy_links_to_form(self):
        response = self.client.get(reverse("privacy"))
        self.assertContains(response, reverse("privacy_request"))


class StaffThreadInteractionTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="alice")
        self.other = User.objects.create_user(username="mallory")
        self.staff = User.objects.create_user(username="staff", is_staff=True)

    def _notifications(self, **filters):
        from notifications.models import Notification

        return Notification.objects.filter(**filters)

    def test_user_reply_on_open_thread_notifies_staff(self):
        fb = Feedback.objects.create(user=self.user, message="An idea")

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("feedback:user_reply", args=[fb.pk]), {"body": "More detail"}
        )

        self.assertEqual(response.status_code, 302)
        reply = fb.replies.get()
        self.assertEqual(reply.author, self.user)
        self.assertEqual(reply.body, "More detail")
        self.assertTrue(
            self._notifications(
                user=self.staff, kind="feedback_user_replied"
            ).exists()
        )

    def test_user_cannot_reply_on_someone_elses_thread(self):
        fb = Feedback.objects.create(user=self.user, message="Mine")

        self.client.force_login(self.other)
        response = self.client.post(
            reverse("feedback:user_reply", args=[fb.pk]), {"body": "Sneaky"}
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(fb.replies.exists())

    def test_reply_blocked_on_closed_thread(self):
        fb = Feedback.objects.create(
            user=self.user, message="Done", status=Feedback.STATUS_RESOLVED
        )

        self.client.force_login(self.user)
        self.client.post(
            reverse("feedback:user_reply", args=[fb.pk]), {"body": "But wait"}
        )

        self.assertFalse(fb.replies.exists())

    def test_reopen_request_flow(self):
        fb = Feedback.objects.create(
            user=self.user, message="Old", status=Feedback.STATUS_RESOLVED
        )

        self.client.force_login(self.user)
        response = self.client.post(
            reverse("feedback:reopen_request", args=[fb.pk]),
            {"body": "It broke again"},
        )

        fb.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(fb.reopen_requested)
        self.assertIn("It broke again", fb.replies.get().body)
        self.assertTrue(
            self._notifications(
                user=self.staff, kind="feedback_reopen_requested"
            ).exists()
        )

        # Second request is refused while one is pending.
        self.client.post(reverse("feedback:reopen_request", args=[fb.pk]), {})
        self.assertEqual(fb.replies.count(), 1)

    def test_staff_dismiss_reopen_keeps_thread_closed(self):
        fb = Feedback.objects.create(
            user=self.user,
            message="Old",
            status=Feedback.STATUS_RESOLVED,
            reopen_requested=True,
        )

        self.client.force_login(self.staff)
        self.client.post(
            reverse("feedback:review"),
            {"feedback_id": str(fb.pk), "action": "dismiss_reopen"},
        )

        fb.refresh_from_db()
        self.assertFalse(fb.reopen_requested)
        self.assertEqual(fb.status, Feedback.STATUS_RESOLVED)

    def test_reopening_clears_the_request_flag(self):
        fb = Feedback.objects.create(
            user=self.user,
            message="Old",
            status=Feedback.STATUS_RESOLVED,
            reopen_requested=True,
        )

        self.client.force_login(self.staff)
        self.client.post(
            reverse("feedback:review"),
            {
                "feedback_id": str(fb.pk),
                "status": Feedback.STATUS_TRIAGED,
                "admin_notes": "",
            },
        )

        fb.refresh_from_db()
        self.assertEqual(fb.status, Feedback.STATUS_TRIAGED)
        self.assertFalse(fb.reopen_requested)

    def test_compose_is_staff_only_and_notifies_recipient(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("feedback:compose"))
        self.assertEqual(response.status_code, 302)

        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("feedback:compose"),
            {"username": "alice", "message": "About your dispute"},
        )

        self.assertEqual(response.status_code, 302)
        fb = Feedback.objects.get()
        self.assertEqual(fb.category, Feedback.CATEGORY_OUTREACH)
        self.assertEqual(fb.user, self.user)
        self.assertEqual(fb.opened_by, self.staff)
        self.assertEqual(fb.status, Feedback.STATUS_TRIAGED)
        note = self._notifications(
            user=self.user, kind="staff_message_received"
        ).get()
        self.assertIn("staff sent you a message", note.title)

    def test_compose_rejects_unknown_user(self):
        self.client.force_login(self.staff)
        self.client.post(
            reverse("feedback:compose"),
            {"username": "nobody", "message": "Hello?"},
        )
        self.assertFalse(Feedback.objects.exists())

    def test_mine_page_shows_reply_or_reopen_controls(self):
        open_fb = Feedback.objects.create(user=self.user, message="Open one")
        closed_fb = Feedback.objects.create(
            user=self.user, message="Closed one", status=Feedback.STATUS_WONTFIX
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("feedback:mine"))

        self.assertContains(
            response, reverse("feedback:user_reply", args=[open_fb.pk])
        )
        self.assertContains(
            response, reverse("feedback:reopen_request", args=[closed_fb.pk])
        )
        self.assertContains(response, ">closed<")
