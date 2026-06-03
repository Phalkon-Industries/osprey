from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse

from conversations.models import ProjectReply, ProjectThread
from feedback.models import Feedback
from people.models import Profile
from projects.models import Contribution, Project
from use_reports.models import UseReport
from wiki.models import WikiPage, WikiRevision

from .helpers import log_action
from .models import ModerationLog, Report
from .views import _purge_user_content

User = get_user_model()


def _project(slug, *, public=True, hidden=False, created_by=None):
    return Project.objects.create(
        slug=slug,
        title=slug.replace("-", " ").title(),
        visibility=Project.VISIBILITY_PUBLIC if public else Project.VISIBILITY_PRIVATE,
        is_staff_hidden=hidden,
        created_by=created_by,
    )


class StaffHiddenProjectTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner")
        self.staff = User.objects.create_user(username="staff", is_staff=True)
        self.other = User.objects.create_user(username="other")
        self.project = _project("p1", public=True, hidden=True, created_by=self.owner)

    def test_hidden_project_is_404_for_non_staff(self):
        url = reverse("projects:detail", args=[self.project.slug])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)
        self.client.force_login(self.owner)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)

    def test_hidden_project_visible_to_staff(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("projects:detail", args=[self.project.slug]))
        self.assertEqual(resp.status_code, 200)

    def test_hidden_project_excluded_from_list(self):
        resp = self.client.get(reverse("projects:list"))
        self.assertNotContains(resp, self.project.title)


class ReportFlowTests(TestCase):
    def setUp(self):
        self.reporter = User.objects.create_user(username="reporter")
        self.staff = User.objects.create_user(username="staff", is_staff=True)
        self.owner = User.objects.create_user(username="owner")
        self.project = _project("rp1", created_by=self.owner)

    def test_submit_and_review_report(self):
        self.client.force_login(self.reporter)
        url = reverse(
            "moderation:report_new",
            args=["projects", "project", self.project.pk],
        )
        resp = self.client.post(
            url,
            data={"category": "spam", "reason": "looks spammy", "context_url": "/p/x/"},
        )
        self.assertEqual(resp.status_code, 302)
        report = Report.objects.get()
        self.assertEqual(report.category, "spam")
        self.assertEqual(report.target_repr, self.project.title)

        # Non-staff can't see queue.
        resp = self.client.get(reverse("moderation:queue"))
        self.assertEqual(resp.status_code, 302)

        self.client.force_login(self.staff)
        resp = self.client.get(reverse("moderation:queue"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "looks spammy")

        # Mark actioned.
        resp = self.client.post(
            reverse("moderation:report_detail", args=[report.pk]),
            data={"action": "action", "review_note": "hid the project"},
        )
        self.assertEqual(resp.status_code, 302)
        report.refresh_from_db()
        self.assertEqual(report.status, Report.STATUS_ACTIONED)
        self.assertEqual(report.reviewed_by, self.staff)
        self.assertTrue(
            ModerationLog.objects.filter(
                action="report_action", actor=self.staff
            ).exists()
        )

    def test_report_unreportable_model_404(self):
        self.client.force_login(self.reporter)
        url = reverse("moderation:report_new", args=["sessions", "session", 1])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)


class ProjectHideActionTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username="staff", is_staff=True)
        self.other = User.objects.create_user(username="other")
        self.project = _project("ph1")

    def test_non_staff_blocked(self):
        self.client.force_login(self.other)
        resp = self.client.post(
            reverse("moderation:project_hide", args=[self.project.pk]),
            data={"reason": "spam"},
        )
        self.assertEqual(resp.status_code, 302)
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_staff_hidden)

    def test_staff_can_hide_and_unhide(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse("moderation:project_hide", args=[self.project.pk]),
            data={"reason": "spam"},
        )
        self.assertEqual(resp.status_code, 302)
        self.project.refresh_from_db()
        self.assertTrue(self.project.is_staff_hidden)
        self.assertTrue(ModerationLog.objects.filter(action="project_hide").exists())

        resp = self.client.post(
            reverse("moderation:project_unhide", args=[self.project.pk]),
            data={"reason": "false report"},
        )
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_staff_hidden)


class UserSuspendTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username="staff", is_staff=True)
        self.victim = User.objects.create_user(username="victim")
        self.project = _project("vp1", created_by=self.victim)

    def test_suspend_blocks_active_and_optionally_hides_content(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse("moderation:user_suspend", args=[self.victim.pk]),
            data={"reason": "abuse", "hide_content": "1"},
        )
        self.assertEqual(resp.status_code, 302)
        self.victim.refresh_from_db()
        self.assertFalse(self.victim.is_active)
        self.project.refresh_from_db()
        self.assertTrue(self.project.is_staff_hidden)
        self.assertTrue(ModerationLog.objects.filter(action="user_suspend").exists())

    def test_reinstate(self):
        self.victim.is_active = False
        self.victim.save()
        self.client.force_login(self.staff)
        resp = self.client.post(
            reverse("moderation:user_reinstate", args=[self.victim.pk]),
            data={"reason": "false report"},
        )
        self.assertEqual(resp.status_code, 302)
        self.victim.refresh_from_db()
        self.assertTrue(self.victim.is_active)


class BulkPurgeTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username="staff", is_staff=True)
        self.victim = User.objects.create_user(username="victim")
        self.collaborator = User.objects.create_user(username="collab")

        # Solo project - should be deleted.
        self.solo = _project("solo", created_by=self.victim)
        Contribution.objects.create(
            project=self.solo, user=self.victim, display_name="victim"
        )

        # Shared project - should be kept; victim's contribution removed.
        self.shared = _project("shared", created_by=self.collaborator)
        Contribution.objects.create(
            project=self.shared, user=self.collaborator, display_name="collab"
        )
        Contribution.objects.create(
            project=self.shared, user=self.victim, display_name="victim"
        )

        # Wiki revision, use report, conversation, feedback by victim
        self.page = WikiPage.objects.create(
            project=self.shared, title="X", body="b", slug="x"
        )
        WikiRevision.objects.create(
            page=self.page,
            author=self.victim,
            title="X",
            body="b",
            status=WikiRevision.STATUS_APPLIED,
        )
        UseReport.objects.create(
            project=self.shared, author=self.victim, narrative="abc"
        )
        self.thread = ProjectThread.objects.create(
            project=self.shared, author=self.victim, title="t", body="b"
        )
        ProjectReply.objects.create(thread=self.thread, author=self.victim, body="r")
        Feedback.objects.create(user=self.victim, message="hi")

    def test_purge_removes_solo_content_only(self):
        counts = _purge_user_content(self.victim)
        self.assertEqual(counts["projects"], 1)
        self.assertFalse(Project.objects.filter(pk=self.solo.pk).exists())
        self.assertTrue(Project.objects.filter(pk=self.shared.pk).exists())
        self.assertFalse(Contribution.objects.filter(user=self.victim).exists())
        self.assertEqual(WikiRevision.objects.filter(author=self.victim).count(), 0)
        self.assertEqual(UseReport.objects.filter(author=self.victim).count(), 0)
        self.assertEqual(ProjectThread.objects.filter(author=self.victim).count(), 0)
        self.assertEqual(ProjectReply.objects.filter(author=self.victim).count(), 0)
        self.assertEqual(Feedback.objects.filter(user=self.victim).count(), 0)
        # Victim user record itself untouched.
        self.victim.refresh_from_db()
        self.assertTrue(User.objects.filter(pk=self.victim.pk).exists())

    def test_purge_view_requires_typed_confirm(self):
        self.client.force_login(self.staff)
        url = reverse("moderation:user_purge", args=[self.victim.pk])
        resp = self.client.post(url, data={"reason": "x", "confirm": "wrong"})
        self.assertEqual(resp.status_code, 302)
        # Solo project should still exist - no purge happened.
        self.assertTrue(Project.objects.filter(pk=self.solo.pk).exists())

        resp = self.client.post(
            url, data={"reason": "x", "confirm": self.victim.get_username()}
        )
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Project.objects.filter(pk=self.solo.pk).exists())
        self.assertTrue(
            ModerationLog.objects.filter(action="user_purge_content").exists()
        )


class LogActionTests(TestCase):
    def test_log_action_records_target(self):
        actor = User.objects.create_user(username="actor", is_staff=True)
        target = User.objects.create_user(username="target")
        log = log_action(actor, "user_suspend", target=target, reason="why")
        self.assertEqual(log.actor, actor)
        self.assertEqual(log.action, "user_suspend")
        self.assertEqual(log.target_id, target.pk)
        self.assertEqual(log.target_ct, ContentType.objects.get_for_model(User))
        self.assertEqual(log.reason, "why")
