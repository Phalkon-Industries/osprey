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
from django.utils import timezone

from anymail.backends.mailjet import EmailBackend as MailjetBackend
from anymail.exceptions import AnymailAPIError

from .email import OspreyEmailBackend
from .models import (
    EmailSettings,
    Notification,
    NotificationPreference,
    QueuedEmail,
    send,
)


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


class EventEdgeCaseTests(TestCase):
    """Edge cases: deleted users, ownerless projects, self-actions,
    inactive recipients, dedup semantics, and field truncation."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner")
        self.other = User.objects.create_user(username="other")
        self.staff_a = User.objects.create_user(username="staff_a", is_staff=True)
        self.staff_b = User.objects.create_user(username="staff_b", is_staff=True)
        from projects.models import Project

        self.project = Project.objects.create(
            slug="edge-pump",
            title="Edge Pump",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.owner,
        )

    def test_send_truncates_long_title_and_url(self):
        n = send(
            self.owner,
            kind="generic",
            title="t" * 500,
            url="/x/" + "y" * 500,
        )
        self.assertEqual(len(n.title), 200)
        self.assertEqual(len(n.url), 400)

    def test_thread_created_on_ownerless_project_is_quiet(self):
        from conversations.models import ProjectThread
        from notifications import events

        self.project.created_by = None
        self.project.save(update_fields=["created_by"])
        thread = ProjectThread.objects.create(
            project=self.project, author=self.other, title="Q", body="?"
        )
        events.thread_created(thread)  # must not raise
        self.assertEqual(Notification.objects.count(), 0)

    def test_reply_on_thread_with_deleted_author_still_notifies_subscribers(self):
        from conversations.models import (
            ProjectReply,
            ProjectThread,
            ensure_subscribed,
        )
        from notifications import events

        thread = ProjectThread.objects.create(
            project=self.project, author=None, title="Orphan", body="?"
        )
        ensure_subscribed(self.owner, thread)
        reply = ProjectReply.objects.create(
            thread=thread, author=self.other, body="answer"
        )
        events.thread_replied(reply)
        self.assertEqual(
            list(
                Notification.objects.filter(kind="thread_reply").values_list(
                    "user_id", flat=True
                )
            ),
            [self.owner.pk],
        )

    def test_accepting_your_own_reply_notifies_nobody(self):
        from conversations.models import ProjectReply, ProjectThread
        from notifications import events

        thread = ProjectThread.objects.create(
            project=self.project, author=self.other, title="Q", body="?"
        )
        reply = ProjectReply.objects.create(
            thread=thread, author=self.other, body="answered myself"
        )
        events.answer_accepted(reply)
        self.assertEqual(Notification.objects.count(), 0)

    def test_accepted_reply_with_deleted_author_is_quiet(self):
        from conversations.models import ProjectReply, ProjectThread
        from notifications import events

        thread = ProjectThread.objects.create(
            project=self.project, author=self.other, title="Q", body="?"
        )
        reply = ProjectReply.objects.create(thread=thread, author=None, body="x")
        events.answer_accepted(reply)  # must not raise
        self.assertEqual(Notification.objects.count(), 0)

    def test_inactive_staff_are_not_notified(self):
        from feedback.models import Feedback
        from notifications import events

        self.staff_b.is_active = False
        self.staff_b.save(update_fields=["is_active"])
        fb = Feedback.objects.create(user=self.other, message="hi")
        events.feedback_submitted(fb)
        recipients = set(
            Notification.objects.filter(kind="feedback_submitted").values_list(
                "user_id", flat=True
            )
        )
        self.assertEqual(recipients, {self.staff_a.pk})

    def test_staff_reporter_is_excluded_from_report_notifications(self):
        from moderation.models import Report
        from django.contrib.contenttypes.models import ContentType
        from notifications import events

        report = Report.objects.create(
            reporter=self.staff_a,
            target_ct=ContentType.objects.get_for_model(self.project),
            target_id=self.project.pk,
            target_repr="Edge Pump",
            category="spam",
            reason="looks off",
        )
        events.content_report_filed(report)
        recipients = set(
            Notification.objects.filter(kind="content_report").values_list(
                "user_id", flat=True
            )
        )
        self.assertEqual(recipients, {self.staff_b.pk})

    def test_staff_creator_excluded_from_own_publish_event(self):
        from notifications import events
        from projects.models import Project

        staff_project = Project.objects.create(
            slug="staff-pump",
            title="Staff Pump",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.staff_a,
        )
        events.project_published(staff_project)
        recipients = set(
            Notification.objects.filter(kind="project_published").values_list(
                "user_id", flat=True
            )
        )
        self.assertEqual(recipients, {self.staff_b.pk})

    def test_dedup_applies_per_user_not_globally(self):
        from notifications import events

        events._emit(
            self.staff_a, kind="content_report", title="r", dedup_key="k1"
        )
        events._emit(
            self.staff_b, kind="content_report", title="r", dedup_key="k1"
        )
        self.assertEqual(Notification.objects.count(), 2)

    def test_dedup_releases_after_notification_is_read(self):
        from notifications import events

        first = events._emit(
            self.owner, kind="project_question", title="q", dedup_key="k2"
        )
        # While unread: suppressed.
        self.assertIsNone(
            events._emit(
                self.owner, kind="project_question", title="q", dedup_key="k2"
            )
        )
        first.is_read = True
        first.save(update_fields=["is_read"])
        # Once read, new activity may notify again.
        self.assertIsNotNone(
            events._emit(
                self.owner, kind="project_question", title="q", dedup_key="k2"
            )
        )

    def test_use_report_restore_says_restored(self):
        from notifications import events
        from use_reports.models import UseReport

        report = UseReport.objects.create(
            project=self.project, author=self.other, narrative="used"
        )
        events.use_report_moderated(report, hidden=False)
        n = Notification.objects.get(kind="use_report_moderated")
        self.assertIn("restored", n.title)

    def test_project_unhide_notifies_owner_with_restored_wording(self):
        from notifications import events

        events.project_hidden(self.project, hidden=False)
        n = Notification.objects.get(kind="project_hidden")
        self.assertEqual(n.user, self.owner)
        self.assertIn("restored", n.title)

    def test_wiki_review_of_anonymous_suggestion_is_quiet(self):
        from notifications import events
        from wiki.models import WikiPage, WikiRevision

        page = WikiPage.objects.create(project=self.project, title="N", body="")
        revision = WikiRevision.objects.create(
            page=page, author=None, title="N", body="v",
            status=WikiRevision.STATUS_PENDING,
        )
        events.wiki_suggestion_reviewed(
            revision, self.project, approved=True, page_slug=page.slug
        )
        self.assertEqual(Notification.objects.count(), 0)


class EmitterFailureResilienceTests(TestCase):
    """A broken notification layer must never break the user's action."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner")
        self.visitor = User.objects.create_user(username="visitor")
        from projects.models import Project

        self.project = Project.objects.create(
            slug="resilient-pump",
            title="Resilient Pump",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.owner,
        )

    def test_thread_posts_even_if_notifications_explode(self):
        from conversations.models import ProjectThread

        self.client.force_login(self.visitor)
        with patch(
            "notifications.events.thread_created",
            side_effect=RuntimeError("boom"),
        ):
            response = self.client.post(
                reverse("conversations:new", args=[self.project.slug]),
                {"title": "Survives", "body": "even when the bell breaks"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            ProjectThread.objects.filter(title="Survives").exists()
        )

    def test_reply_posts_even_if_notifications_explode(self):
        from conversations.models import ProjectReply, ProjectThread

        thread = ProjectThread.objects.create(
            project=self.project, author=self.owner, title="Q", body="?"
        )
        self.client.force_login(self.visitor)
        with patch(
            "notifications.events.thread_replied",
            side_effect=RuntimeError("boom"),
        ):
            response = self.client.post(
                reverse(
                    "conversations:detail", args=[self.project.slug, thread.pk]
                ),
                {"body": "still lands"},
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ProjectReply.objects.filter(body="still lands").exists())


class MenuAndInboxEdgeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="alice")

    def test_menu_caps_at_ten_items(self):
        for i in range(12):
            send(self.user, kind="generic", title=f"item-{i}")
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications:menu"))
        self.assertEqual(response.content.decode().count("bell-menu-item"), 10)

    def test_menu_escapes_html_in_titles(self):
        send(self.user, kind="generic", title="<script>alert(1)</script>")
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications:menu"))
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;")

    def test_menu_orders_newest_first(self):
        send(self.user, kind="generic", title="older")
        send(self.user, kind="generic", title="newer")
        self.client.force_login(self.user)
        body = self.client.get(reverse("notifications:menu")).content.decode()
        self.assertLess(body.index("newer"), body.index("older"))

    def test_open_notification_without_url_falls_back_to_inbox(self):
        n = send(self.user, kind="generic", title="no destination")
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications:open", args=[n.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("notifications:inbox"))

    def test_anonymous_pages_render_without_badge_errors(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "data-bell")


class EmailAdminAccessTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.regular = User.objects.create_user(username="regular")
        self.staff = User.objects.create_superuser(username="root2", password="x")

    def test_email_settings_pages_require_staff(self):
        config = EmailSettings.load()
        self.client.force_login(self.regular)
        for url in [
            reverse("admin:notifications_emailsettings_changelist"),
            reverse("admin:notifications_emailsettings_change", args=[config.pk]),
        ]:
            response = self.client.get(url)
            # Django admin bounces non-staff to its login page.
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/admin/login/", response["Location"])

    @override_settings(
        ANYMAIL={"MAILJET_API_KEY": "k", "MAILJET_SECRET_KEY": "s"}
    )
    def test_provider_status_reports_ready(self):
        from .admin import EmailSettingsAdmin

        status = EmailSettingsAdmin(EmailSettings, None).delivery_provider_status(
            EmailSettings.load()
        )
        self.assertIn("ready", status)
        self.assertNotIn("not ready", status)

    @override_settings(ANYMAIL={})
    def test_provider_status_reports_not_ready_with_reason(self):
        from .admin import EmailSettingsAdmin

        status = EmailSettingsAdmin(EmailSettings, None).delivery_provider_status(
            EmailSettings.load()
        )
        self.assertIn("not ready", status)
        self.assertIn("env file", status)

    def test_command_falls_back_to_admin_test_recipient(self):
        config = EmailSettings.load()
        config.test_recipient = "fallback@example.org"
        config.save()
        call_command("send_test_email")
        self.assertEqual(mail.outbox[0].to, ["fallback@example.org"])


