from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from projects.models import Project

from .models import Attestation


class AttestationTests(TestCase):
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

    def test_index_lists_only_public_attestations(self):
        Attestation.objects.create(
            project=self.project, author=self.user, narrative="visible"
        )
        Attestation.objects.create(
            project=self.project,
            author=self.user,
            narrative="hidden one",
            visibility=Attestation.VIS_HIDDEN,
        )
        response = self.client.get(
            reverse("attestations:index", args=[self.project.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "visible")
        self.assertNotContains(response, "hidden one")

    def test_signed_in_user_can_post_attestation(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("attestations:new", args=[self.project.slug]),
            {"narrative": "I used this on a cruise.", "used_at": "2024"},
        )
        self.assertEqual(response.status_code, 302)
        att = Attestation.objects.get()
        self.assertEqual(att.author, self.user)
        self.assertEqual(att.narrative, "I used this on a cruise.")
        self.assertEqual(att.visibility, Attestation.VIS_PUBLIC)
        self.assertEqual(att.endorsement, Attestation.ENDORSE_NONE)

    def test_maintainer_can_endorse(self):
        att = Attestation.objects.create(
            project=self.project, author=self.user, narrative="x"
        )
        self.client.force_login(self.owner)
        self.client.post(
            reverse("attestations:moderate", args=[self.project.slug, att.pk]),
            {"action": "feature"},
        )
        att.refresh_from_db()
        self.assertEqual(att.endorsement, Attestation.ENDORSE_FEATURED)
        self.assertEqual(att.endorsed_by, self.owner)

    def test_maintainer_can_hide(self):
        att = Attestation.objects.create(
            project=self.project, author=self.user, narrative="x"
        )
        self.client.force_login(self.owner)
        self.client.post(
            reverse("attestations:moderate", args=[self.project.slug, att.pk]),
            {"action": "hide"},
        )
        att.refresh_from_db()
        self.assertEqual(att.visibility, Attestation.VIS_HIDDEN)

    def test_stranger_cannot_moderate(self):
        att = Attestation.objects.create(
            project=self.project, author=self.user, narrative="x"
        )
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("attestations:moderate", args=[self.project.slug, att.pk]),
            {"action": "hide"},
        )
        self.assertEqual(response.status_code, 403)

    def test_anonymous_cannot_post(self):
        response = self.client.post(
            reverse("attestations:new", args=[self.project.slug]),
            {"narrative": "anon"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Attestation.objects.count(), 0)
