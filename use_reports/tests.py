from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from projects.models import Project

from .models import UseReport


class UseReportTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="alice")
        self.user = User.objects.create_user(username="bob")
        self.project = Project.objects.create(
            title="Pump",
            slug="pump",
            created_by=self.owner,
            visibility=Project.VISIBILITY_PUBLIC,
        )
        self.project.contributions.create(
            user=self.owner, display_name="Alice", role="Lead"
        )

    def test_index_lists_only_public_use_reports(self):
        UseReport.objects.create(
            project=self.project, author=self.user, narrative="visible"
        )
        UseReport.objects.create(
            project=self.project,
            author=self.user,
            narrative="hidden one",
            visibility=UseReport.VIS_HIDDEN,
        )
        response = self.client.get(
            reverse("use_reports:index", args=[self.project.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "visible")
        self.assertNotContains(response, "hidden one")

    def test_signed_in_user_can_post_use_report(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("use_reports:new", args=[self.project.slug]),
            {"narrative": "I used this on a cruise.", "used_at": "2024"},
        )
        self.assertEqual(response.status_code, 302)
        report = UseReport.objects.get()
        self.assertEqual(report.author, self.user)
        self.assertEqual(report.narrative, "I used this on a cruise.")
        self.assertEqual(report.visibility, UseReport.VIS_PUBLIC)

    def test_maintainer_cannot_hide_only_staff(self):
        report = UseReport.objects.create(
            project=self.project, author=self.user, narrative="x"
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("use_reports:moderate", args=[self.project.slug, report.pk]),
            {"action": "hide"},
        )
        self.assertEqual(response.status_code, 403)
        report.refresh_from_db()
        self.assertEqual(report.visibility, UseReport.VIS_PUBLIC)

    def test_staff_can_hide(self):
        report = UseReport.objects.create(
            project=self.project, author=self.user, narrative="x"
        )
        User = get_user_model()
        staff = User.objects.create_user(
            "modstaff", "mod@example.org", "x", is_staff=True
        )
        self.client.force_login(staff)
        self.client.post(
            reverse("use_reports:moderate", args=[self.project.slug, report.pk]),
            {"action": "hide"},
        )
        report.refresh_from_db()
        self.assertEqual(report.visibility, UseReport.VIS_HIDDEN)

    def test_stranger_cannot_moderate(self):
        report = UseReport.objects.create(
            project=self.project, author=self.user, narrative="x"
        )
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("use_reports:moderate", args=[self.project.slug, report.pk]),
            {"action": "hide"},
        )
        self.assertEqual(response.status_code, 403)

    def test_anonymous_cannot_post(self):
        response = self.client.post(
            reverse("use_reports:new", args=[self.project.slug]),
            {"narrative": "anon"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(UseReport.objects.count(), 0)
