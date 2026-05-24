from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from projects.models import Project

from .models import WikiPage, WikiRevision


class WikiTestCase(TestCase):
    def setUp(self):
        self.User = get_user_model()
        self.owner = self.User.objects.create_user(username="alice")
        self.editor = self.User.objects.create_user(username="bob")
        self.stranger = self.User.objects.create_user(username="carol")
        self.project = Project.objects.create(
            title="Pump",
            slug="pump",
            created_by=self.owner,
            visibility=Project.VISIBILITY_PUBLIC,
        )
        # Make alice a maintainer via contribution.
        self.project.contributions.create(
            user=self.owner, display_name="Alice", role="Lead"
        )


class OpenWikiTests(WikiTestCase):
    def setUp(self):
        super().setUp()
        self.project.wiki_requires_approval = False
        self.project.save(update_fields=["wiki_requires_approval"])

    def test_anonymous_can_view_public_wiki(self):
        WikiPage.objects.create(project=self.project, title="Home", body="Welcome")
        response = self.client.get(reverse("wiki:index", args=[self.project.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Home")

    def test_anonymous_cannot_edit(self):
        response = self.client.get(reverse("wiki:new", args=[self.project.slug]))
        self.assertEqual(response.status_code, 302)

    def test_signed_in_user_can_edit_open_wiki_directly(self):
        self.client.force_login(self.stranger)
        response = self.client.post(
            reverse("wiki:new", args=[self.project.slug]),
            {"title": "Intro", "body": "Hello world", "summary": "first"},
        )
        self.assertEqual(response.status_code, 302)
        page = WikiPage.objects.get(project=self.project, slug="intro")
        self.assertEqual(page.body, "Hello world")
        self.assertEqual(
            page.revisions.filter(status=WikiRevision.STATUS_APPLIED).count(), 1
        )

    def test_edit_existing_page_records_revision(self):
        page = WikiPage.objects.create(project=self.project, title="X", body="old")
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("wiki:edit", args=[self.project.slug, page.slug]),
            {"title": "X", "body": "new", "summary": "update"},
        )
        self.assertEqual(response.status_code, 302)
        page.refresh_from_db()
        self.assertEqual(page.body, "new")
        self.assertEqual(page.revisions.count(), 1)


class ApprovalWikiTests(WikiTestCase):
    def setUp(self):
        super().setUp()
        self.project.wiki_requires_approval = True
        self.project.save()

    def test_non_maintainer_edit_is_queued_as_pending(self):
        self.client.force_login(self.stranger)
        response = self.client.post(
            reverse("wiki:new", args=[self.project.slug]),
            {"title": "Suggested", "body": "Proposed body", "summary": "rfc"},
        )
        self.assertEqual(response.status_code, 302)
        page = WikiPage.objects.get(project=self.project, slug="suggested")
        # Page body stays empty until a maintainer applies.
        self.assertEqual(page.body, "")
        rev = page.revisions.get()
        self.assertEqual(rev.status, WikiRevision.STATUS_PENDING)
        self.assertEqual(rev.body, "Proposed body")

    def test_maintainer_bypasses_approval(self):
        self.client.force_login(self.owner)
        self.client.post(
            reverse("wiki:new", args=[self.project.slug]),
            {"title": "Direct", "body": "lands now", "summary": ""},
        )
        page = WikiPage.objects.get(project=self.project, slug="direct")
        self.assertEqual(page.body, "lands now")

    def test_maintainer_applies_pending_revision(self):
        page = WikiPage.objects.create(project=self.project, title="Page", body="old")
        rev = WikiRevision.objects.create(
            page=page,
            author=self.stranger,
            title="Page",
            body="new body",
            status=WikiRevision.STATUS_PENDING,
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("wiki:review_action", args=[self.project.slug, rev.pk]),
            {"action": "apply"},
        )
        self.assertEqual(response.status_code, 302)
        page.refresh_from_db()
        self.assertEqual(page.body, "new body")
        rev.refresh_from_db()
        self.assertEqual(rev.status, WikiRevision.STATUS_APPLIED)

    def test_maintainer_rejects_pending_revision(self):
        page = WikiPage.objects.create(project=self.project, title="Page", body="old")
        rev = WikiRevision.objects.create(
            page=page,
            author=self.stranger,
            title="Page",
            body="new body",
            status=WikiRevision.STATUS_PENDING,
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("wiki:review_action", args=[self.project.slug, rev.pk]),
            {"action": "reject"},
        )
        self.assertEqual(response.status_code, 302)
        page.refresh_from_db()
        self.assertEqual(page.body, "old")
        rev.refresh_from_db()
        self.assertEqual(rev.status, WikiRevision.STATUS_REJECTED)

    def test_stranger_cannot_review(self):
        page = WikiPage.objects.create(project=self.project, title="Page")
        rev = WikiRevision.objects.create(
            page=page,
            author=self.stranger,
            title="Page",
            body="new",
            status=WikiRevision.STATUS_PENDING,
        )
        self.client.force_login(self.stranger)
        response = self.client.post(
            reverse("wiki:review_action", args=[self.project.slug, rev.pk]),
            {"action": "apply"},
        )
        self.assertEqual(response.status_code, 403)