def _verify_email(user, address="user@example.org"):
    from allauth.account.models import EmailAddress

    return EmailAddress.objects.create(
        user=user, email=address, verified=True, primary=True
    )


class EmailEnqueueTests(TestCase):
    """events -> outbox: who gets queued mail, and when."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner")
        self.asker = User.objects.create_user(username="asker")
        from projects.models import Project

        self.project = Project.objects.create(
            slug="mailpump",
            title="Mail Pump",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.owner,
        )

    def _fire_question(self):
        from conversations.models import ProjectThread
        from notifications import events

        thread = ProjectThread.objects.create(
            project=self.project, author=self.asker, title="Q", body="?"
        )
        events.thread_created(thread)

    def _prefs(self, **kwargs):
        preference = NotificationPreference.for_user(self.owner)
        for key, value in kwargs.items():
            setattr(preference, key, value)
        preference.save()
        return preference

    def test_immediate_cadence_with_verified_address_queues_mail(self):
        _verify_email(self.owner)
        self._prefs(projects="immediate")
        self._fire_question()
        row = QueuedEmail.objects.get()
        self.assertEqual(row.user, self.owner)
        self.assertEqual(row.group, "projects")
        self.assertIn("New question on Mail Pump", row.subject)
        self.assertIn("/discussion/", row.body_text)
        # The in-app notification exists too.
        self.assertEqual(Notification.objects.count(), 1)

    def test_daily_cadence_queues_nothing_immediately(self):
        _verify_email(self.owner)
        self._prefs(projects="daily")
        self._fire_question()
        self.assertEqual(QueuedEmail.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), 1)

    def test_master_switch_off_queues_nothing(self):
        _verify_email(self.owner)
        self._prefs(projects="immediate", email_enabled=False)
        self._fire_question()
        self.assertEqual(QueuedEmail.objects.count(), 0)

    def test_no_verified_address_queues_nothing(self):
        self._prefs(projects="immediate")
        self._fire_question()
        self.assertEqual(QueuedEmail.objects.count(), 0)

    def test_unverified_address_queues_nothing(self):
        from allauth.account.models import EmailAddress

        EmailAddress.objects.create(
            user=self.owner, email="x@example.org", verified=False, primary=True
        )
        self._prefs(projects="immediate")
        self._fire_question()
        self.assertEqual(QueuedEmail.objects.count(), 0)

    def test_group_off_queues_nothing(self):
        _verify_email(self.owner)
        self._prefs(projects="off")
        self._fire_question()
        self.assertEqual(QueuedEmail.objects.count(), 0)


class SenderCommandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="mailee")
        _verify_email(self.user, "mailee@example.org")

    def _row(self, **kwargs):
        defaults = {
            "user": self.user,
            "group": "projects",
            "subject": "[OSPREY] Something happened",
            "body_text": "Details here.",
        }
        defaults.update(kwargs)
        return QueuedEmail.objects.create(**defaults)

    def test_sends_with_footer_and_unsubscribe_header(self):
        row = self._row()
        call_command("send_queued_email")
        row.refresh_from_db()
        self.assertEqual(row.status, QueuedEmail.STATUS_SENT)
        self.assertIsNotNone(row.sent_at)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["mailee@example.org"])
        self.assertIn("Stop all OSPREY email:", message.body)
        self.assertIn("/inbox/unsubscribe/", message.body)
        self.assertIn("List-Unsubscribe", message.extra_headers)

    def test_cancels_when_address_removed(self):
        from allauth.account.models import EmailAddress

        row = self._row()
        EmailAddress.objects.all().delete()
        call_command("send_queued_email")
        row.refresh_from_db()
        self.assertEqual(row.status, QueuedEmail.STATUS_CANCELLED)
        self.assertEqual(len(mail.outbox), 0)

    def test_cancels_when_master_switch_off(self):
        row = self._row()
        preference = NotificationPreference.for_user(self.user)
        preference.email_enabled = False
        preference.save()
        call_command("send_queued_email")
        row.refresh_from_db()
        self.assertEqual(row.status, QueuedEmail.STATUS_CANCELLED)

    def test_digest_rows_are_delivered_not_cancelled(self):
        # Regression: the sender re-checks kill switches only; a group of
        # "digest" (not a preference group) must still deliver.
        row = self._row(group="digest")
        call_command("send_queued_email")
        row.refresh_from_db()
        self.assertEqual(row.status, QueuedEmail.STATUS_SENT)

    def test_failure_backs_off_then_gives_up(self):
        from django.utils import timezone as tz

        row = self._row()
        with patch(
            "notifications.management.commands.send_queued_email.EmailMessage"
        ) as message_class:
            message_class.return_value.send.side_effect = RuntimeError("mailjet down")
            call_command("send_queued_email")
        row.refresh_from_db()
        self.assertEqual(row.status, QueuedEmail.STATUS_QUEUED)
        self.assertEqual(row.attempts, 1)
        self.assertGreater(row.scheduled_for, tz.now())
        self.assertIn("mailjet down", row.last_error)
        # Exhaust the remaining attempts.
        row.attempts = 4
        row.scheduled_for = tz.now()
        row.save()
        with patch(
            "notifications.management.commands.send_queued_email.EmailMessage"
        ) as message_class:
            message_class.return_value.send.side_effect = RuntimeError("still down")
            call_command("send_queued_email")
        row.refresh_from_db()
        self.assertEqual(row.status, QueuedEmail.STATUS_FAILED)

    def test_future_scheduled_rows_wait(self):
        from datetime import timedelta

        from django.utils import timezone as tz

        row = self._row(scheduled_for=tz.now() + timedelta(hours=1))
        call_command("send_queued_email")
        row.refresh_from_db()
        self.assertEqual(row.status, QueuedEmail.STATUS_QUEUED)
        self.assertEqual(len(mail.outbox), 0)


class DigestCommandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="digestee")
        _verify_email(self.user, "digestee@example.org")
        self.preference = NotificationPreference.for_user(self.user)

    def _notify(self, kind="project_question", title="Something happened"):
        send(self.user, kind=kind, title=title, url="/projects/x/")

    def test_daily_digest_collects_and_advances_stamp(self):
        self._notify(title="First thing")
        self._notify(kind="thread_reply", title="Second thing")
        call_command("send_email_digests")
        row = QueuedEmail.objects.get(group="digest")
        self.assertIn("2 updates", row.subject)
        self.assertIn("First thing", row.body_text)
        self.assertIn("Second thing", row.body_text)
        self.preference.refresh_from_db()
        self.assertIsNotNone(self.preference.last_daily_digest_at)
        # Second run right away: nothing new, no second digest.
        call_command("send_email_digests")
        self.assertEqual(QueuedEmail.objects.filter(group="digest").count(), 1)

    def test_weekly_group_waits_a_week(self):
        from datetime import timedelta

        from django.utils import timezone as tz

        self.preference.projects = "weekly"
        self.preference.replies = "off"
        self.preference.last_weekly_digest_at = tz.now() - timedelta(days=2)
        self.preference.save()
        self._notify(title="Weekly thing")
        call_command("send_email_digests")
        self.assertEqual(QueuedEmail.objects.count(), 0)
        # Push the stamp past a week and it fires.
        self.preference.last_weekly_digest_at = tz.now() - timedelta(days=8)
        self.preference.save(update_fields=["last_weekly_digest_at"])
        call_command("send_email_digests")
        self.assertEqual(QueuedEmail.objects.filter(group="digest").count(), 1)

    def test_no_address_no_digest(self):
        from allauth.account.models import EmailAddress

        EmailAddress.objects.all().delete()
        self._notify()
        call_command("send_email_digests")
        self.assertEqual(QueuedEmail.objects.count(), 0)

    def test_opted_out_user_gets_no_digest(self):
        self.preference.email_enabled = False
        self.preference.save()
        self._notify()
        call_command("send_email_digests")
        self.assertEqual(QueuedEmail.objects.count(), 0)


class UnsubscribeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="unsub")
        _verify_email(self.user, "unsub@example.org")

    def test_group_token_turns_group_off(self):
        from notifications import emails

        token = emails.make_unsubscribe_token(self.user, "replies")
        url = reverse("notifications:unsubscribe", args=[token])
        # GET shows a confirm page (no state change), POST applies. The
        # user is NOT logged in for any of this.
        response = self.client.get(url)
        self.assertContains(response, "replies")
        response = self.client.post(url)
        self.assertContains(response, "Done")
        preference = NotificationPreference.for_user(self.user)
        self.assertEqual(preference.replies, "off")
        self.assertTrue(preference.email_enabled)

    def test_all_token_disables_email_deletes_address_and_cancels_queue(self):
        from allauth.account.models import EmailAddress

        from notifications import emails

        QueuedEmail.objects.create(
            user=self.user, group="projects", subject="s", body_text="b"
        )
        token = emails.make_unsubscribe_token(self.user, "all")
        response = self.client.post(
            reverse("notifications:unsubscribe", args=[token])
        )
        self.assertContains(response, "not send you any email")
        self.assertContains(response, "been removed")
        preference = NotificationPreference.for_user(self.user)
        self.assertFalse(preference.email_enabled)
        self.assertEqual(EmailAddress.objects.filter(user=self.user).count(), 0)
        self.assertEqual(
            QueuedEmail.objects.get().status, QueuedEmail.STATUS_CANCELLED
        )

    def test_tampered_token_is_rejected(self):
        from notifications import emails

        token = emails.make_unsubscribe_token(self.user, "all") + "x"
        response = self.client.get(
            reverse("notifications:unsubscribe", args=[token])
        )
        self.assertContains(response, "invalid")
        response = self.client.post(
            reverse("notifications:unsubscribe", args=[token])
        )
        preference = NotificationPreference.for_user(self.user)
        self.assertTrue(preference.email_enabled)


class NotificationSettingsViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="settee")
        self.client.force_login(self.user)
        self.url = reverse("notifications:settings")

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_add_email_sends_confirmation_and_records_consent(self):
        response = self.client.post(
            self.url,
            {"action": "add_email", "email": "new@example.org"},
            follow=True,
        )
        self.assertContains(response, "Confirmation sent")
        from allauth.account.models import EmailAddress

        address = EmailAddress.objects.get(user=self.user)
        self.assertEqual(address.email, "new@example.org")
        self.assertFalse(address.verified)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn("new@example.org", message.to)
        # The branded template, not allauth's example.com default.
        self.assertIn("[OSPREY]", message.subject)
        self.assertIn("email notifications on OSPREY", message.body)
        self.assertNotIn("example.com", message.body)
        preference = NotificationPreference.for_user(self.user)
        self.assertIsNotNone(preference.consented_at)
        self.assertEqual(preference.consent_source, "settings")

    def test_confirmation_link_verifies_address(self):
        # A distinct address: allauth rate-limits confirmation sends per
        # address in the (process-wide) cache, so reusing one across
        # tests silently sends nothing.
        self.client.post(
            self.url, {"action": "add_email", "email": "confirm-me@example.org"}
        )
        body = mail.outbox[0].body
        import re

        match = re.search(r"(/accounts/confirm-email/[^\s/]+/)", body)
        self.assertIsNotNone(match, body)
        response = self.client.get(match.group(1))
        self.assertEqual(response.status_code, 302)
        from allauth.account.models import EmailAddress

        self.assertTrue(EmailAddress.objects.get(user=self.user).verified)

    def test_remove_email_deletes_and_cancels_queue(self):
        _verify_email(self.user, "old@example.org")
        QueuedEmail.objects.create(
            user=self.user, group="projects", subject="s", body_text="b"
        )
        response = self.client.post(
            self.url, {"action": "remove_email"}, follow=True
        )
        self.assertContains(response, "not receive any email notifications at all")
        from allauth.account.models import EmailAddress

        self.assertEqual(EmailAddress.objects.count(), 0)
        self.assertEqual(
            QueuedEmail.objects.get().status, QueuedEmail.STATUS_CANCELLED
        )

    def test_disabling_email_deletes_address_and_cancels_queue(self):
        from allauth.account.models import EmailAddress

        _verify_email(self.user, "goner@example.org")
        preference = NotificationPreference.for_user(self.user)
        preference.consented_at = timezone.now()
        preference.save(update_fields=["consented_at"])
        QueuedEmail.objects.create(
            user=self.user, group="projects", subject="s", body_text="b"
        )
        # Unchecked box = the toggle form posts without email_enabled.
        response = self.client.post(
            self.url, {"action": "toggle_email"}, follow=True
        )
        self.assertContains(response, "has")
        self.assertContains(response, "been removed from OSPREY")
        preference.refresh_from_db()
        self.assertFalse(preference.email_enabled)
        self.assertIsNone(preference.consented_at)
        self.assertEqual(EmailAddress.objects.count(), 0)
        self.assertEqual(
            QueuedEmail.objects.get().status, QueuedEmail.STATUS_CANCELLED
        )

    def test_reenabling_email_starts_clean(self):
        preference = NotificationPreference.for_user(self.user)
        preference.email_enabled = False
        preference.save()
        response = self.client.post(
            self.url,
            {"action": "toggle_email", "email_enabled": "on"},
            follow=True,
        )
        self.assertContains(response, "Add and confirm an email")
        preference.refresh_from_db()
        self.assertTrue(preference.email_enabled)

    def test_disabled_email_greys_out_and_blocks_sections(self):
        preference = NotificationPreference.for_user(self.user)
        preference.email_enabled = False
        preference.save()
        response = self.client.get(self.url)
        self.assertContains(response, "settings-muted")
        self.assertContains(response, "holds no email address")
        # Server-side enforcement, not just greyed pixels: adding an
        # address and saving cadences are both refused while off.
        response = self.client.post(
            self.url,
            {"action": "add_email", "email": "sneaky@example.org"},
            follow=True,
        )
        self.assertContains(response, "Turn email notifications on")
        from allauth.account.models import EmailAddress

        self.assertEqual(EmailAddress.objects.count(), 0)
        self.client.post(self.url, {"action": "save_cadences", "projects": "immediate"})
        preference.refresh_from_db()
        self.assertEqual(preference.projects, "daily")

    def test_cadences_save_and_reject_bad_values(self):
        response = self.client.post(
            self.url,
            {
                "action": "save_cadences",
                "projects": "immediate",
                "replies": "weekly",
                "follows": "bogus-value",
                "account": "off",
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        preference = NotificationPreference.for_user(self.user)
        self.assertEqual(preference.projects, "immediate")
        self.assertEqual(preference.replies, "weekly")
        self.assertEqual(preference.follows, "daily")  # bogus ignored
        self.assertEqual(preference.account, "off")

    def test_thread_prefs_save_independently(self):
        self.client.post(
            self.url, {"action": "save_thread_prefs"}, follow=True
        )
        preference = NotificationPreference.for_user(self.user)
        self.assertFalse(preference.auto_follow_threads)
        self.client.post(
            self.url,
            {"action": "save_thread_prefs", "auto_follow_threads": "on"},
        )
        preference.refresh_from_db()
        self.assertTrue(preference.auto_follow_threads)


class EmailBannerTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="bannered")

    def test_banner_shows_without_address(self):
        self.client.force_login(self.user)
        self.assertContains(self.client.get("/"), "email-nudge-banner")

    def test_banner_hidden_with_verified_address(self):
        _verify_email(self.user)
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get("/"), "email-nudge-banner")

    def test_banner_hidden_for_opted_out_user(self):
        preference = NotificationPreference.for_user(self.user)
        preference.email_enabled = False
        preference.save()
        self.client.force_login(self.user)
        self.assertNotContains(self.client.get("/"), "email-nudge-banner")

    def test_banner_hidden_for_anonymous(self):
        self.assertNotContains(self.client.get("/"), "email-nudge-banner")

    def test_dismiss_hides_for_session_and_returns_next_login(self):
        self.client.force_login(self.user)
        self.client.post(
            reverse("notifications:dismiss_email_banner"), {"next": "/"}
        )
        self.assertNotContains(self.client.get("/"), "email-nudge-banner")
        # A fresh session (new sign-in) brings it back.
        self.client.logout()
        self.client.force_login(self.user)
        self.assertContains(self.client.get("/"), "email-nudge-banner")

    def test_dismiss_rejects_offsite_next(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("notifications:dismiss_email_banner"),
            {"next": "https://evil.example.com/"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("notifications:inbox"))


class PrivacyAndOnboardingSurfaceTests(TestCase):
    def test_privacy_page_renders(self):
        response = self.client.get("/about/privacy/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mailjet")
        self.assertContains(response, "No marketing and no newsletters")

    def test_footer_links_privacy(self):
        self.assertContains(self.client.get("/"), "/about/privacy/")

    def test_onboarding_offers_email_field(self):
        user = get_user_model().objects.create_user(username="newbie")
        self.client.force_login(user)
        response = self.client.get(reverse("people:onboarding"))
        self.assertContains(response, "Email notifications (optional)")
        self.assertContains(response, 'name="email"')
        self.assertContains(response, "/about/privacy/")


class EndToEndEmailFlowTests(TestCase):
    """The full path: event -> outbox -> sender -> mailbox."""

    def test_question_reaches_owner_mailbox(self):
        User = get_user_model()
        owner = User.objects.create_user(username="e2e-owner")
        asker = User.objects.create_user(username="e2e-asker")
        _verify_email(owner, "owner@example.org")
        preference = NotificationPreference.for_user(owner)
        preference.projects = "immediate"
        preference.save()
        from projects.models import Project

        project = Project.objects.create(
            slug="e2e-pump",
            title="E2E Pump",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=owner,
        )
        self.client.force_login(asker)
        self.client.post(
            reverse("conversations:new", args=[project.slug]),
            {"title": "Does it survive salt water?", "body": "Asking."},
        )
        call_command("send_queued_email")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["owner@example.org"])
        self.assertIn("New question on E2E Pump", message.subject)
        self.assertIn("Does it survive salt water?", message.body)
        self.assertIn("/discussion/", message.body)
        self.assertIn("unsubscribe", message.body.lower())


class FollowAndWatchTests(TestCase):
    """Phase 4: creator follows, project watches, and their fan-outs."""

    def setUp(self):
        User = get_user_model()
        self.creator = User.objects.create_user(username="creator")
        self.fan = User.objects.create_user(username="fan")
        self.watcher = User.objects.create_user(username="watcher")
        self.staffer = User.objects.create_user(username="staffer4", is_staff=True)
        from projects.models import Project

        self.project = Project.objects.create(
            slug="watched-pump",
            title="Watched Pump",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.creator,
        )

    def _follow(self, follower=None, creator=None):
        from people.models import Follow

        return Follow.objects.create(
            follower=follower or self.fan, creator=creator or self.creator
        )

    def _watch(self, user=None, project=None):
        from projects.models import Watch

        return Watch.objects.create(
            user=user or self.watcher, project=project or self.project
        )

    def test_follow_toggle_view_creates_and_removes_silently(self):
        from people.models import Follow

        self.client.force_login(self.fan)
        url = reverse("people:follow_toggle", args=[self.creator.pk])
        self.client.post(url)
        self.assertTrue(
            Follow.objects.filter(follower=self.fan, creator=self.creator).exists()
        )
        # Anti-gamification rule: the followed person is NOT notified,
        # in-app or by email. Follows are purely functional.
        self.assertEqual(Notification.objects.count(), 0)
        self.assertEqual(QueuedEmail.objects.count(), 0)
        # Toggle off.
        self.client.post(url)
        self.assertFalse(Follow.objects.exists())

    def test_cannot_follow_yourself(self):
        self.client.force_login(self.creator)
        response = self.client.post(
            reverse("people:follow_toggle", args=[self.creator.pk])
        )
        self.assertEqual(response.status_code, 403)
        from people.models import Follow

        self.assertFalse(Follow.objects.exists())

    def test_follow_requires_login(self):
        response = self.client.post(
            reverse("people:follow_toggle", args=[self.creator.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_profile_shows_follow_button_but_never_a_count(self):
        self._follow()
        self.client.force_login(self.watcher)
        response = self.client.get(reverse("people:detail", args=[self.creator.pk]))
        self.assertContains(response, ">Follow<")
        self.assertNotContains(response, "follower")
        # Own profile: no button, and no count for the owner either.
        self.client.force_login(self.creator)
        response = self.client.get(reverse("people:detail", args=[self.creator.pk]))
        self.assertNotContains(response, ">Follow<")
        self.assertNotContains(response, "follower")

    def test_project_page_never_shows_watch_count(self):
        self._watch()
        self._watch(user=self.fan)
        # Signed in as "fan" (whose username can't collide with the word
        # "watchers") the page shows the toggle but never any count.
        self.client.force_login(self.fan)
        response = self.client.get(
            reverse("projects:detail", args=[self.project.slug])
        )
        body = response.content.decode().lower()
        self.assertIn(">unwatch<", body)
        self.assertNotIn("watchers", body)
        self.assertNotIn("2 watch", body)
        self.assertNotIn("watched by", body)

    def test_publish_fans_out_to_followers_not_creator_not_staff(self):
        from notifications import events

        self._follow()  # fan follows creator
        self._follow(follower=self.staffer)  # staff follower: staff row only
        events.project_published(self.project)
        follower_rows = Notification.objects.filter(
            kind="followed_creator_published"
        )
        self.assertEqual(
            list(follower_rows.values_list("user_id", flat=True)), [self.fan.pk]
        )
        staff_rows = Notification.objects.filter(kind="project_published")
        self.assertEqual(
            list(staff_rows.values_list("user_id", flat=True)), [self.staffer.pk]
        )

    def test_watch_toggle_view(self):
        from projects.models import Watch

        self.client.force_login(self.watcher)
        url = reverse("projects:watch_toggle", args=[self.project.slug])
        self.client.post(url)
        self.assertTrue(
            Watch.objects.filter(user=self.watcher, project=self.project).exists()
        )
        self.client.post(url)
        self.assertFalse(Watch.objects.exists())

    def test_watch_toggle_hidden_project_404s_for_stranger(self):
        from projects.models import Project

        private = Project.objects.create(
            slug="secret-pump",
            title="Secret Pump",
            visibility=Project.VISIBILITY_PRIVATE,
            created_by=self.creator,
        )
        self.client.force_login(self.watcher)
        response = self.client.post(
            reverse("projects:watch_toggle", args=[private.slug])
        )
        self.assertEqual(response.status_code, 404)

    def test_project_page_shows_watch_button(self):
        self.client.force_login(self.watcher)
        response = self.client.get(
            reverse("projects:detail", args=[self.project.slug])
        )
        self.assertContains(response, ">Watch<")
        self._watch()
        response = self.client.get(
            reverse("projects:detail", args=[self.project.slug])
        )
        self.assertContains(response, ">Unwatch<")

    def test_new_version_notifies_watchers_not_actor(self):
        from notifications import events

        self._watch()
        self._watch(user=self.fan)
        events.new_version_published(self.project, actor=self.fan)
        recipients = set(
            Notification.objects.filter(
                kind="watched_version_published"
            ).values_list("user_id", flat=True)
        )
        self.assertEqual(recipients, {self.watcher.pk})

    def test_new_wiki_page_notifies_watchers(self):
        from notifications import events
        from wiki.models import WikiPage

        self._watch()
        page = WikiPage.objects.create(
            project=self.project, title="Bench notes", body="text"
        )
        events.watched_wiki_page_created(page, self.project, self.creator)
        n = Notification.objects.get(kind="watched_activity")
        self.assertEqual(n.user, self.watcher)
        self.assertIn("Bench notes", n.title)

    def test_use_report_notifies_watchers_but_not_owner_twice(self):
        from notifications import events
        from use_reports.models import UseReport

        self._watch()  # watcher watches
        self._watch(user=self.creator)  # owner also watches their own project
        report = UseReport.objects.create(
            project=self.project, author=self.fan, narrative="used it"
        )
        events.use_report_created(report)
        events.watched_use_report_created(report)
        # Owner: exactly one row (the owner kind); watcher: the watched kind.
        self.assertEqual(
            Notification.objects.filter(user=self.creator).count(), 1
        )
        self.assertEqual(
            Notification.objects.get(user=self.creator).kind, "use_report"
        )
        self.assertEqual(
            Notification.objects.get(kind="watched_activity").user, self.watcher
        )




class PruneCommandTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="pruned")

    def test_prunes_old_read_keeps_unread_and_recent(self):
        from datetime import timedelta

        from django.utils import timezone as tz

        old_read = send(self.user, kind="generic", title="old read")
        old_read.is_read = True
        old_read.save()
        Notification.objects.filter(pk=old_read.pk).update(
            created_at=tz.now() - timedelta(days=400)
        )
        old_unread = send(self.user, kind="generic", title="old unread")
        Notification.objects.filter(pk=old_unread.pk).update(
            created_at=tz.now() - timedelta(days=400)
        )
        fresh_read = send(self.user, kind="generic", title="fresh read")
        fresh_read.is_read = True
        fresh_read.save()
        old_sent = QueuedEmail.objects.create(
            user=self.user,
            subject="s",
            body_text="b",
            status=QueuedEmail.STATUS_SENT,
        )
        QueuedEmail.objects.filter(pk=old_sent.pk).update(
            created_at=tz.now() - timedelta(days=100)
        )
        stuck_queued = QueuedEmail.objects.create(
            user=self.user, subject="s2", body_text="b2"
        )
        QueuedEmail.objects.filter(pk=stuck_queued.pk).update(
            created_at=tz.now() - timedelta(days=100)
        )

        call_command("prune_notifications")

        remaining = set(Notification.objects.values_list("title", flat=True))
        self.assertEqual(remaining, {"old unread", "fresh read"})
        # Sent row pruned; still-queued row kept regardless of age.
        self.assertEqual(
            list(QueuedEmail.objects.values_list("subject", flat=True)), ["s2"]
        )


@override_settings(RATELIMIT_ENABLE=True, RATELIMIT_EMAIL_ADD="3/h")
class EmailAddRateLimitTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()  # rate-limit counters live in the cache
        self.user = get_user_model().objects.create_user(username="bomber")
        self.client.force_login(self.user)
        self.url = reverse("notifications:settings")

    def _add(self, address):
        from allauth.account.models import EmailAddress

        EmailAddress.objects.filter(user=self.user).delete()
        return self.client.post(
            self.url, {"action": "add_email", "email": address}, follow=True
        )

    def test_rotating_addresses_hits_the_limit(self):
        for i in range(3):
            self._add(f"victim{i}@example.org")
        sent_before = len(mail.outbox)
        response = self._add("victim99@example.org")
        self.assertContains(response, "Too many email changes")
        # The blocked attempt sent nothing and stored nothing.
        self.assertEqual(len(mail.outbox), sent_before)
        from allauth.account.models import EmailAddress

        self.assertFalse(
            EmailAddress.objects.filter(email="victim99@example.org").exists()
        )

    def test_limit_is_per_user(self):
        for i in range(3):
            self._add(f"victim{i}@example.org")
        other = get_user_model().objects.create_user(username="innocent")
        self.client.force_login(other)
        response = self.client.post(
            self.url,
            {"action": "add_email", "email": "mine@example.org"},
            follow=True,
        )
        self.assertContains(response, "Confirmation sent")
