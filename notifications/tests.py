import os
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.mail import EmailMessage
from django.core.mail.backends import locmem
from django.core.mail.backends.console import EmailBackend as ConsoleBackend
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from anymail.backends.mailjet import EmailBackend as MailjetBackend
from anymail.exceptions import AnymailAPIError

from .email import OspreyEmailBackend
from .models import EmailSettings, Notification, send


class NotificationSendTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="alice")

    def test_send_creates_row(self):
        n = send(
            self.user,
            kind=Notification.KIND_GENERIC,
            title="Hi",
            body="There",
            url="/x/",
        )
        self.assertIsNotNone(n)
        self.assertEqual(n.user, self.user)
        self.assertEqual(n.title, "Hi")
        self.assertFalse(n.is_read)

    def test_send_to_none_user_is_noop(self):
        self.assertIsNone(send(None, kind="generic", title="x"))

    def test_send_to_inactive_user_is_noop(self):
        self.user.is_active = False
        self.user.save()
        self.assertIsNone(send(self.user, kind="generic", title="x"))


class NotificationViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="alice")
        self.other = get_user_model().objects.create_user(username="bob")

    def test_inbox_requires_login(self):
        response = self.client.get(reverse("notifications:inbox"))
        self.assertEqual(response.status_code, 302)

    def test_inbox_lists_own_notifications(self):
        send(self.user, kind="generic", title="Mine")
        send(self.other, kind="generic", title="Not mine")
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications:inbox"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mine")
        self.assertNotContains(response, "Not mine")

    def test_open_marks_read_and_redirects(self):
        n = send(self.user, kind="generic", title="t", url="/somewhere/")
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications:open", args=[n.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/somewhere/")
        n.refresh_from_db()
        self.assertTrue(n.is_read)

    def test_open_someone_elses_notification_is_404(self):
        n = send(self.other, kind="generic", title="t")
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications:open", args=[n.pk]))
        self.assertEqual(response.status_code, 404)

    def test_mark_all_read(self):
        send(self.user, kind="generic", title="a")
        send(self.user, kind="generic", title="b")
        self.client.force_login(self.user)
        response = self.client.post(reverse("notifications:mark_all_read"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.user.notifications.filter(is_read=False).count(), 0)


class NotificationBadgeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="alice")

    def test_badge_shows_unread_count(self):
        send(self.user, kind="generic", title="One")
        send(self.user, kind="generic", title="Two")
        self.client.force_login(self.user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-bell")
        self.assertContains(response, ">2<")


class EmailTransportTests(TestCase):
    """Phase-1 transport: the EmailSettings singleton and the backend."""

    def test_load_creates_singleton_seeded_from_env(self):
        with patch.dict(
            os.environ, {"DEFAULT_FROM_EMAIL": "OSPREY <seed@example.org>"}
        ):
            config = EmailSettings.load()
        self.assertEqual(config.pk, 1)
        self.assertEqual(config.from_email, "OSPREY <seed@example.org>")
        self.assertFalse(config.enabled)
        # A second load returns the same row, not a new one.
        self.assertEqual(EmailSettings.load().pk, 1)
        self.assertEqual(EmailSettings.objects.count(), 1)

    def test_save_enforces_singleton_pk(self):
        EmailSettings.load()
        clone = EmailSettings(enabled=True)
        clone.save()
        self.assertEqual(EmailSettings.objects.count(), 1)
        self.assertTrue(EmailSettings.load().enabled)

    def test_backend_falls_back_to_console_when_disabled(self):
        EmailSettings.load()
        backend = OspreyEmailBackend()
        self.assertIsInstance(backend.delegate, ConsoleBackend)

    @override_settings(
        ANYMAIL={"MAILJET_API_KEY": "env-key", "MAILJET_SECRET_KEY": "env-secret"}
    )
    def test_backend_uses_env_credentials_when_enabled(self):
        config = EmailSettings.load()
        config.enabled = True
        config.save()
        backend = OspreyEmailBackend()
        self.assertIsInstance(backend.delegate, MailjetBackend)
        self.assertEqual(backend.delegate.api_key, "env-key")
        self.assertEqual(backend.delegate.secret_key, "env-secret")

    @override_settings(ANYMAIL={})
    def test_backend_falls_back_to_console_when_provider_unconfigured(self):
        config = EmailSettings.load()
        config.enabled = True
        config.save()
        backend = OspreyEmailBackend()
        self.assertIsInstance(backend.delegate, ConsoleBackend)

    @override_settings(
        EMAIL_DELIVERY_BACKEND="django.core.mail.backends.locmem.EmailBackend"
    )
    def test_backend_is_provider_agnostic(self):
        # Any Django email backend path works as the delivery provider;
        # nothing assumes a specific vendor.
        config = EmailSettings.load()
        config.enabled = True
        config.save()
        backend = OspreyEmailBackend()
        self.assertIsInstance(backend.delegate, locmem.EmailBackend)

    def test_backend_rewrites_default_from_address(self):
        config = EmailSettings.load()
        config.from_email = "OSPREY <panel@example.org>"
        config.save()
        backend = OspreyEmailBackend()
        backend.delegate = locmem.EmailBackend()
        message = EmailMessage(
            subject="s", body="b", to=["someone@example.org"]
        )  # from_email defaults to DEFAULT_FROM_EMAIL
        backend.send_messages([message])
        self.assertEqual(message.from_email, "OSPREY <panel@example.org>")

    def test_backend_keeps_explicit_from_address(self):
        config = EmailSettings.load()
        config.from_email = "OSPREY <panel@example.org>"
        config.save()
        backend = OspreyEmailBackend()
        backend.delegate = locmem.EmailBackend()
        message = EmailMessage(
            subject="s",
            body="b",
            from_email="Other <other@example.org>",
            to=["someone@example.org"],
        )
        backend.send_messages([message])
        self.assertEqual(message.from_email, "Other <other@example.org>")


class EmailAdminAndCommandTests(TestCase):
    """The admin test-send action and its CLI twin."""

    def setUp(self):
        self.staff = get_user_model().objects.create_superuser(
            username="root", password="x"
        )

    def _change_form_data(self, config, **overrides):
        data = {
            "enabled": "",
            "from_email": config.from_email,
            "test_recipient": config.test_recipient,
        }
        data.update(overrides)
        return data

    def test_admin_changelist_redirects_to_singleton_form(self):
        self.client.force_login(self.staff)
        response = self.client.get(
            reverse("admin:notifications_emailsettings_changelist")
        )
        config = EmailSettings.load()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse("admin:notifications_emailsettings_change", args=[config.pk]),
        )

    def test_admin_save_and_send_test_button(self):
        config = EmailSettings.load()
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("admin:notifications_emailsettings_change", args=[config.pk]),
            self._change_form_data(
                config, test_recipient="me@example.org", _send_test="1"
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["me@example.org"])
        config.refresh_from_db()
        self.assertIn("sent to me@example.org", config.last_test_result)
        self.assertIsNotNone(config.last_test_at)

    @override_settings(EMAIL_BACKEND="notifications.email.OspreyEmailBackend")
    def test_admin_test_send_warns_when_it_goes_to_server_log(self):
        # Email disabled: the test message lands in the console/log, and
        # the admin must say so with a warning, never a green "sent".
        config = EmailSettings.load()
        config.enabled = False
        config.test_recipient = "me@example.org"
        config.save()
        self.client.force_login(self.staff)
        with patch("sys.stdout", new=StringIO()):
            response = self.client.post(
                reverse(
                    "admin:notifications_emailsettings_change", args=[config.pk]
                ),
                self._change_form_data(
                    config, test_recipient="me@example.org", _send_test="1"
                ),
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Not delivered")
        self.assertContains(response, "email is disabled")
        self.assertContains(response, "server log")
        config.refresh_from_db()
        self.assertIn("went to server log (email is disabled)", config.last_test_result)

    def test_admin_send_test_without_recipient_errors_cleanly(self):
        config = EmailSettings.load()
        # Blank both fallbacks explicitly: load() may have seeded
        # from_email from the environment's DEFAULT_FROM_EMAIL.
        config.from_email = ""
        config.test_recipient = ""
        config.save()
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("admin:notifications_emailsettings_change", args=[config.pk]),
            self._change_form_data(config, _send_test="1"),
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        self.assertContains(response, "Set a test recipient")

    def test_admin_test_send_surfaces_provider_rejection(self):
        # A provider API rejection (bad key, refused recipient, quota)
        # must reach the admin as a visible error and be recorded on the
        # row, never pass as a silent success.
        config = EmailSettings.load()
        config.test_recipient = "me@example.org"
        config.save()
        self.client.force_login(self.staff)
        with patch(
            "notifications.admin.send_test_email",
            side_effect=AnymailAPIError("Mailjet API response 401: invalid key"),
        ):
            response = self.client.post(
                reverse(
                    "admin:notifications_emailsettings_change", args=[config.pk]
                ),
                self._change_form_data(
                    config, test_recipient="me@example.org", _send_test="1"
                ),
                follow=True,
            )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Test email failed")
        self.assertContains(response, "invalid key")
        config.refresh_from_db()
        self.assertIn("failed: Mailjet API response 401", config.last_test_result)
        self.assertIsNotNone(config.last_test_at)

    def test_send_test_email_command(self):
        call_command("send_test_email", "cli@example.org")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["cli@example.org"])

    def test_send_test_email_command_requires_some_recipient(self):
        config = EmailSettings.load()
        config.from_email = ""
        config.test_recipient = ""
        config.save()
        with self.assertRaises(CommandError):
            call_command("send_test_email")


