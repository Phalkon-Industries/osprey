from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from projects.models import Project

from .merge import three_way_merge
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
            base_title="Page",
            base_body="old",
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
            base_title="Page",
            base_body="old",
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


class ThreeWayMergeTests(TestCase):
    def test_clean_apply_when_base_matches_current(self):
        base = "a\nb\nc\n"
        current = base
        proposed = "a\nB\nc\n"
        merged, conflict = three_way_merge(base, current, proposed)
        self.assertEqual(merged, proposed)
        self.assertFalse(conflict)

    def test_disjoint_edits_both_apply(self):
        base = "intro\n\nmaterials\noriginal-materials\n\ncalibration\noriginal-cal\n"
        # Current changed the calibration section only.
        current = "intro\n\nmaterials\noriginal-materials\n\ncalibration\nnew-cal\n"
        # Proposed changed the materials section only (from base's view).
        proposed = "intro\n\nmaterials\nnew-materials\n\ncalibration\noriginal-cal\n"
        merged, conflict = three_way_merge(base, current, proposed)
        self.assertFalse(conflict)
        self.assertIn("new-materials", merged)
        self.assertIn("new-cal", merged)
        self.assertNotIn("original-materials", merged)
        self.assertNotIn("original-cal", merged)

    def test_overlapping_edits_flag_conflict_and_keep_current(self):
        base = "a\nb\nc\n"
        current = "a\nCURRENT\nc\n"
        proposed = "a\nPROPOSED\nc\n"
        merged, conflict = three_way_merge(base, current, proposed)
        self.assertTrue(conflict)
        # Conflict resolution falls back to keeping current to avoid losing landed text.
        self.assertIn("CURRENT", merged)
        self.assertNotIn("PROPOSED", merged)

    def test_new_page_proposal_returns_proposed(self):
        merged, conflict = three_way_merge("", "", "hello\nworld\n")
        self.assertEqual(merged, "hello\nworld\n")
        self.assertFalse(conflict)


class RevisionBaseTrackingTests(WikiTestCase):
    def setUp(self):
        super().setUp()
        self.project.wiki_requires_approval = True
        self.project.save(update_fields=["wiki_requires_approval"])

    def test_suggestion_records_base_body(self):
        page = WikiPage.objects.create(
            project=self.project, title="Spec", body="line 1\nline 2\n"
        )
        self.client.force_login(self.stranger)
        self.client.post(
            reverse("wiki:edit", args=[self.project.slug, page.slug]),
            {"title": "Spec", "body": "line 1\nline 2 edited\n", "summary": ""},
        )
        rev = page.revisions.get(status=WikiRevision.STATUS_PENDING)
        self.assertEqual(rev.base_body, "line 1\nline 2\n")
        self.assertEqual(rev.base_title, "Spec")

    def test_disjoint_suggestions_both_land(self):
        page = WikiPage.objects.create(
            project=self.project,
            title="Doc",
            body="top\nmiddle\nbottom\n",
        )
        # Alice (maintainer) edits the top section first.
        self.client.force_login(self.owner)
        self.client.post(
            reverse("wiki:edit", args=[self.project.slug, page.slug]),
            {"title": "Doc", "body": "TOP\nmiddle\nbottom\n", "summary": ""},
        )
        page.refresh_from_db()
        self.assertIn("TOP", page.body)
        self.assertIn("bottom", page.body)

        # Bob (non-maintainer) had been editing the bottom section from the
        # pre-Alice body. Simulate by submitting a suggestion whose base is
        # the original body but proposing a bottom-section change.
        self.client.force_login(self.editor)
        # We need the suggestion's base to be the original body, not the
        # post-Alice body. The edit view always reads the current page body
        # as base, so to test "Bob started editing earlier" we create the
        # revision directly with the original base.
        rev = WikiRevision.objects.create(
            page=page,
            author=self.editor,
            title="Doc",
            body="top\nmiddle\nBOTTOM\n",
            base_title="Doc",
            base_body="top\nmiddle\nbottom\n",
            status=WikiRevision.STATUS_PENDING,
        )

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("wiki:review_action", args=[self.project.slug, rev.pk]),
            {"action": "apply"},
        )
        self.assertEqual(response.status_code, 302)
        page.refresh_from_db()
        # Both edits should now be present.
        self.assertIn("TOP", page.body)
        self.assertIn("BOTTOM", page.body)
        self.assertIn("middle", page.body)

    def test_review_page_shows_diff_against_base(self):
        page = WikiPage.objects.create(
            project=self.project, title="Doc", body="alpha\nbeta\n"
        )
        WikiRevision.objects.create(
            page=page,
            author=self.stranger,
            title="Doc",
            body="alpha\nBETA\n",
            base_title="Doc",
            base_body="alpha\nbeta\n",
            status=WikiRevision.STATUS_PENDING,
        )
        self.client.force_login(self.owner)
        response = self.client.get(reverse("wiki:review", args=[self.project.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "BETA")
        self.assertContains(response, "Suggested change")

