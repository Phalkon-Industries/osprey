from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from projects.models import Project

from .models import ProjectReply, ProjectThread


class AcceptAnswerTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner")
        self.asker = User.objects.create_user(username="asker")
        self.helper = User.objects.create_user(username="helper")
        self.project = Project.objects.create(
            title="Pump",
            slug="pump",
            created_by=self.owner,
            visibility=Project.VISIBILITY_PUBLIC,
        )
        self.thread = ProjectThread.objects.create(
            project=self.project,
            author=self.asker,
            title="Why does the seal leak?",
            body="It leaks after 40 hours.",
        )
        self.reply = ProjectReply.objects.create(
            thread=self.thread,
            author=self.helper,
            body="Replace o-ring with Viton.",
        )

    def _accept(self, user):
        self.client.force_login(user)
        return self.client.post(
            reverse(
                "conversations:accept_answer",
                args=[self.project.slug, self.thread.pk],
            ),
            {"reply_id": self.reply.pk},
        )

    def test_asker_can_accept_reply(self):
        self._accept(self.asker)
        self.reply.refresh_from_db()
        self.assertIsNotNone(self.reply.accepted_by_asker_at)

    def test_non_asker_cannot_accept(self):
        response = self._accept(self.owner)
        self.assertEqual(response.status_code, 403)
        self.reply.refresh_from_db()
        self.assertIsNone(self.reply.accepted_by_asker_at)

    def test_accept_again_unaccepts(self):
        self._accept(self.asker)
        self._accept(self.asker)
        self.reply.refresh_from_db()
        self.assertIsNone(self.reply.accepted_by_asker_at)


class ThreadViewEdgeTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="powner")
        self.visitor = User.objects.create_user(username="visitor")
        self.stranger = User.objects.create_user(username="stranger")
        self.public_project = Project.objects.create(
            title="Public Pump",
            slug="public-pump",
            created_by=self.owner,
            visibility=Project.VISIBILITY_PUBLIC,
        )
        self.private_project = Project.objects.create(
            title="Private Pump",
            slug="private-pump",
            created_by=self.owner,
            visibility=Project.VISIBILITY_PRIVATE,
        )
        self.thread = ProjectThread.objects.create(
            project=self.public_project,
            author=self.visitor,
            title="Q",
            body="?",
        )

    def test_reply_on_closed_thread_is_forbidden(self):
        self.thread.is_closed = True
        self.thread.save(update_fields=["is_closed"])
        self.client.force_login(self.visitor)
        response = self.client.post(
            reverse(
                "conversations:detail",
                args=[self.public_project.slug, self.thread.pk],
            ),
            {"body": "late reply"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.thread.replies.count(), 0)

    def test_anonymous_reply_redirects_to_login_and_saves_nothing(self):
        response = self.client.post(
            reverse(
                "conversations:detail",
                args=[self.public_project.slug, self.thread.pk],
            ),
            {"body": "drive-by"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.thread.replies.count(), 0)

    def test_empty_reply_body_creates_nothing(self):
        self.client.force_login(self.visitor)
        self.client.post(
            reverse(
                "conversations:detail",
                args=[self.public_project.slug, self.thread.pk],
            ),
            {"body": "   "},
        )
        self.assertEqual(self.thread.replies.count(), 0)

    def test_follow_toggle_requires_login(self):
        response = self.client.post(
            reverse(
                "conversations:toggle_follow",
                args=[self.public_project.slug, self.thread.pk],
            )
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])
        self.assertEqual(self.thread.subscriptions.count(), 0)

    def test_follow_toggle_rejects_get(self):
        self.client.force_login(self.visitor)
        response = self.client.get(
            reverse(
                "conversations:toggle_follow",
                args=[self.public_project.slug, self.thread.pk],
            )
        )
        self.assertEqual(response.status_code, 405)

    def test_follow_toggle_404_on_thread_from_other_project(self):
        # Thread id must belong to the slug in the URL.
        other_thread = ProjectThread.objects.create(
            project=self.private_project, author=self.owner, title="P", body="?"
        )
        self.client.force_login(self.visitor)
        response = self.client.post(
            reverse(
                "conversations:toggle_follow",
                args=[self.public_project.slug, other_thread.pk],
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_private_project_thread_hidden_from_stranger(self):
        thread = ProjectThread.objects.create(
            project=self.private_project, author=self.owner, title="P", body="?"
        )
        self.client.force_login(self.stranger)
        response = self.client.post(
            reverse(
                "conversations:toggle_follow",
                args=[self.private_project.slug, thread.pk],
            )
        )
        self.assertEqual(response.status_code, 404)

    def test_detail_page_shows_follow_state(self):
        from .models import ensure_subscribed

        self.client.force_login(self.visitor)
        url = reverse(
            "conversations:detail",
            args=[self.public_project.slug, self.thread.pk],
        )
        self.assertContains(self.client.get(url), ">Follow<")
        ensure_subscribed(self.visitor, self.thread)
        self.assertContains(self.client.get(url), ">Unfollow<")
