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
