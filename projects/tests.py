from __future__ import annotations

import io
import os
import unittest
import uuid
import zipfile
from io import StringIO
from unittest.mock import patch
from urllib import error

from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings, tag
from django.urls import reverse

from people.models import Profile

from .forms import ContributionFormSet, ProjectForm
from .models import (
    ArtifactLink,
    Contribution,
    Project,
    ProjectDeposit,
    Tag,
    TagAssignment,
)
from .templatetags.osprey_md import render_markdown
from .zenodo import (
    ProjectArchive,
    ZenodoClient,
    ZenodoError,
    build_project_archive,
    metadata_for_project,
    publish_project_deposit,
    sync_project_to_zenodo,
)


def orcid_extra(orcid_id: str = "0000-0001-2345-6789") -> dict:
    return {
        "orcid-identifier": {"path": orcid_id},
        "person": {
            "name": {
                "given-names": {"value": "Alice"},
                "family-name": {"value": "Researcher"},
                "credit-name": {"value": "Alice Researcher"},
            }
        },
    }


def add_orcid_account(user, orcid_id: str = "0000-0001-2345-6789") -> SocialAccount:
    return SocialAccount.objects.create(
        user=user,
        provider="orcid",
        uid=orcid_id,
        extra_data=orcid_extra(orcid_id),
    )


class ProjectTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner")
        self.contributor = User.objects.create_user(username="contributor")
        self.staff = User.objects.create_user(username="staff", is_staff=True)
        self.unrelated = User.objects.create_user(username="unrelated")
        self.public_project = Project.objects.create(
            slug="public-pump",
            title="Public Pump",
            summary="A public pump controller.",
            readme="# Public Pump\n\nBench notes.",
            field="Oceanography",
            artifact_type="Hardware",
            license="MIT",
            canonical_url="https://github.com/example/public-pump",
            institution="WHOI",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.owner,
        )
        self.private_project = Project.objects.create(
            slug="private-pump",
            title="Private Pump",
            summary="A private pump controller.",
            field="Oceanography",
            artifact_type="Hardware",
            institution="WHOI",
            visibility=Project.VISIBILITY_PRIVATE,
            created_by=self.owner,
        )
        Contribution.objects.create(
            project=self.private_project,
            user=self.contributor,
            display_name="Contributor Person",
            role="Maintainer",
            order=0,
        )

    def project_form_post_data(self, *, action: str = "draft") -> dict:
        return {
            "title": "Submitted Pump",
            "summary": "A newly submitted pump.",
            "readme": "# Submitted Pump",
            "field": "Oceanography",
            "artifact_type": "Hardware",
            "canonical_url": "https://github.com/example/submitted-pump",
            "cover_image_url": "",
            "cover_image_focal_x": "50",
            "cover_image_focal_y": "50",
            "cover_image_zoom": "1",
            "institution": "WHOI",
            "license_choice": "MIT",
            "license_custom": "",
            "tags_input": "pump, controller",
            "contributions-TOTAL_FORMS": "1",
            "contributions-INITIAL_FORMS": "0",
            "contributions-MIN_NUM_FORMS": "1",
            "contributions-MAX_NUM_FORMS": "1000",
            "contributions-0-display_name": "Owner Person",
            "contributions-0-role": "Project lead",
            "contributions-0-orcid_id": "",
            "contributions-0-credit_statement": "Built the prototype.",
            "contributions-0-order": "0",
            "action": action,
        }


