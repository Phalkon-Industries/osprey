from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from .models import Contribution, Project, ProjectDeposit
from .zenodo import build_project_archive, metadata_for_project, sync_project_to_zenodo


class ZenodoServiceTests(TestCase):
	def setUp(self):
		User = get_user_model()
		self.user = User.objects.create_user(username="alice")
		self.project = Project.objects.create(
			slug="test-pump",
			title="Test Pump Controller",
			summary="Open controller for a lab pump.",
			readme="# Test Pump\n\nBench notes.",
			artifact_type="firmware",
			field="oceanography",
			license="MIT",
			canonical_url="https://github.com/example/test-pump",
			visibility=Project.VISIBILITY_PUBLIC,
			created_by=self.user,
		)
		Contribution.objects.create(
			project=self.project,
			display_name="Alice Researcher",
			role="Project lead",
			order=0,
		)

	def test_metadata_for_project_matches_zenodo_shape(self):
		metadata = metadata_for_project(self.project)

		self.assertEqual(metadata["title"], "Test Pump Controller")
		self.assertEqual(metadata["upload_type"], "software")
		self.assertEqual(metadata["access_right"], "open")
		self.assertEqual(metadata["license"], "mit-license")
		self.assertEqual(metadata["creators"], [{"name": "Alice Researcher"}])
		self.assertIn("oceanography", metadata["keywords"])

	def test_project_archive_contains_snapshot_files(self):
		archive = build_project_archive(self.project)

		self.assertEqual(archive.filename, "osprey-test-pump-snapshot.zip")
		self.assertGreater(len(archive.content), 100)

	def test_sandbox_zenodo_badge_uses_sandbox_host(self):
		self.project.doi = "10.5072/zenodo.123"

		self.assertTrue(self.project.is_zenodo_doi)
		self.assertEqual(
			self.project.zenodo_badge_url,
			"https://sandbox.zenodo.org/badge/DOI/10.5072/zenodo.123.svg",
		)

	@override_settings(
		ZENODO_USE_SANDBOX=True,
		ZENODO_ACCESS_TOKEN="fake-token",
		ZENODO_API_BASE_URL="https://sandbox.zenodo.org",
		ZENODO_DEFAULT_COMMUNITY="",
	)
	@patch("projects.zenodo.ZenodoClient")
	def test_sync_project_to_zenodo_creates_deposit_and_sets_doi(self, client_class):
		client = client_class.from_settings.return_value
		client.create_deposition.return_value = {
			"id": 123,
			"links": {"bucket": "https://sandbox.zenodo.org/api/files/bucket"},
			"metadata": {"prereserve_doi": {"doi": "10.5072/zenodo.123"}},
		}
		client.update_deposition_metadata.return_value = {
			"id": 123,
			"links": {"bucket": "https://sandbox.zenodo.org/api/files/bucket"},
			"metadata": {"prereserve_doi": {"doi": "10.5072/zenodo.123"}},
		}
		client.upload_to_bucket.return_value = {"ok": True}

		deposit = sync_project_to_zenodo(self.project, self.user)

		self.assertEqual(deposit.state, ProjectDeposit.STATE_DRAFT)
		self.assertEqual(deposit.deposition_id, "123")
		self.assertEqual(deposit.doi, "10.5072/zenodo.123")
		self.project.refresh_from_db()
		self.assertEqual(self.project.doi, "10.5072/zenodo.123")
		client.update_deposition_metadata.assert_called_once()
		client.upload_to_bucket.assert_called_once()