class EventLayerTests(TestCase):
    """The phase-2 event layer: emitters, subscriptions, dedup, registry."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner")
        self.asker = User.objects.create_user(username="asker")
        self.replier = User.objects.create_user(username="replier")
        self.staff = User.objects.create_user(username="staffer", is_staff=True)
        from projects.models import Project

        self.project = Project.objects.create(
            slug="pump",
            title="Pump",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.owner,
        )

    def _thread(self, author=None):
        from conversations.models import ProjectThread

        return ProjectThread.objects.create(
            project=self.project, author=author or self.asker, title="Q", body="?"
        )

    def test_unregistered_kind_is_refused(self):
        from notifications import events

        self.assertIsNone(
            events._emit(self.owner, kind="not-a-kind", title="x")
        )

    def test_thread_created_notifies_owner_not_author(self):
        from notifications import events

        events.thread_created(self._thread())
        kinds = list(
            Notification.objects.values_list("user_id", "kind")
        )
        self.assertEqual(kinds, [(self.owner.pk, "project_question")])

    def test_thread_created_by_owner_notifies_nobody(self):
        from notifications import events

        events.thread_created(self._thread(author=self.owner))
        self.assertEqual(Notification.objects.count(), 0)

    def test_reply_notifies_subscribers_minus_actor(self):
        from conversations.models import ProjectReply, ensure_subscribed
        from notifications import events

        thread = self._thread()
        ensure_subscribed(self.asker, thread)
        ensure_subscribed(self.owner, thread)
        reply = ProjectReply.objects.create(
            thread=thread, author=self.replier, body="try this"
        )
        events.thread_replied(reply)
        recipients = set(
            Notification.objects.filter(kind="thread_reply").values_list(
                "user_id", flat=True
            )
        )
        self.assertEqual(recipients, {self.asker.pk, self.owner.pk})

    def test_unfollow_is_persistent_and_stops_notifications(self):
        from conversations.models import (
            ProjectReply,
            ThreadSubscription,
            ensure_subscribed,
        )
        from notifications import events

        thread = self._thread()
        ensure_subscribed(self.asker, thread)
        ThreadSubscription.objects.filter(user=self.asker, thread=thread).update(
            subscribed=False
        )
        # Auto-follow after contributing must NOT override the mute.
        ensure_subscribed(self.asker, thread)
        reply = ProjectReply.objects.create(
            thread=thread, author=self.replier, body="x"
        )
        events.thread_replied(reply)
        self.assertEqual(
            Notification.objects.filter(kind="thread_reply").count(), 0
        )

    def test_reply_flow_through_view_subscribes_and_notifies(self):
        thread = self._thread()
        from conversations.models import ensure_subscribed

        ensure_subscribed(self.asker, thread)
        self.client.force_login(self.replier)
        response = self.client.post(
            reverse(
                "conversations:detail", args=[self.project.slug, thread.pk]
            ),
            {"body": "did you check the seal?"},
        )
        self.assertEqual(response.status_code, 302)
        # Asker heard about it; the replier is now subscribed but was not
        # notified about their own reply.
        self.assertEqual(
            set(
                Notification.objects.filter(kind="thread_reply").values_list(
                    "user_id", flat=True
                )
            ),
            {self.asker.pk},
        )
        self.assertTrue(
            thread.subscriptions.filter(
                user=self.replier, subscribed=True
            ).exists()
        )

    def test_follow_toggle_view(self):
        thread = self._thread()
        self.client.force_login(self.replier)
        url = reverse(
            "conversations:toggle_follow", args=[self.project.slug, thread.pk]
        )
        self.client.post(url)
        self.assertTrue(
            thread.subscriptions.get(user=self.replier).subscribed
        )
        self.client.post(url)
        self.assertFalse(
            thread.subscriptions.get(user=self.replier).subscribed
        )

    def test_answer_accepted_notifies_reply_author(self):
        from conversations.models import ProjectReply
        from notifications import events

        thread = self._thread()
        reply = ProjectReply.objects.create(
            thread=thread, author=self.replier, body="fix"
        )
        events.answer_accepted(reply)
        n = Notification.objects.get(kind="answer_accepted")
        self.assertEqual(n.user, self.replier)

    def test_use_report_notifies_owner_only(self):
        from notifications import events
        from use_reports.models import UseReport

        report = UseReport.objects.create(
            project=self.project, author=self.replier, narrative="used it"
        )
        events.use_report_created(report)
        recipients = list(
            Notification.objects.filter(kind="use_report").values_list(
                "user_id", flat=True
            )
        )
        self.assertEqual(recipients, [self.owner.pk])

    def test_use_report_moderated_notifies_author(self):
        from notifications import events
        from use_reports.models import UseReport

        report = UseReport.objects.create(
            project=self.project, author=self.replier, narrative="used it"
        )
        events.use_report_moderated(report, hidden=True)
        n = Notification.objects.get(kind="use_report_moderated")
        self.assertEqual(n.user, self.replier)
        self.assertIn("hidden", n.title)

    def test_wiki_suggestion_dedups_per_page(self):
        from notifications import events
        from wiki.models import WikiPage, WikiRevision

        page = WikiPage.objects.create(
            project=self.project, title="Build notes", body=""
        )
        for i in range(2):
            revision = WikiRevision.objects.create(
                page=page,
                author=self.replier,
                title="Build notes",
                body=f"v{i}",
                status=WikiRevision.STATUS_PENDING,
            )
            events.wiki_suggestion_created(revision, self.project, page)
        self.assertEqual(
            Notification.objects.filter(kind="wiki_suggestion").count(), 1
        )

    def test_feedback_submitted_notifies_staff(self):
        from feedback.models import Feedback
        from notifications import events

        fb = Feedback.objects.create(user=self.asker, message="the button is odd")
        events.feedback_submitted(fb)
        recipients = list(
            Notification.objects.filter(kind="feedback_submitted").values_list(
                "user_id", flat=True
            )
        )
        self.assertEqual(recipients, [self.staff.pk])

    def test_project_published_notifies_staff_with_dedup(self):
        from notifications import events

        events.project_published(self.project)
        events.project_published(self.project)
        self.assertEqual(
            Notification.objects.filter(kind="project_published").count(), 1
        )
        n = Notification.objects.get(kind="project_published")
        self.assertEqual(n.user, self.staff)

    def test_project_hidden_notifies_owner(self):
        from notifications import events

        events.project_hidden(self.project, hidden=True)
        n = Notification.objects.get(kind="project_hidden")
        self.assertEqual(n.user, self.owner)

    def test_menu_fragment(self):
        send(self.owner, kind="generic", title="Hello bell")
        self.client.force_login(self.owner)
        response = self.client.get(reverse("notifications:menu"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hello bell")
        self.assertContains(response, "Mark all read")

    def test_menu_requires_login(self):
        response = self.client.get(reverse("notifications:menu"))
        self.assertEqual(response.status_code, 302)