class ProjectModelTests(ProjectTestCase):
    def test_visibility_and_editing_rules(self):
        anonymous = AnonymousUser()

        self.assertTrue(self.public_project.viewable_by(anonymous))
        self.assertFalse(self.private_project.viewable_by(anonymous))
        self.assertTrue(self.private_project.viewable_by(self.owner))
        self.assertTrue(self.private_project.viewable_by(self.contributor))
        self.assertTrue(self.private_project.viewable_by(self.staff))
        self.assertFalse(self.private_project.viewable_by(self.unrelated))
        self.assertTrue(self.private_project.editable_by(self.owner))
        self.assertTrue(self.private_project.editable_by(self.contributor))
        self.assertFalse(self.private_project.editable_by(self.unrelated))

    def test_doi_and_deposit_urls(self):
        self.public_project.doi = "https://doi.org/10.5281/zenodo.456"
        self.assertEqual(
            self.public_project.doi_url, "https://doi.org/10.5281/zenodo.456"
        )
        self.assertTrue(self.public_project.is_zenodo_doi)
        self.assertEqual(
            self.public_project.zenodo_badge_url,
            "https://zenodo.org/badge/DOI/10.5281/zenodo.456.svg",
        )

        self.public_project.doi = "10.5072/zenodo.123"
        self.assertEqual(
            self.public_project.zenodo_badge_url,
            "https://sandbox.zenodo.org/badge/DOI/10.5072/zenodo.123.svg",
        )
        deposit = ProjectDeposit.objects.create(
            project=self.public_project,
            deposition_id="draft-1",
            record_id="record-1",
            sandbox=True,
        )
        self.assertEqual(
            deposit.external_url, "https://sandbox.zenodo.org/records/record-1"
        )

    def test_contribution_orcid_is_verified_against_social_account(self):
        add_orcid_account(self.owner, "0000-0001-2345-6789")
        contribution = Contribution.objects.create(
            project=self.public_project,
            user=self.owner,
            orcid_id="0000-0001-2345-6789",
            display_name="Owner Person",
            role="Project lead",
        )

        self.assertEqual(contribution.verified_orcid_id, "0000-0001-2345-6789")
        contribution.orcid_id = "0000-0001-9999-9999"
        self.assertEqual(contribution.verified_orcid_id, "")

    def test_contribution_save_attaches_existing_profile_orcid(self):
        Profile.objects.filter(user=self.owner).update(
            orcid_placeholder="0000-0001-2345-6789"
        )

        contribution = Contribution.objects.create(
            project=self.public_project,
            orcid_id="0000-0001-2345-6789",
            display_name="Owner Person",
            role="Project lead",
        )

        self.assertEqual(contribution.user, self.owner)


