"""The notifier heartbeat: a run of failing iterations raises an alarm."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from notifications import loop_health
from notifications.models import LoopHeartbeat, Notification


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class LoopHealthTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(username="staff", is_staff=True, email="staff@example.org")
        self.reader = get_user_model().objects.create_user(username="reader")

    def test_ok_iterations_keep_the_heartbeat_fresh_and_quiet(self):
        loop_health.record_ok()
        hb = LoopHeartbeat.load()
        self.assertFalse(hb.is_stale)
        self.assertEqual(hb.consecutive_failures, 0)
        self.assertFalse(Notification.objects.filter(kind="notifier_failing").exists())

    @patch("notifications.loop_health.verified_address_for", create=True)
    def test_a_streak_of_failures_alerts_staff_once_by_inbox_and_email(self, _addr):
        with patch("notifications.emails.verified_address_for", return_value="staff@example.org"):
            for _ in range(loop_health.ALERT_AFTER - 1):
                loop_health.record_failure(RuntimeError("column projects_project.source does not exist"))
            self.assertFalse(Notification.objects.filter(kind="notifier_failing").exists())
            loop_health.record_failure(RuntimeError("column projects_project.source does not exist"))
            notices = Notification.objects.filter(kind="notifier_failing")
            self.assertEqual(notices.count(), 1)
            self.assertEqual(notices.get().user, self.staff)
            self.assertIn("5 iterations in a row", notices.get().title)
            self.assertIn("projects_project.source", notices.get().body)
            self.assertEqual(len(mail.outbox), 1)
            self.assertEqual(mail.outbox[0].to, ["staff@example.org"])
            self.assertIn("Notifier loop failing", mail.outbox[0].subject)
            # More failures in the same streak: no second alarm.
            for _ in range(10):
                loop_health.record_failure(RuntimeError("still broken"))
            self.assertEqual(Notification.objects.filter(kind="notifier_failing").count(), 1)
            self.assertEqual(len(mail.outbox), 1)
            hb = LoopHeartbeat.load()
            self.assertEqual(hb.consecutive_failures, 15)
            self.assertEqual(hb.last_error, "still broken")
            # Recovery: one inbox notice, no email, counters reset, a new streak can alarm again.
            loop_health.record_ok()
            self.assertEqual(Notification.objects.filter(kind="notifier_failing", title="Notifier loop recovered").count(), 1)
            self.assertEqual(len(mail.outbox), 1)
            self.assertEqual(LoopHeartbeat.load().consecutive_failures, 0)
            for _ in range(loop_health.ALERT_AFTER):
                loop_health.record_failure(RuntimeError("again"))
            self.assertEqual(Notification.objects.filter(kind="notifier_failing").exclude(title="Notifier loop recovered").count(), 2)

    def test_the_loop_itself_records_both_outcomes(self):
        with patch("notifications.management.commands.notifier_loop.call_command", side_effect=RuntimeError("boom")):
            call_command("notifier_loop", once=True, stderr=open("/dev/null", "w"))
        hb = LoopHeartbeat.load()
        self.assertEqual(hb.consecutive_failures, 1)
        self.assertEqual(hb.last_error, "boom")
        self.assertTrue(hb.is_stale)
        with patch("notifications.management.commands.notifier_loop.call_command"):
            call_command("notifier_loop", once=True)
        hb = LoopHeartbeat.load()
        self.assertEqual(hb.consecutive_failures, 0)
        self.assertFalse(hb.is_stale)

    def test_staff_pages_show_a_stale_loop(self):
        hb = LoopHeartbeat.load()
        hb.last_ok_at = timezone.now() - timedelta(minutes=20)
        hb.consecutive_failures = 7
        hb.last_error = "column projects_project.source does not exist"
        hb.save()
        self.client.force_login(self.staff)
        dashboard = self.client.get(reverse("core:staff_dashboard"))
        self.assertContains(dashboard, "stale")
        self.assertContains(dashboard, "7 failures in a row")
        queue = self.client.get(reverse("zenodo_jobs"))
        self.assertContains(queue, "(stale)")
        hb.last_ok_at = timezone.now()
        hb.consecutive_failures = 0
        hb.save()
        self.assertNotContains(self.client.get(reverse("core:staff_dashboard")), "stale")
