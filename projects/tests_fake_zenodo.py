"""Tests for the fake Zenodo, the endpoint guard, and the first real
flows driven end to end through the fake.

Three groups:

- `FakeZenodoServerTests`: the fake behaves like Zenodo where the code
  depends on it, and failure injection works.
- `ZenodoContractTests` / `LiveZenodoContractTests`: one scenario,
  asserted identically against the fake (always) and against the real
  sandbox (opt-in, `RUN_LIVE_ZENODO=1`, tag `live-zenodo`). Drift between
  the two shows up here.
- `ZenodoFlowsThroughFakeTests`: publish, metadata sync, new version, and
  outage behavior through the real `projects.zenodo` functions.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings, tag

from projects import checks
from projects.models import (
    Contribution,
    Project,
    ProjectAttachment,
    ProjectDeposit,
    ProjectDepositVersion,
)
from projects.testing.fake_zenodo import (
    DOI_PREFIX,
    STATE_DONE,
    STATE_INPROGRESS,
    STATE_UNSUBMITTED,
    FakeZenodoMixin,
)
from projects.zenodo import (
    ProjectArchive,
    ZenodoClient,
    ZenodoError,
    publish_new_version_now,
    publish_project_now,
    sync_project_to_zenodo,
    update_published_metadata,
)

VALID_METADATA = {
    "title": "Fake Pump",
    "upload_type": "software",
    "description": "<p>A pump.</p>",
    "creators": [{"name": "Tester, Test"}],
}


def _zip_bytes(name: str = "README.md", text: str = "# hi") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


class FakeZenodoServerTests(FakeZenodoMixin, TestCase):
    def client_for(self, timeout: int = 5) -> ZenodoClient:
        return ZenodoClient(self.fz.url, self.fz.token, timeout=timeout)

    def test_create_update_upload_publish_roundtrip(self):
        client = self.client_for()
        created = client.create_deposition()
        self.assertEqual(created["state"], STATE_UNSUBMITTED)
        self.assertTrue(created["links"]["bucket"].startswith(self.fz.url))
        self.assertEqual(created["metadata"]["prereserve_doi"]["doi"], f"{DOI_PREFIX}{created['id']}")
        self.assertIn("conceptrecid", created)
        self.assertNotIn("conceptdoi", created)  # sandbox quirk: only after publish

        updated = client.update_deposition_metadata(created["id"], VALID_METADATA)
        self.assertEqual(updated["metadata"]["title"], "Fake Pump")
        client.upload_to_bucket(created["links"]["bucket"], ProjectArchive("a.zip", _zip_bytes()))

        published = client.publish_deposition(created["id"])
        self.assertEqual(published["doi"], f"{DOI_PREFIX}{created['id']}")
        self.assertEqual(published["conceptdoi"], f"{DOI_PREFIX}{created['conceptrecid']}")
        self.assertEqual(published["record_id"], created["id"])
        self.assertEqual(self.fz.deposition(created["id"]).state, STATE_DONE)
        self.assertEqual([f["filename"] for f in published["files"]], ["a.zip"])

    def test_publish_requires_a_file_and_the_required_metadata(self):
        client = self.client_for()
        dep = client.create_deposition()
        with self.assertRaises(ZenodoError) as caught:
            client.publish_deposition(dep["id"])
        self.assertIn("HTTP 400", str(caught.exception))
        self.assertIn("Missing uploaded files", str(caught.exception))

        client.upload_to_bucket(dep["links"]["bucket"], ProjectArchive("a.zip", _zip_bytes()))
        client.update_deposition_metadata(dep["id"], {"title": "Only a title"})
        with self.assertRaises(ZenodoError) as caught:
            client.publish_deposition(dep["id"])
        self.assertIn("metadata.creators", str(caught.exception))
        self.assertEqual(self.fz.deposition(dep["id"]).state, STATE_UNSUBMITTED)

    def test_wrong_token_is_401(self):
        client = ZenodoClient(self.fz.url, "not-the-token", timeout=5)
        with self.assertRaises(ZenodoError) as caught:
            client.create_deposition()
        self.assertIn("HTTP 401", str(caught.exception))

    def test_streamed_upload_reads_exactly_content_length(self):
        client = self.client_for()
        dep = client.create_deposition()
        payload = b"x" * (3 * 1024 * 1024 + 17)
        client.stream_to_bucket(dep["links"]["bucket"], "big file.zip", io.BytesIO(payload), len(payload))
        files = client.list_deposition_files(dep["id"])
        self.assertEqual(files[0]["filename"], "big file.zip")
        self.assertEqual(files[0]["filesize"], len(payload))

    def _published(self, client: ZenodoClient) -> dict:
        dep = client.create_deposition()
        client.update_deposition_metadata(dep["id"], VALID_METADATA)
        client.upload_to_bucket(dep["links"]["bucket"], ProjectArchive("v1.zip", _zip_bytes()))
        return client.publish_deposition(dep["id"])

    def test_new_version_copies_files_and_keeps_the_concept(self):
        client = self.client_for()
        v1 = self._published(client)
        draft = client.create_new_version(v1["id"])  # follows links.latest_draft
        self.assertNotEqual(draft["id"], v1["id"])
        self.assertEqual(draft["conceptrecid"], v1["conceptrecid"])
        self.assertEqual(draft["conceptdoi"], v1["conceptdoi"])
        self.assertEqual(draft["state"], STATE_UNSUBMITTED)
        self.assertEqual([f["filename"] for f in draft["files"]], ["v1.zip"])
        # OSPREY clears carried-over files before uploading the new archive.
        for entry in client.list_deposition_files(draft["id"]):
            client.delete_deposition_file(draft["id"], entry["id"])
        self.assertEqual(client.list_deposition_files(draft["id"]), [])
        client.upload_to_bucket(draft["links"]["bucket"], ProjectArchive("v2.zip", _zip_bytes()))
        v2 = client.publish_deposition(draft["id"])
        self.assertEqual(v2["conceptdoi"], v1["conceptdoi"])
        self.assertNotEqual(v2["doi"], v1["doi"])
        self.assertEqual(self.fz.deposition(v2["id"]).version_index, 2)

    def test_new_version_of_an_unpublished_draft_is_400(self):
        client = self.client_for()
        dep = client.create_deposition()
        with self.assertRaises(ZenodoError) as caught:
            client.create_new_version(dep["id"])
        self.assertIn("HTTP 400", str(caught.exception))

    def test_edit_then_discard_restores_metadata_and_publish_keeps_doi(self):
        client = self.client_for()
        v1 = self._published(client)
        client.edit_published_deposition(v1["id"])
        self.assertEqual(self.fz.deposition(v1["id"]).state, STATE_INPROGRESS)
        client.update_deposition_metadata(v1["id"], {**VALID_METADATA, "title": "Renamed"})
        client.discard_deposition_changes(v1["id"])
        dep = self.fz.deposition(v1["id"])
        self.assertEqual(dep.state, STATE_DONE)
        self.assertEqual(dep.metadata["title"], "Fake Pump")

        client.edit_published_deposition(v1["id"])
        client.update_deposition_metadata(v1["id"], {**VALID_METADATA, "title": "Renamed"})
        republished = client.publish_deposition(v1["id"])
        self.assertEqual(republished["doi"], v1["doi"])
        self.assertEqual(self.fz.deposition(v1["id"]).metadata["title"], "Renamed")

    def test_published_deposit_rejects_metadata_and_uploads_until_edited(self):
        client = self.client_for()
        v1 = self._published(client)
        with self.assertRaises(ZenodoError) as caught:
            client.update_deposition_metadata(v1["id"], VALID_METADATA)
        self.assertIn("HTTP 403", str(caught.exception))
        with self.assertRaises(ZenodoError) as caught:
            client.upload_to_bucket(v1["links"]["bucket"], ProjectArchive("late.zip", _zip_bytes()))
        self.assertIn("HTTP 403", str(caught.exception))

    def test_record_pages_exist_only_after_publish(self):
        client = self.client_for()
        dep = client.create_deposition()
        with self.assertRaises(ZenodoError):
            client._request("GET", f"/api/records/{dep['id']}")
        v1 = self._published(client)
        record = client._request("GET", f"/api/records/{v1['id']}")
        self.assertEqual(record["doi"], v1["doi"])

    # -- failure injection ------------------------------------------------

    def test_status_rule_fails_n_times_then_recovers(self):
        client = self.client_for()
        dep = client.create_deposition()
        client.update_deposition_metadata(dep["id"], VALID_METADATA)
        client.upload_to_bucket(dep["links"]["bucket"], ProjectArchive("a.zip", _zip_bytes()))
        self.fz.add_rule(match="actions/publish", mode="status", status=500, times=1)
        with self.assertRaises(ZenodoError) as caught:
            client.publish_deposition(dep["id"])
        self.assertIn("HTTP 500", str(caught.exception))
        self.assertEqual(self.fz.deposition(dep["id"]).state, STATE_UNSUBMITTED)
        published = client.publish_deposition(dep["id"])
        self.assertEqual(published["doi"], f"{DOI_PREFIX}{dep['id']}")

    def test_hang_rule_surfaces_as_a_timeout(self):
        client = self.client_for(timeout=1)
        self.fz.add_rule(match="/api/deposit/depositions", mode="hang", delay_s=2.5)
        with self.assertRaises(ZenodoError) as caught:
            client.create_deposition()
        self.assertIn("Could not reach Zenodo", str(caught.exception))
        self.assertIn("timed out", str(caught.exception))

    def test_drop_rule_surfaces_as_unreachable(self):
        client = self.client_for()
        self.fz.add_rule(match="/api/deposit/depositions", mode="drop")
        with self.assertRaises(ZenodoError) as caught:
            client.create_deposition()
        self.assertIn("Could not reach Zenodo", str(caught.exception))

    def test_garbage_rule_surfaces_as_non_json_or_html_error(self):
        client = self.client_for()
        # A proxy error page with a 5xx: reported as the HTTP status.
        self.fz.add_rule(match="/api/deposit/depositions", mode="garbage", status=502)
        with self.assertRaises(ZenodoError) as caught:
            client.create_deposition()
        self.assertIn("HTTP 502", str(caught.exception))
        # HTML with the status the client expected: reported as non-JSON.
        self.fz.add_rule(match="/api/deposit/depositions", mode="garbage", status=201)
        with self.assertRaises(ZenodoError) as caught:
            client.create_deposition()
        self.assertIn("non-JSON", str(caught.exception))

    def test_request_log_records_what_the_code_called(self):
        client = self.client_for()
        client.create_deposition()
        self.assertEqual(self.fz.paths("POST"), ["/api/deposit/depositions"])


# -- the contract, run against both backends ---------------------------------


class ZenodoContractScenario:
    """Same assertions for the fake and for real Zenodo.

    `publish` is False by default for the live run because publishing
    creates permanent sandbox records; set RUN_LIVE_ZENODO_PUBLISH=1 to
    include it there.
    """

    def run_scenario(self, client: ZenodoClient, *, publish: bool) -> None:
        created = client.create_deposition()
        self.assertIsInstance(created["id"], int)
        self.assertTrue(created["links"]["bucket"].startswith("http"))
        self.assertTrue(created["metadata"]["prereserve_doi"]["doi"].startswith("10.5072/zenodo."))
        self.assertTrue(created.get("conceptrecid"))

        updated = client.update_deposition_metadata(created["id"], VALID_METADATA)
        self.assertEqual(updated["metadata"]["title"], VALID_METADATA["title"])

        client.upload_to_bucket(created["links"]["bucket"], ProjectArchive("scenario.zip", _zip_bytes()))
        files = client.list_deposition_files(created["id"])
        self.assertEqual({f["filename"] for f in files}, {"scenario.zip"})
        self.assertTrue(all("id" in f for f in files))

        with self.assertRaises(ZenodoError) as caught:
            client.create_new_version(created["id"])  # not published yet
        self.assertIn("HTTP 4", str(caught.exception))

        if not publish:
            return
        published = client.publish_deposition(created["id"])
        self.assertTrue(published["doi"].startswith("10.5072/zenodo."))
        self.assertTrue(published.get("conceptdoi") or published.get("conceptrecid"))

        draft = client.create_new_version(created["id"])
        self.assertNotEqual(draft["id"], created["id"])
        self.assertEqual(str(draft["conceptrecid"]), str(created["conceptrecid"]))
        for entry in client.list_deposition_files(draft["id"]):
            client.delete_deposition_file(draft["id"], entry["id"])
        self.assertEqual(client.list_deposition_files(draft["id"]), [])

        client.edit_published_deposition(created["id"])
        client.discard_deposition_changes(created["id"])


class ZenodoContractTests(ZenodoContractScenario, FakeZenodoMixin, TestCase):
    def test_scenario_against_the_fake(self):
        self.run_scenario(ZenodoClient(self.fz.url, self.fz.token, timeout=5), publish=True)


@tag("live-zenodo")
class LiveZenodoContractTests(ZenodoContractScenario, TestCase):
    """Run by hand: RUN_LIVE_ZENODO=1 manage.py test --tag=live-zenodo."""

    def setUp(self):
        if os.environ.get("RUN_LIVE_ZENODO") != "1" or not os.environ.get("ZENODO_ACCESS_TOKEN"):
            self.skipTest("set RUN_LIVE_ZENODO=1 and ZENODO_ACCESS_TOKEN to hit the real sandbox")

    def test_scenario_against_the_sandbox(self):
        self.run_scenario(
            ZenodoClient.from_settings(),
            publish=os.environ.get("RUN_LIVE_ZENODO_PUBLISH") == "1",
        )


# -- the startup guard --------------------------------------------------------


class ZenodoEndpointGuardTests(TestCase):
    def _errors(self, **overrides):
        with override_settings(**overrides), mock.patch("osprey.test_runner.IS_TEST_RUN", False):
            return [e.id for e in checks.zenodo_endpoint_check(None)]

    def test_real_hosts_pass_when_flag_matches(self):
        self.assertEqual(self._errors(ZENODO_API_BASE_URL="https://sandbox.zenodo.org", ZENODO_USE_SANDBOX=True), [])
        self.assertEqual(self._errors(ZENODO_API_BASE_URL="https://zenodo.org/", ZENODO_USE_SANDBOX=False), [])

    def test_any_other_host_is_rejected_outside_a_test_run(self):
        self.assertEqual(self._errors(ZENODO_API_BASE_URL="http://127.0.0.1:9999", ZENODO_USE_SANDBOX=True), ["projects.E001"])
        self.assertEqual(self._errors(ZENODO_API_BASE_URL="https://zenodo.example.org", ZENODO_USE_SANDBOX=False), ["projects.E001"])

    def test_flag_and_host_must_agree(self):
        self.assertEqual(self._errors(ZENODO_API_BASE_URL="https://zenodo.org", ZENODO_USE_SANDBOX=True), ["projects.E002"])
        self.assertEqual(self._errors(ZENODO_API_BASE_URL="https://sandbox.zenodo.org", ZENODO_USE_SANDBOX=False), ["projects.E002"])

    def test_test_run_is_exempt(self):
        with override_settings(ZENODO_API_BASE_URL="http://127.0.0.1:9999"):
            self.assertEqual(checks.zenodo_endpoint_check(None), [])


# -- real flows through the fake ---------------------------------------------


class ZenodoFlowsThroughFakeTests(FakeZenodoMixin, TestCase):
    def setUp(self):
        super().setUp()
        self._media = tempfile.mkdtemp()
        media_override = override_settings(MEDIA_ROOT=self._media)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.addCleanup(shutil.rmtree, self._media, ignore_errors=True)
        User = get_user_model()
        self.owner = User.objects.create_user(username="fakeflow")
        self.project = Project.objects.create(
            slug="fake-flow-pump",
            title="Fake Flow Pump",
            summary="Drives the Zenodo flows through the fake.",
            readme="# Fake Flow Pump",
            artifact_type="Software",
            field="Oceanography",
            license="MIT",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.owner,
        )
        Contribution.objects.create(project=self.project, display_name="Flow Tester", role="Project lead")
        payload = _zip_bytes("firmware/main.c", "int main(){}")
        self.attachment = ProjectAttachment.objects.create(
            project=self.project,
            file=ContentFile(payload, name="pump-v1.zip"),
            filename="pump-v1.zip",
            size_bytes=len(payload),
        )

    def test_publish_project_now_end_to_end(self):
        deposit = publish_project_now(self.project, self.owner)

        self.assertEqual(deposit.state, ProjectDeposit.STATE_PUBLISHED)
        fake_dep = self.fz.deposition(deposit.deposition_id)
        self.assertEqual(fake_dep.state, STATE_DONE)
        self.assertEqual(
            sorted(f["filename"] for f in fake_dep.files),
            ["CITATION.cff", "LICENSE.txt", "osprey-project.json", "pump-v1.zip"],
        )
        self.assertEqual(fake_dep.metadata["title"], "Fake Flow Pump")
        self.assertEqual(fake_dep.metadata["creators"][0]["name"], "Flow Tester")
        self.project.refresh_from_db()
        self.assertEqual(self.project.doi, fake_dep.conceptdoi)
        self.assertEqual(deposit.doi, fake_dep.doi)
        self.attachment.refresh_from_db()
        self.assertTrue(self.attachment.published_to_zenodo)
        self.assertFalse(self.attachment.file)  # local copy released
        self.assertEqual(ProjectDepositVersion.objects.filter(deposit=deposit).count(), 1)

    def test_metadata_edit_round_trips_through_edit_mode(self):
        deposit = publish_project_now(self.project, self.owner)
        self.project.title = "Fake Flow Pump, renamed"
        self.project.save(update_fields=["title"])

        update_published_metadata(self.project)

        fake_dep = self.fz.deposition(deposit.deposition_id)
        self.assertEqual(fake_dep.state, STATE_DONE)
        self.assertEqual(fake_dep.metadata["title"], "Fake Flow Pump, renamed")
        self.assertEqual(fake_dep.doi, deposit.doi)
        actions = [p for p in self.fz.paths("POST") if "/actions/" in p]
        self.assertTrue(actions[-2].endswith("/actions/edit"))
        self.assertTrue(actions[-1].endswith("/actions/publish"))

    def test_metadata_edit_failure_discards_the_edit(self):
        deposit = publish_project_now(self.project, self.owner)
        self.fz.add_rule(match="actions/publish", mode="status", status=500)
        with self.assertRaises(ZenodoError):
            update_published_metadata(self.project)
        fake_dep = self.fz.deposition(deposit.deposition_id)
        self.assertEqual(fake_dep.state, STATE_DONE)
        self.assertEqual(fake_dep.metadata["title"], "Fake Flow Pump")

    def test_new_version_clears_carried_files_and_mints_a_version_doi(self):
        deposit = publish_project_now(self.project, self.owner)
        first_id, first_doi = deposit.deposition_id, deposit.doi

        publish_new_version_now(deposit, changelog="Second spin of the board.", user=self.owner)

        deposit.refresh_from_db()
        self.assertEqual(deposit.state, ProjectDeposit.STATE_PUBLISHED)
        self.assertNotEqual(deposit.deposition_id, first_id)
        self.assertNotEqual(deposit.doi, first_doi)
        fake_v2 = self.fz.deposition(deposit.deposition_id)
        self.assertEqual(fake_v2.conceptdoi, self.fz.deposition(first_id).conceptdoi)
        # Carried-over files were cleared; only the fresh sidecars remain.
        self.assertEqual(sorted(f["filename"] for f in fake_v2.files), ["CITATION.cff", "LICENSE.txt", "osprey-project.json"])
        versions = ProjectDepositVersion.objects.filter(deposit=deposit).order_by("version_index")
        self.assertEqual([v.version_index for v in versions], [1, 2])
        self.assertEqual(versions[1].changelog, "Second spin of the board.")
        self.project.refresh_from_db()
        self.assertEqual(self.project.doi, fake_v2.conceptdoi)

    def test_publish_failure_leaves_a_retryable_draft(self):
        self.fz.add_rule(match="actions/publish", mode="status", status=503, message="Service Unavailable", times=1)
        with self.assertRaises(ZenodoError):
            publish_project_now(self.project, self.owner)
        deposit = ProjectDeposit.objects.get(project=self.project)
        self.assertEqual(deposit.state, ProjectDeposit.STATE_ERROR)
        self.assertIn("HTTP 503", deposit.last_error)

        retried = publish_project_now(self.project, self.owner)
        self.assertEqual(retried.state, ProjectDeposit.STATE_PUBLISHED)
        # The retry re-created the draft; the failed one is orphaned on Zenodo.
        self.assertEqual(len([p for p in self.fz.paths("POST") if p == "/api/deposit/depositions"]), 2)

    @override_settings(ZENODO_TIMEOUT_SECONDS=1)
    def test_zenodo_hanging_surfaces_as_a_timeout_not_a_crash(self):
        self.fz.add_rule(match="actions/publish", mode="hang", delay_s=2.5)
        with self.assertRaises(ZenodoError) as caught:
            publish_project_now(self.project, self.owner)
        self.assertIn("timed out", str(caught.exception))
        deposit = ProjectDeposit.objects.get(project=self.project)
        self.assertEqual(deposit.state, ProjectDeposit.STATE_ERROR)

    def test_sync_then_publish_reuses_the_draft(self):
        sync_project_to_zenodo(self.project, self.owner)
        deposit = publish_project_now(self.project, self.owner)
        self.assertEqual(len([p for p in self.fz.paths("POST") if p == "/api/deposit/depositions"]), 1)
        self.assertEqual(deposit.state, ProjectDeposit.STATE_PUBLISHED)