class ProjectFormTests(ProjectTestCase):
    def test_project_form_saves_license_tags_and_slug(self):
        form = ProjectForm(
            data={
                "title": "Field Test Pump",
                "summary": "A field test pump.",
                "readme": "# Notes",
                "field": "Oceanography",
                "artifact_type": "Hardware",
                "canonical_url": "https://github.com/example/field-test-pump",
                "cover_image_url": "",
                "cover_image_focal_x": "50",
                "cover_image_focal_y": "50",
                "cover_image_zoom": "1",
                "institution": "WHOI",
                "license_choice": "__other__",
                "license_custom": "Custom-OHL-1.0",
                "tags_input": "Pump, pump, CO2",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        with patch("projects.forms.secrets.token_hex", return_value="abcd"):
            project = form.save()
        self.assertEqual(project.license, "Custom-OHL-1.0")
        self.assertEqual(project.slug, "field-test-pump-abcd")
        self.assertEqual(
            list(project.tags.values_list("name", flat=True)), ["co2", "pump"]
        )

    def test_recommended_licenses_lead_with_agpl(self):
        from projects.forms import RECOMMENDED_LICENSES

        ids = [code for code, _ in RECOMMENDED_LICENSES]
        self.assertEqual(ids[0], "AGPL-3.0")
        self.assertNotIn("GPL-3.0-or-later", ids)

    def test_contribution_formset_requires_at_least_one_contributor(self):
        formset = ContributionFormSet(
            data={
                "contributions-TOTAL_FORMS": "0",
                "contributions-INITIAL_FORMS": "0",
                "contributions-MIN_NUM_FORMS": "1",
                "contributions-MAX_NUM_FORMS": "1000",
            },
            instance=self.public_project,
        )

        self.assertFalse(formset.is_valid())


class ProjectViewTests(ProjectTestCase):
    def test_list_and_detail_respect_visibility_and_filters(self):
        tag = Tag.objects.create(name="pump")
        TagAssignment.objects.create(project=self.public_project, tag=tag)

        response = self.client.get(reverse("projects:list"), {"tag": "pump"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Public Pump")
        self.assertNotContains(response, "Private Pump")

        response = self.client.get(
            reverse("projects:detail", args=[self.private_project.slug])
        )
        self.assertEqual(response.status_code, 404)

        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("projects:detail", args=[self.private_project.slug])
        )
        self.assertEqual(response.status_code, 200)

    def test_list_filters_by_search_field_type_and_institution(self):
        Project.objects.create(
            slug="software-float",
            title="Float Analysis",
            summary="Analysis scripts for floats.",
            field="Atmospheric science",
            artifact_type="Software",
            institution="Another Lab",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.owner,
        )

        response = self.client.get(reverse("projects:list"), {"q": "Float"})
        self.assertContains(response, "Float Analysis")
        self.assertNotContains(response, "Public Pump")

        response = self.client.get(reverse("projects:list"), {"field": "Oceanography"})
        self.assertContains(response, "Public Pump")
        self.assertNotContains(response, "Float Analysis")

        response = self.client.get(
            reverse("projects:list"), {"project_type": "Software"}
        )
        self.assertContains(response, "Float Analysis")
        self.assertNotContains(response, "Public Pump")

        response = self.client.get(reverse("projects:list"), {"institution": "WHOI"})
        self.assertContains(response, "Public Pump")
        self.assertNotContains(response, "Float Analysis")

    def test_new_get_prefills_initial_contributor(self):
        self.owner.profile.display_name = "Owner Person"
        self.owner.profile.save()
        self.client.force_login(self.owner)

        response = self.client.get(reverse("projects:new"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Owner Person")

    def test_unverified_user_cannot_publish_new_project(self):
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("projects:new"), self.project_form_post_data(action="publish")
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sign in with ORCID before publishing a project.")
        self.assertFalse(
            Project.objects.filter(
                title="Submitted Pump", visibility=Project.VISIBILITY_PUBLIC
            ).exists()
        )

    @patch("projects.views.publish_project_now")
    def test_verified_user_can_publish_and_gets_verified_submitter_row(
        self, _publish_mock
    ):
        add_orcid_account(self.owner, "0000-0001-2345-6789")
        Profile.objects.filter(user=self.owner).update(display_name="Owner Person")
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("projects:new"), self.project_form_post_data(action="publish")
        )

        project = Project.objects.get(title="Submitted Pump")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(project.visibility, Project.VISIBILITY_PUBLIC)
        contribution = project.contributions.get(user=self.owner)
        self.assertEqual(contribution.orcid_id, "0000-0001-2345-6789")

    @patch("projects.views.publish_project_now")
    def test_edit_can_publish_draft_with_verified_orcid(self, _publish_mock):
        add_orcid_account(self.owner, "0000-0001-2345-6789")
        self.client.force_login(self.owner)
        data = self.project_form_post_data(action="publish")
        data["title"] = "Private Pump Updated"
        data["contributions-INITIAL_FORMS"] = "1"
        data["contributions-0-id"] = str(self.private_project.contributions.first().pk)

        response = self.client.post(
            reverse("projects:edit", args=[self.private_project.slug]), data
        )

        self.private_project.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.private_project.visibility, Project.VISIBILITY_PUBLIC)
        self.assertEqual(self.private_project.title, "Private Pump Updated")

    def test_public_project_stays_public_on_save(self):
        self.client.force_login(self.owner)
        Contribution.objects.create(
            project=self.public_project,
            display_name="Owner Person",
            role="Project lead",
        )
        data = self.project_form_post_data(action="save")
        data["title"] = "Still Public"
        data["contributions-INITIAL_FORMS"] = "1"
        data["contributions-0-id"] = str(self.public_project.contributions.first().pk)

        response = self.client.post(
            reverse("projects:edit", args=[self.public_project.slug]), data
        )

        self.public_project.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.public_project.visibility, Project.VISIBILITY_PUBLIC)
        self.assertEqual(self.public_project.title, "Still Public")

    @patch("projects.views.publish_project_now")
    def test_publish_action_on_new_form_calls_publish(self, publish_mock):
        add_orcid_account(self.owner)
        self.client.force_login(self.owner)
        data = self.project_form_post_data(action="publish")
        data["title"] = "New Published Project"

        response = self.client.post(reverse("projects:new"), data)

        self.assertEqual(response.status_code, 302)
        publish_mock.assert_called_once()
        project = Project.objects.get(title="New Published Project")
        self.assertEqual(project.visibility, Project.VISIBILITY_PUBLIC)


class MarkdownTemplateTagTests(TestCase):
    def test_markdown_sanitizes_html_and_keeps_safe_links(self):
        rendered = str(
            render_markdown("[link](https://example.org)\n\n<script>alert(1)</script>")
        )

        self.assertIn('href="https://example.org"', rendered)
        self.assertIn('rel="noopener"', rendered)
        self.assertNotIn("<script>", rendered)


class ZenodoServiceTests(ProjectTestCase):
    def test_metadata_for_project_matches_zenodo_shape(self):
        Contribution.objects.create(
            project=self.public_project,
            display_name="Alice Researcher",
            role="Project lead",
            order=0,
        )

        metadata = metadata_for_project(self.public_project)

        self.assertEqual(metadata["title"], "Public Pump")
        self.assertEqual(metadata["upload_type"], "physicalobject")
        self.assertEqual(metadata["access_right"], "open")
        self.assertEqual(metadata["license"], "mit-license")
        self.assertIn({"name": "Alice Researcher"}, metadata["creators"])
        self.assertIn("Oceanography", metadata["keywords"])

    def test_project_archive_contains_snapshot_files(self):
        archive = build_project_archive(self.public_project)

        self.assertEqual(archive.filename, "osprey-public-pump-snapshot.zip")
        self.assertEqual(archive.content_type, "application/octet-stream")
        with zipfile.ZipFile(io.BytesIO(archive.content)) as archive_zip:
            self.assertEqual(
                set(archive_zip.namelist()),
                {"README.md", "osprey-project.json", "CITATION.cff"},
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

        deposit = sync_project_to_zenodo(self.public_project, self.owner)

        self.assertEqual(deposit.state, ProjectDeposit.STATE_DRAFT)
        self.assertEqual(deposit.deposition_id, "123")
        self.assertEqual(deposit.doi, "10.5072/zenodo.123")
        self.public_project.refresh_from_db()
        self.assertEqual(self.public_project.doi, "10.5072/zenodo.123")
        client.update_deposition_metadata.assert_called_once()
        # Two sidecar uploads: osprey-project.json and CITATION.cff.
        self.assertEqual(client.upload_to_bucket.call_count, 2)

    @override_settings(
        ZENODO_USE_SANDBOX=True,
        ZENODO_ACCESS_TOKEN="fake-token",
        ZENODO_API_BASE_URL="https://sandbox.zenodo.org",
    )
    @patch("projects.zenodo.ZenodoClient")
    def test_sync_error_sets_error_state(self, client_class):
        client = client_class.from_settings.return_value
        client.create_deposition.return_value = {
            "id": 123,
            "links": {"bucket": "https://sandbox.zenodo.org/api/files/bucket"},
            "metadata": {},
        }
        client.update_deposition_metadata.return_value = {
            "id": 123,
            "links": {"bucket": "https://sandbox.zenodo.org/api/files/bucket"},
            "metadata": {},
        }
        client.upload_to_bucket.side_effect = ZenodoError("upload failed")

        with self.assertRaises(ZenodoError):
            sync_project_to_zenodo(self.public_project, self.owner)

        deposit = ProjectDeposit.objects.get(project=self.public_project)
        self.assertEqual(deposit.state, ProjectDeposit.STATE_ERROR)
        self.assertIn("upload failed", deposit.last_error)

    @override_settings(
        ZENODO_USE_SANDBOX=True,
        ZENODO_ACCESS_TOKEN="fake-token",
        ZENODO_API_BASE_URL="https://sandbox.zenodo.org",
    )
    @patch("projects.zenodo.ZenodoClient")
    def test_publish_project_deposit_updates_project_and_artifact_link(
        self, client_class
    ):
        client = client_class.from_settings.return_value
        client.publish_deposition.return_value = {
            "id": 123,
            "record_id": 456,
            "metadata": {"doi": "10.5072/zenodo.456"},
        }
        deposit = ProjectDeposit.objects.create(
            project=self.public_project,
            deposition_id="123",
            state=ProjectDeposit.STATE_DRAFT,
            sandbox=True,
        )

        published = publish_project_deposit(deposit)

        self.assertEqual(published.state, ProjectDeposit.STATE_PUBLISHED)
        self.assertEqual(published.record_id, "456")
        self.public_project.refresh_from_db()
        self.assertEqual(self.public_project.doi, "10.5072/zenodo.456")
        link = ArtifactLink.objects.get(project=self.public_project, kind="zenodo")
        self.assertEqual(link.url, "https://sandbox.zenodo.org/records/456")


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.body


class ZenodoClientTests(TestCase):
    def test_client_requires_access_token(self):
        with self.assertRaises(ZenodoError):
            ZenodoClient("https://sandbox.zenodo.org", "")

    @patch("projects.zenodo.request.urlopen")
    def test_client_sends_json_and_parses_response(self, urlopen_mock):
        urlopen_mock.return_value = _FakeResponse(201, b'{"id": 123}')
        client = ZenodoClient("https://sandbox.zenodo.org", "token")

        response = client.create_deposition()

        self.assertEqual(response, {"id": 123})
        request_obj = urlopen_mock.call_args.args[0]
        self.assertEqual(request_obj.get_method(), "POST")
        self.assertEqual(request_obj.headers["Authorization"], "Bearer token")

    @patch("projects.zenodo.request.urlopen")
    def test_client_raises_readable_http_and_url_errors(self, urlopen_mock):
        client = ZenodoClient("https://sandbox.zenodo.org", "token")
        urlopen_mock.side_effect = error.HTTPError(
            "https://sandbox.zenodo.org/api/deposit/depositions",
            415,
            "Unsupported Media Type",
            {},
            io.BytesIO(b'{"message":"bad content type"}'),
        )
        with self.assertRaisesRegex(ZenodoError, "HTTP 415"):
            client.create_deposition()

        urlopen_mock.side_effect = error.URLError("offline")
        with self.assertRaisesRegex(ZenodoError, "Could not reach Zenodo"):
            client.create_deposition()

    @patch("projects.zenodo.request.urlopen")
    def test_bucket_upload_quotes_filename_and_uses_archive_content_type(
        self, urlopen_mock
    ):
        urlopen_mock.return_value = _FakeResponse(200, b'{"ok": true}')
        client = ZenodoClient("https://sandbox.zenodo.org", "token")
        archive = ProjectArchive(
            filename="space name.zip",
            content=b"zip",
            content_type="application/octet-stream",
        )

        response = client.upload_to_bucket(
            "https://sandbox.zenodo.org/api/files/bucket", archive
        )

        self.assertEqual(response, {"ok": True})
        request_obj = urlopen_mock.call_args.args[0]
        self.assertEqual(
            request_obj.full_url,
            "https://sandbox.zenodo.org/api/files/bucket/space%20name.zip",
        )
        self.assertEqual(
            request_obj.headers["Content-type"], "application/octet-stream"
        )


class ZenodoSmokeCommandTests(TestCase):
    @override_settings(ZENODO_USE_SANDBOX=False, ZENODO_ACCESS_TOKEN="fake-token")
    def test_smoke_command_refuses_production_mode(self):
        with self.assertRaises(CommandError):
            call_command("zenodo_sandbox_smoke")

    @override_settings(ZENODO_USE_SANDBOX=True, ZENODO_ACCESS_TOKEN="")
    def test_smoke_command_requires_token(self):
        with self.assertRaises(CommandError):
            call_command("zenodo_sandbox_smoke")

    @override_settings(ZENODO_USE_SANDBOX=True, ZENODO_ACCESS_TOKEN="fake-token")
    @patch("projects.management.commands.zenodo_sandbox_smoke.publish_project_deposit")
    @patch("projects.management.commands.zenodo_sandbox_smoke.sync_project_to_zenodo")
    def test_smoke_command_sync_and_publish_success_paths(
        self, sync_mock, publish_mock
    ):
        User = get_user_model()
        user = User.objects.create_user(username="owner")
        project = Project.objects.create(
            slug="command-pump",
            title="Command Pump",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=user,
        )
        draft = ProjectDeposit.objects.create(
            project=project,
            deposition_id="123",
            doi="10.5072/zenodo.123",
            state=ProjectDeposit.STATE_DRAFT,
            sandbox=True,
        )
        published = ProjectDeposit(
            project=project,
            deposition_id="123",
            doi="10.5072/zenodo.123",
            state=ProjectDeposit.STATE_PUBLISHED,
            record_id="123",
            sandbox=True,
        )
        sync_mock.return_value = draft
        publish_mock.return_value = published
        output = StringIO()

        call_command("zenodo_sandbox_smoke", "command-pump", "--publish", stdout=output)

        self.assertIn("Synced Zenodo sandbox draft", output.getvalue())
        self.assertIn("Published Zenodo sandbox record", output.getvalue())
        sync_mock.assert_called_once_with(project, user)
        publish_mock.assert_called_once_with(draft)


@tag("live", "zenodo")
@unittest.skipUnless(
    os.environ.get("RUN_LIVE_ZENODO_TESTS") == "1", "live Zenodo tests are opt-in"
)
@override_settings(
    ZENODO_USE_SANDBOX=True, ZENODO_API_BASE_URL="https://sandbox.zenodo.org"
)
class LiveZenodoSandboxTests(TestCase):
    def setUp(self):
        if not os.environ.get("ZENODO_ACCESS_TOKEN"):
            self.skipTest("ZENODO_ACCESS_TOKEN is required for live Zenodo tests")
        User = get_user_model()
        self.user = User.objects.create_user(username="live-zenodo-user")
        self.project = Project.objects.create(
            slug=f"live-zenodo-{uuid.uuid4().hex[:8]}",
            title="Live Zenodo Sandbox Test",
            summary="Disposable sandbox test project.",
            readme="# Live Zenodo Sandbox Test",
            artifact_type="Software",
            field="Oceanography",
            license="MIT",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.user,
        )
        Contribution.objects.create(
            project=self.project,
            display_name="Live Tester",
            role="Project lead",
        )

    def test_live_sync_creates_sandbox_deposit(self):
        deposit = sync_project_to_zenodo(self.project, self.user)

        self.assertEqual(deposit.state, ProjectDeposit.STATE_DRAFT)
        self.assertTrue(deposit.deposition_id)
        self.assertTrue(deposit.external_url)

    @unittest.skipUnless(
        os.environ.get("RUN_LIVE_ZENODO_PUBLISH") == "1",
        "live publish is separately opt-in",
    )
    def test_live_publish_creates_sandbox_record(self):
        deposit = sync_project_to_zenodo(self.project, self.user)
        published = publish_project_deposit(deposit)

        self.assertEqual(published.state, ProjectDeposit.STATE_PUBLISHED)
        self.assertTrue(published.doi.startswith("10.5072/zenodo."))
        self.assertTrue(
            published.external_url.startswith("https://sandbox.zenodo.org/records/")
        )


class ProjectVersionsTests(ProjectTestCase):
    """New-version flow, project permalink, and versions page."""

    def _published_deposit(
        self, *, concept_doi: str = "10.5072/zenodo.concept"
    ) -> ProjectDeposit:
        return ProjectDeposit.objects.create(
            project=self.public_project,
            provider=ProjectDeposit.PROVIDER_ZENODO,
            sandbox=True,
            deposition_id="123",
            record_id="456",
            doi="10.5072/zenodo.456",
            concept_doi=concept_doi,
            state=ProjectDeposit.STATE_PUBLISHED,
        )

    def test_project_has_public_id_uuid(self):
        self.assertIsInstance(self.public_project.public_id, uuid.UUID)
        # Idempotent on re-save
        original = self.public_project.public_id
        self.public_project.save()
        self.public_project.refresh_from_db()
        self.assertEqual(self.public_project.public_id, original)

    def test_project_permalink_redirects_to_slug_url(self):
        url = reverse("project_permalink", args=[str(self.public_project.public_id)])

        response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], self.public_project.get_absolute_url())

    def test_metadata_includes_osprey_permalink_and_canonical_url(self):
        deposit = self._published_deposit()

        metadata = metadata_for_project(self.public_project, deposit)

        identifiers = metadata.get("related_identifiers", [])
        urls = [row["identifier"] for row in identifiers]
        self.assertTrue(
            any(str(self.public_project.public_id) in u for u in urls),
            f"permalink not in {urls}",
        )
        self.assertIn(self.public_project.canonical_url, urls)

    def test_metadata_includes_version_notes_when_pending_changelog_set(self):
        deposit = self._published_deposit()
        deposit.pending_changelog = "Fixed bench wiring."
        deposit.repo_link = "https://github.com/example/public-pump/releases/tag/v0.2"
        deposit.save()

        metadata = metadata_for_project(self.public_project, deposit)

        self.assertEqual(metadata["notes"], "Fixed bench wiring.")
        self.assertEqual(metadata["version"], "v1")
        urls = [row["identifier"] for row in metadata["related_identifiers"]]
        self.assertIn(deposit.repo_link, urls)

    @override_settings(
        ZENODO_USE_SANDBOX=True,
        ZENODO_ACCESS_TOKEN="fake-token",
        ZENODO_API_BASE_URL="https://sandbox.zenodo.org",
    )
    @patch("projects.zenodo.ZenodoClient")
    def test_start_new_version_requires_published_deposit(self, client_class):
        from .zenodo import start_new_version_for_deposit

        deposit = ProjectDeposit.objects.create(
            project=self.public_project,
            deposition_id="123",
            state=ProjectDeposit.STATE_DRAFT,
        )

        with self.assertRaises(ZenodoError):
            start_new_version_for_deposit(deposit, changelog="x")

    @override_settings(
        ZENODO_USE_SANDBOX=True,
        ZENODO_ACCESS_TOKEN="fake-token",
        ZENODO_API_BASE_URL="https://sandbox.zenodo.org",
    )
    @patch("projects.zenodo.ZenodoClient")
    def test_start_new_version_requires_changelog(self, client_class):
        from .zenodo import start_new_version_for_deposit

        deposit = self._published_deposit()

        with self.assertRaises(ZenodoError):
            start_new_version_for_deposit(deposit, changelog="   ")

    @override_settings(
        ZENODO_USE_SANDBOX=True,
        ZENODO_ACCESS_TOKEN="fake-token",
        ZENODO_API_BASE_URL="https://sandbox.zenodo.org",
    )
    @patch("projects.zenodo.ZenodoClient")
    def test_start_new_version_creates_draft_and_uploads_snapshot(self, client_class):
        from .zenodo import start_new_version_for_deposit

        client = client_class.from_settings.return_value
        client.create_new_version.return_value = {
            "id": 999,
            "links": {"bucket": "https://sandbox.zenodo.org/api/files/bucket-v2"},
            "metadata": {"prereserve_doi": {"doi": "10.5072/zenodo.999"}},
        }
        client.update_deposition_metadata.return_value = {
            "id": 999,
            "links": {"bucket": "https://sandbox.zenodo.org/api/files/bucket-v2"},
            "metadata": {"prereserve_doi": {"doi": "10.5072/zenodo.999"}},
        }
        client.upload_to_bucket.return_value = {"ok": True}
        deposit = self._published_deposit()

        result = start_new_version_for_deposit(
            deposit,
            changelog="New bench data.",
            repo_link="https://github.com/example/public-pump/releases/tag/v0.2",
            user=self.owner,
        )

        self.assertEqual(result.state, ProjectDeposit.STATE_DRAFT)
        self.assertEqual(result.deposition_id, "999")
        self.assertEqual(result.pending_changelog, "New bench data.")
        self.assertEqual(
            result.repo_link,
            "https://github.com/example/public-pump/releases/tag/v0.2",
        )
        client.create_new_version.assert_called_once_with("123")
        client.update_deposition_metadata.assert_called_once()
        # Two sidecar uploads: osprey-project.json and CITATION.cff.
        self.assertEqual(client.upload_to_bucket.call_count, 2)

    @override_settings(
        ZENODO_USE_SANDBOX=True,
        ZENODO_ACCESS_TOKEN="fake-token",
        ZENODO_API_BASE_URL="https://sandbox.zenodo.org",
    )
    @patch("projects.zenodo.ZenodoClient")
    def test_publish_records_version_and_clears_pending_fields(self, client_class):
        client = client_class.from_settings.return_value
        client.publish_deposition.return_value = {
            "id": 999,
            "record_id": 1000,
            "metadata": {
                "doi": "10.5072/zenodo.1000",
                "conceptdoi": "10.5072/zenodo.concept",
            },
        }
        deposit = ProjectDeposit.objects.create(
            project=self.public_project,
            deposition_id="999",
            state=ProjectDeposit.STATE_DRAFT,
            sandbox=True,
            pending_changelog="Fixed wiring.",
            repo_link="https://github.com/example/public-pump/releases/tag/v0.2",
        )

        published = publish_project_deposit(deposit)

        self.assertEqual(published.state, ProjectDeposit.STATE_PUBLISHED)
        self.assertEqual(published.pending_changelog, "")
        self.assertEqual(published.repo_link, "")
        version = published.versions.get(version_index=1)
        self.assertEqual(version.changelog, "Fixed wiring.")
        self.assertEqual(version.doi, "10.5072/zenodo.1000")
        self.assertEqual(
            version.repo_link,
            "https://github.com/example/public-pump/releases/tag/v0.2",
        )
        # Project DOI tracks the concept (project) DOI.
        self.public_project.refresh_from_db()
        self.assertEqual(self.public_project.doi, "10.5072/zenodo.concept")

    def test_versions_page_lists_published_versions(self):
        deposit = self._published_deposit()
        from .models import ProjectDepositVersion
        from django.utils import timezone

        ProjectDepositVersion.objects.create(
            deposit=deposit,
            version_index=1,
            deposition_id="123",
            record_id="456",
            doi="10.5072/zenodo.456",
            changelog="First public release.",
            published_at=timezone.now(),
        )

        url = reverse("projects:versions", args=[self.public_project.slug])
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v1")
        self.assertContains(response, "First public release.")
        self.assertContains(response, "10.5072/zenodo.456")

    def test_new_version_view_blocks_when_no_published_deposit(self):
        add_orcid_account(self.owner)
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("projects:zenodo_new_version", args=[self.public_project.slug])
        )

        # No published deposit -> redirect with error message back to project page.
        self.assertEqual(response.status_code, 302)

    def test_new_version_view_rejects_non_editor(self):
        self._published_deposit()
        self.client.force_login(self.unrelated)

        response = self.client.get(
            reverse("projects:zenodo_new_version", args=[self.public_project.slug])
        )

        self.assertEqual(response.status_code, 404)

    @patch("projects.views.publish_new_version_now")
    def test_new_version_view_post_calls_service(self, service_mock):
        deposit = self._published_deposit()
        service_mock.return_value = deposit
        add_orcid_account(self.owner)
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("projects:zenodo_new_version", args=[self.public_project.slug]),
            {
                "action": "publish",
                "changelog": "Cleaned up wiring.",
                "repo_link": "https://github.com/example/public-pump/releases/tag/v0.2",
                "archive": SimpleUploadedFile(
                    "panda-v2.zip", b"PK\x03\x04stub", content_type="application/zip"
                ),
            },
        )

        self.assertEqual(response.status_code, 302)
        service_mock.assert_called_once()
        kwargs = service_mock.call_args.kwargs
        self.assertEqual(kwargs["changelog"], "Cleaned up wiring.")
        self.assertEqual(
            kwargs["repo_link"],
            "https://github.com/example/public-pump/releases/tag/v0.2",
        )

    @patch("projects.views.publish_new_version_now")
    def test_new_version_view_requires_changelog(self, service_mock):
        self._published_deposit()
        add_orcid_account(self.owner)
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("projects:zenodo_new_version", args=[self.public_project.slug]),
            {
                "action": "publish",
                "changelog": "   ",
                "archive": SimpleUploadedFile(
                    "panda-v2.zip", b"PK\x03\x04stub", content_type="application/zip"
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        service_mock.assert_not_called()
        self.assertContains(response, "field is required")


class NewProjectFieldsTests(ProjectTestCase):
    """Smoke tests for the new project fields and the contributor ORCID input."""

    @patch("projects.views.publish_project_now")
    def test_form_round_trip_persists_funding_publications_self_rating_and_orcid(
        self, _publish_mock
    ):
        add_orcid_account(self.owner, "0000-0001-2345-6789")
        self.client.force_login(self.owner)

        data = self.project_form_post_data(action="publish")
        data["title"] = "Funded Pump"
        data["funding"] = "NSF OCE-1234567 (PI: Smith)\nWHOI seed grant"
        data["publications"] = "Smith et al., 2024. DOI:10.1234/abcd"
        data["self_rating"] = "7"
        data["institution"] = "WHOI\nMIT"
        data["contributions-0-orcid_id"] = "0000-0002-1825-0097"

        response = self.client.post(reverse("projects:new"), data)
        self.assertEqual(response.status_code, 302, response.content[:600])

        project = Project.objects.get(title="Funded Pump")
        self.assertEqual(project.self_rating, 7)
        self.assertIn("NSF OCE-1234567", project.funding)
        self.assertIn("DOI:10.1234/abcd", project.publications)
        self.assertEqual(project.institutions, ["WHOI", "MIT"])
        # Multiple contributors may exist; find the one we added by ORCID.
        contribution = project.contributions.filter(
            orcid_id="0000-0002-1825-0097"
        ).first()
        self.assertIsNotNone(contribution)

    def test_invalid_orcid_on_contribution_is_rejected(self):
        add_orcid_account(self.owner, "0000-0001-2345-6789")
        self.client.force_login(self.owner)

        data = self.project_form_post_data(action="publish")
        data["contributions-0-orcid_id"] = "not-an-orcid"

        response = self.client.post(reverse("projects:new"), data)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Project.objects.filter(title="Submitted Pump").exists())
