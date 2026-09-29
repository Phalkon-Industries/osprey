"""The Files tab: downloads for native, registered, and indexed pages."""
from __future__ import annotations

import tempfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from projects import zenodo_register
from projects.management.commands.seed_indexed_examples import seed_indexed_examples
from projects.models import ArtifactLink, Project, ProjectAttachment
from projects.testing.fake_zenodo import FakeZenodoMixin
from projects.tests import add_orcid_account
from projects.tests_journeys import zip_bytes
from projects.tests_registered import OWNER_ORCID, RECORD
from projects.zenodo import publish_new_version_now, publish_project_now

_MEDIA = tempfile.mkdtemp(prefix="osprey-files-test-")


@override_settings(MEDIA_ROOT=_MEDIA)
class FilesTabTests(FakeZenodoMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner = get_user_model().objects.create_user(username="files-owner")
        add_orcid_account(self.owner, OWNER_ORCID)

    def _native(self, slug="files-pump", public=True):
        project = Project.objects.create(
            slug=slug,
            title="Files Pump",
            summary="A pump with files.",
            readme="# Files Pump",
            artifact_type="Hardware",
            field="Oceanography",
            license="MIT",
            canonical_url="https://github.com/example/files-pump",
            visibility=Project.VISIBILITY_PUBLIC if public else Project.VISIBILITY_PRIVATE,
            created_by=self.owner,
        )
        payload = zip_bytes()
        ProjectAttachment.objects.create(
            project=project,
            file=ContentFile(payload, name="pump-v1.zip"),
            filename="pump-v1.zip",
            size_bytes=len(payload),
        )
        return project

    def test_tab_appears_next_to_overview_everywhere(self):
        project = self._native()
        response = self.client.get(project.get_absolute_url())
        self.assertContains(response, reverse("projects:files", args=[project.slug]))
        self.assertNotContains(response, "<h2>Repositories</h2>")

    def test_draft_lists_local_attachments_for_the_owner_only(self):
        project = self._native(public=False)
        url = reverse("projects:files", args=[project.slug])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.owner)
        response = self.client.get(url)
        self.assertContains(response, "Draft files")
        self.assertContains(response, "pump-v1.zip")
        self.assertContains(response, "Moves to Zenodo on publish.")
        self.assertContains(response, "https://github.com/example/files-pump")

    def test_published_native_project_lists_files_per_version_with_downloads(self):
        project = self._native()
        deposit = publish_project_now(project, self.owner)
        payload = zip_bytes("firmware/v2.c")
        ProjectAttachment.objects.create(
            project=project,
            file=ContentFile(payload, name="pump-v2.zip"),
            filename="pump-v2.zip",
            size_bytes=len(payload),
        )
        publish_new_version_now(deposit, changelog="Second spin.", user=self.owner)
        response = self.client.get(reverse("projects:files", args=[project.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v2")
        self.assertContains(response, "pump-v2.zip")
        self.assertContains(response, "Older versions")
        self.assertContains(response, "pump-v1.zip")
        self.assertContains(response, "Zenodo record")
        self.assertContains(response, "download=1")
        self.assertNotContains(response, "Draft files")

    def test_registered_project_lists_record_files_and_record_link(self):
        record = self.fz.seed_published(RECORD, files=[("pump-v1.zip", 4096), ("bom.csv", 512)])
        project = zenodo_register.register_record(record.doi, self.owner)
        response = self.client.get(reverse("projects:files", args=[project.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pump-v1.zip")
        self.assertContains(response, "bom.csv")
        self.assertContains(response, "KB")
        self.assertContains(response, f"/records/{record.id}")
        self.assertContains(response, "Zenodo record")

    def test_indexed_entry_shows_design_files_and_original(self):
        entries = {e.source: e for e in seed_indexed_examples()}
        response = self.client.get(reverse("projects:files", args=[entries["hardwarex"].slug]))
        self.assertContains(response, "https://doi.org/10.17632/8tb37yjp9m.3")
        self.assertContains(response, "https://doi.org/10.1016/j.ohx.2026.e00839")
        Project.objects.filter(pk=entries["github"].pk).update(files_url="")
        response = self.client.get(reverse("projects:files", args=[entries["github"].slug]))
        self.assertContains(response, "not yet linked")
        overview = self.client.get(entries["github"].get_absolute_url())
        self.assertContains(overview, reverse("projects:files", args=[entries["github"].slug]))

    def test_repositories_moved_to_the_files_tab(self):
        project = self._native()
        ArtifactLink.objects.create(project=project, kind="repository", url="https://gitlab.com/example/mirror", label="Mirror")
        response = self.client.get(reverse("projects:files", args=[project.slug]))
        self.assertContains(response, "Repositories")
        self.assertContains(response, "https://gitlab.com/example/mirror")
