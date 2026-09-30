"""Provided as-is: a badge, a sentence, a filter, and the owner's mute."""
from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from notifications.models import Notification
from projects.models import Project
from projects.tests import ProjectTestCase
from projects.zenodo import _osprey_metadata_payload, metadata_for_project


class AsIsTests(ProjectTestCase):
    def _mark(self):
        Project.objects.filter(pk=self.public_project.pk).update(provided_as_is=True)
        self.public_project.refresh_from_db()

    def test_form_checkbox_sets_and_clears_the_mark(self):
        self.client.force_login(self.owner)
        data = self.project_form_post_data(action="save")
        data["title"] = self.public_project.title
        data["provided_as_is"] = "on"
        self.client.post(reverse("projects:edit", args=[self.public_project.slug]), data)
        self.public_project.refresh_from_db()
        self.assertTrue(self.public_project.provided_as_is)
        data.pop("provided_as_is")
        self.client.post(reverse("projects:edit", args=[self.public_project.slug]), data)
        self.public_project.refresh_from_db()
        self.assertFalse(self.public_project.provided_as_is)
        page = self.client.get(reverse("projects:edit", args=[self.public_project.slug]))
        self.assertContains(page, "Provided as-is. No support or updates are planned.")

    def test_as_is_forces_an_open_wiki(self):
        Project.objects.filter(pk=self.public_project.pk).update(wiki_requires_approval=True)
        self.client.force_login(self.owner)
        data = self.project_form_post_data(action="save")
        data["title"] = self.public_project.title
        data["provided_as_is"] = "on"
        data["wiki_requires_approval"] = "on"
        self.client.post(reverse("projects:edit", args=[self.public_project.slug]), data)
        self.public_project.refresh_from_db()
        self.assertTrue(self.public_project.provided_as_is)
        self.assertFalse(self.public_project.wiki_requires_approval)
        # Posting review-on again while as-is changes nothing, and the form hides the option.
        self.client.post(reverse("projects:edit", args=[self.public_project.slug]), data)
        self.public_project.refresh_from_db()
        self.assertFalse(self.public_project.wiki_requires_approval)
        page = self.client.get(reverse("projects:edit", args=[self.public_project.slug]))
        self.assertContains(page, "Wiki edits apply directly on a project provided as-is.")
        # Any save path, not just the form.
        self.public_project.wiki_requires_approval = True
        self.public_project.save()
        self.public_project.refresh_from_db()
        self.assertFalse(self.public_project.wiki_requires_approval)
        # Un-marking gives the choice back.
        data.pop("provided_as_is")
        self.client.post(reverse("projects:edit", args=[self.public_project.slug]), data)
        self.public_project.refresh_from_db()
        self.assertTrue(self.public_project.wiki_requires_approval)

    def test_as_is_box_sits_above_the_readme(self):
        self._mark()
        body = self.client.get(self.public_project.get_absolute_url()).content.decode()
        self.assertLess(body.index("Provided as-is"), body.index("Bench notes"))
        self.assertIn('class="as-is-box"', body)

    def test_badge_sentence_card_and_ask_box(self):
        self._mark()
        page = self.client.get(self.public_project.get_absolute_url())
        self.assertContains(page, "Provided as-is")
        self.assertContains(page, "No support or updates are planned.")
        self.assertNotContains(page, "Mute activity")  # visitors get no mute button
        self.assertContains(self.client.get(reverse("projects:list")), 'class="badge as-is">as-is<')
        self.client.force_login(self.contributor)
        self.assertContains(self.client.get(reverse("conversations:new", args=[self.public_project.slug])), "Answers come from the community, if at all.")
        self.assertContains(self.client.get(reverse("conversations:index", args=[self.public_project.slug])), "Answers come from the community, if at all.")
        unmarked = self.client.get(reverse("conversations:index", args=[self.private_project.slug]))
        self.assertNotContains(unmarked, "Answers come from the community")

    def test_support_filter(self):
        self._mark()
        Project.objects.filter(pk=self.private_project.pk).update(visibility=Project.VISIBILITY_PUBLIC)
        self.assertContains(self.client.get(reverse("projects:list") + "?support=as_is"), "Public Pump")
        self.assertNotContains(self.client.get(reverse("projects:list") + "?support=as_is"), "Private Pump")
        self.assertNotContains(self.client.get(reverse("projects:list") + "?support=maintained"), "Public Pump")
        self.assertContains(self.client.get(reverse("projects:list") + "?support=maintained"), "Private Pump")
        both = self.client.get(reverse("projects:list"))
        self.assertContains(both, "Public Pump")
        self.assertContains(both, "Private Pump")

    def test_zenodo_metadata_and_sidecar_carry_the_mark(self):
        self._mark()
        metadata = metadata_for_project(self.public_project, None)
        self.assertIn("Provided as-is: no support or updates are planned.", metadata["notes"])
        self.assertTrue(_osprey_metadata_payload(self.public_project)["provided_as_is"])
        Project.objects.filter(pk=self.public_project.pk).update(provided_as_is=False)
        self.public_project.refresh_from_db()
        self.assertNotIn("notes", metadata_for_project(self.public_project, None))

    def test_owner_mute_stops_activity_notifications(self):
        from conversations.models import ProjectThread
        from notifications import events

        self._mark()
        self.client.force_login(self.owner)
        page = self.client.get(self.public_project.get_absolute_url())
        self.assertContains(page, "Mute activity")
        thread = ProjectThread.objects.create(project=self.public_project, author=self.contributor, title="Does it float?", body="Asking.")
        events.thread_created(thread)
        self.assertEqual(Notification.objects.filter(user=self.owner, kind="project_question").count(), 1)
        response = self.client.post(reverse("projects:mute_toggle", args=[self.public_project.slug]))
        self.assertEqual(response.status_code, 302)
        self.public_project.refresh_from_db()
        self.assertTrue(self.public_project.owner_activity_muted)
        thread2 = ProjectThread.objects.create(project=self.public_project, author=self.contributor, title="Still floating?", body="Asking again.")
        events.thread_created(thread2)
        self.assertEqual(Notification.objects.filter(user=self.owner, kind="project_question").count(), 1)
        page = self.client.get(self.public_project.get_absolute_url())
        self.assertContains(page, "Unmute activity")
        self.assertContains(page, "Activity here doesn't notify you.")
        # Only the owner can toggle it.
        self.client.force_login(self.contributor)
        self.assertEqual(self.client.post(reverse("projects:mute_toggle", args=[self.public_project.slug])).status_code, 404)
