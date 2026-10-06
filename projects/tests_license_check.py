"""License audit: OSPREY's license vs the Zenodo record vs linked GitHub repos."""
from __future__ import annotations

import tempfile
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils.text import slugify

from notifications.models import Notification
from projects import zenodo_register
from projects.sources import license_check
from projects.models import ArtifactLink, Project, ProjectAttachment
from projects.testing.fake_github import FakeGitHubMixin
from projects.testing.fake_zenodo import FakeZenodoMixin
from projects.tests import add_orcid_account
from projects.tests_journeys import zip_bytes
from projects.tests_registered import OWNER_ORCID, RECORD
from projects.zenodo import publish_project_now

_MEDIA = tempfile.mkdtemp(prefix="osprey-license-test-")


@override_settings(MEDIA_ROOT=_MEDIA, ZENODO_DEFAULT_COMMUNITY="osprey")
class LicenseAuditTests(FakeGitHubMixin, FakeZenodoMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.owner = get_user_model().objects.create_user(username="owner")
        add_orcid_account(self.owner, OWNER_ORCID)
        self.staff = get_user_model().objects.create_user(username="staff", is_staff=True)

    def _native(self, license="MIT", repo="https://github.com/acme/pump", publish=True):
        project = Project.objects.create(
            slug=slugify(f"pump-{license}"), title="Pump", summary="A pump.", readme="# Pump",
            artifact_type="Hardware", field="Oceanography", license=license, canonical_url=repo,
            visibility=Project.VISIBILITY_PUBLIC, created_by=self.owner,
        )
        if publish:
            payload = zip_bytes()
            ProjectAttachment.objects.create(project=project, file=ContentFile(payload, name="pump.zip"), filename="pump.zip", size_bytes=len(payload))
            publish_project_now(project, self.owner)
        return project

    def test_every_license_on_the_form_survives_the_zenodo_round_trip(self):
        # Publish with each dropdown license, read back what Zenodo says,
        # and the audit must agree. The fake answers with the spellings
        # real Zenodo uses (apgl-v3, cc-zero, apache2.0), and applies
        # CC BY 4.0 when no license is sent, as Zenodo does.
        from projects.forms import COMMON_LICENSES

        for key, _label in COMMON_LICENSES:
            with self.subTest(license=key):
                project = self._native(license=key, repo="")
                result = license_check.audit(project, fetch_github=False)
                self.assertEqual(result["findings"], [], result)
                self.assertEqual(result["zenodo"], key)

    def test_zenodo_legacy_spellings_are_recognized(self):
        for raw, key in [
            ("apgl-v3", "AGPL-3.0"), ("agpl-3.0-only", "AGPL-3.0"), ("cc-zero", "CC0-1.0"),
            ("apache2.0", "Apache-2.0"), ("bsd-2-clause-netbsd", "BSD-2-Clause"),
            ("mit-license", "MIT"), ("cern-ohl-s-2.0", "CERN-OHL-S-2.0"),
        ]:
            with self.subTest(raw=raw):
                self.assertEqual(zenodo_register.license_of({"metadata": {"license": {"id": raw}}}), key)

    def test_consistent_project_has_no_findings(self):
        self.gh.seed_repo("acme/pump", license="MIT")
        project = self._native()
        result = license_check.audit(project)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["zenodo"], "MIT")
        self.assertEqual(result["github"], {"acme/pump": "MIT"})
        self.assertFalse(Notification.objects.filter(kind="license_check").exists())

    def test_missing_repo_license_is_found_and_the_owner_hears_once(self):
        self.gh.seed_repo("acme/pump", license="")
        project = self._native()
        license_check.audit(project)
        project.refresh_from_db()
        self.assertEqual([f["kind"] for f in project.license_findings], ["github_missing"])
        notices = Notification.objects.filter(user=self.owner, kind="license_check")
        self.assertEqual(notices.count(), 1)
        self.assertIn("has no license file", notices.get().body)
        license_check.audit(project)  # same finding again: no second notice
        self.assertEqual(Notification.objects.filter(user=self.owner, kind="license_check").count(), 1)
        # Owner sees it on the page and the edit form; visitors don't.
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(project.get_absolute_url()), "License check.")
        self.assertContains(self.client.get(reverse("projects:edit", args=[project.slug])), "has no license file")
        self.client.logout()
        self.assertNotContains(self.client.get(project.get_absolute_url()), "License check.")

    def test_repo_and_zenodo_mismatches(self):
        self.gh.seed_repo("acme/pump", license="GPL-3.0")
        project = self._native()
        result = license_check.audit(project)
        self.assertEqual([f["kind"] for f in result["findings"]], ["github_mismatch"])
        self.assertIn("OSPREY lists MIT; the repository acme/pump says GPL-3.0", result["findings"][0]["text"])
        # The Zenodo record disagreeing (as happened with the CERN-OHL bug) is a finding too.
        Project.objects.filter(pk=project.pk).update(license="CERN-OHL-S-2.0")
        project.refresh_from_db()
        result = license_check.audit(project)
        kinds = [f["kind"] for f in result["findings"]]
        self.assertIn("zenodo_mismatch", kinds)
        self.assertIn("github_mismatch", kinds)

    def test_registered_project_checks_its_artifact_links_too(self):
        record = self.fz.seed_published(RECORD, files=[("pump.zip", 10)])
        project = zenodo_register.register_record(record.doi, self.owner)
        ArtifactLink.objects.create(project=project, kind="github", url="https://github.com/acme/pump-firmware", label="Firmware")
        self.gh.seed_repo("acme/pump-firmware", license="MIT")
        result = license_check.audit(project)
        self.assertEqual(project.license, "CC-BY-4.0")
        self.assertEqual([f["kind"] for f in result["findings"]], ["github_mismatch"])

    def test_command_and_staff_page(self):
        self.gh.seed_repo("acme/pump", license="")
        good = self._native(license="MIT")
        self.gh.seed_repo("acme/pump", license="MIT")
        license_check.audit(good)
        self.gh.seed_repo("acme/other", license="")
        bad = Project.objects.create(slug="bad", title="Bad", artifact_type="Hardware", field="Oceanography", license="MIT",
                                     canonical_url="https://github.com/acme/other", visibility=Project.VISIBILITY_PUBLIC, created_by=self.owner)
        out = StringIO()
        call_command("license_audit", pause=0, stdout=out)
        self.assertIn("checked 2, with findings 1", out.getvalue())
        self.assertIn("bad: The repository acme/other has no license file.", out.getvalue())
        self.client.force_login(self.staff)
        page = self.client.get(reverse("licenses_staff"))
        self.assertContains(page, "Bad")
        self.assertNotContains(page, ">Pump<")
        self.assertContains(page, "has no license file")
        self.client.post(reverse("licenses_staff"), {"action": "recheck", "project_id": bad.pk})
        bad.refresh_from_db()
        self.assertEqual(len(bad.license_findings), 1)
