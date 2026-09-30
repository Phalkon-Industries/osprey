"""License files in archives, the publish-time block, the LICENSE.txt
sidecar, and the read-only license after publish."""
from __future__ import annotations

import io
import tempfile
import zipfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from projects.licensing import archive_license_conflict, identify_license_text, licenses_in_archive
from projects.models import Project, ProjectAttachment
from projects.testing.fake_zenodo import FakeZenodoMixin
from projects.tests import ProjectTestCase, add_orcid_account
from projects.zenodo import publish_project_now

MIT = "MIT License\n\nCopyright (c) 2026 Someone\n\nPermission is hereby granted, free of charge, to any person obtaining a copy of this software..."
GPL3 = "GNU GENERAL PUBLIC LICENSE\nVersion 3, 29 June 2007\n\nThe GNU General Public License is a free, copyleft license..."
CERN_S = "CERN Open Hardware Licence Version 2 - Strongly Reciprocal\n\nPreamble..."
CC_BY_SA = "Attribution-ShareAlike 4.0 International\n\nCreative Commons Corporation..."
CC_BY_NC = "Attribution-NonCommercial 4.0 International\n\nCreative Commons Corporation..."


def zip_with(files: dict[str, str]) -> io.BytesIO:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    buf.seek(0)
    return buf


class IdentifyTests(TestCase):
    def test_known_texts(self):
        self.assertEqual(identify_license_text(MIT), "MIT")
        self.assertEqual(identify_license_text(GPL3), "GPL-3.0")
        self.assertEqual(identify_license_text(CERN_S), "CERN-OHL-S-2.0")
        self.assertEqual(identify_license_text(CC_BY_SA), "CC-BY-SA-4.0")
        self.assertEqual(identify_license_text(CC_BY_NC), "CC-BY-NC")
        self.assertEqual(identify_license_text("SPDX-License-Identifier: Apache-2.0\n"), "Apache-2.0")
        self.assertEqual(identify_license_text("Some notes about the project."), "")
        self.assertEqual(identify_license_text(""), "")

    def test_archive_scan_finds_root_and_one_level_down_only(self):
        buf = zip_with({
            "LICENSE": MIT,
            "project/COPYING": GPL3,
            "project/firmware/LICENSE.txt": CC_BY_SA,  # too deep
            "project/LICENSE-hardware.md": CERN_S,
            "README.md": "# Pump",
        })
        found = {f.path: f.key for f in licenses_in_archive(buf)}
        self.assertEqual(found, {"LICENSE": "MIT", "project/COPYING": "GPL-3.0", "project/LICENSE-hardware.md": "CERN-OHL-S-2.0"})

    def test_conflict_rules(self):
        self.assertEqual(archive_license_conflict(zip_with({"LICENSE": MIT}), "MIT"), "")
        self.assertEqual(archive_license_conflict(zip_with({"README.md": "no license"}), "MIT"), "")
        self.assertEqual(archive_license_conflict(zip_with({"LICENSE": "custom words"}), "MIT"), "")
        # Dual-licensed: any match passes.
        self.assertEqual(archive_license_conflict(zip_with({"LICENSE-hardware": CERN_S, "LICENSE-software": MIT}), "CERN-OHL-S-2.0"), "")
        msg = archive_license_conflict(zip_with({"LICENSE": GPL3}), "MIT")
        self.assertIn("The provided zip has GPL 3.0 — software (LICENSE), but you chose MIT — software in the overview.", msg)
        self.assertIn("Either remove the license file from the zip before uploading again, or make sure they match.", msg)
        self.assertIn("non-commercial", archive_license_conflict(zip_with({"LICENSE": CC_BY_NC}), "CC-BY-4.0"))
        self.assertEqual(archive_license_conflict(zip_with({"LICENSE": GPL3}), ""), "")
        self.assertEqual(archive_license_conflict(io.BytesIO(b"not a zip"), "MIT"), "")


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="osprey-lic-"), ZENODO_DEFAULT_COMMUNITY="osprey")
class PublishBlockTests(FakeZenodoMixin, ProjectTestCase):
    def setUp(self):
        super().setUp()
        add_orcid_account(self.owner)

    def _post_new(self, archive_files: dict[str, str], license="MIT"):
        data = self.project_form_post_data(action="publish")
        data["license_choice"] = license
        data["title"] = "Blocked Pump"
        data["attachment_files"] = SimpleUploadedFile("pump.zip", zip_with(archive_files).getvalue(), content_type="application/zip")
        return self.client.post(reverse("projects:new"), data)

    def test_new_project_publish_is_blocked_on_a_mismatch_and_nothing_is_saved(self):
        self.client.force_login(self.owner)
        response = self._post_new({"LICENSE": GPL3}, license="MIT")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The provided zip has GPL 3.0")
        self.assertFalse(Project.objects.filter(title="Blocked Pump").exists())

    def test_matching_or_absent_license_file_publishes(self):
        self.client.force_login(self.owner)
        response = self._post_new({"LICENSE": MIT, "src/main.c": "int main(){}"}, license="MIT")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Project.objects.filter(title="Blocked Pump").exists())
        Project.objects.filter(title="Blocked Pump").delete()
        response = self._post_new({"src/main.c": "int main(){}"}, license="MIT")
        self.assertEqual(response.status_code, 302)

    def test_edit_publish_checks_the_pending_draft_archive(self):
        project = self.private_project
        ProjectAttachment.objects.create(project=project, file=ContentFile(zip_with({"LICENSE": CERN_S}).getvalue(), name="d.zip"), filename="d.zip")
        self.client.force_login(self.owner)
        data = self.project_form_post_data(action="publish")
        data["title"] = project.title
        data["license_choice"] = "MIT"
        response = self.client.post(reverse("projects:edit", args=[project.slug]), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The provided zip has CERN OHL-S 2.0")
        project.refresh_from_db()
        self.assertEqual(project.visibility, Project.VISIBILITY_PRIVATE)
        # Same archive, matching choice: published.
        data["license_choice"] = "CERN-OHL-S-2.0"
        response = self.client.post(reverse("projects:edit", args=[project.slug]), data)
        self.assertEqual(response.status_code, 302)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="osprey-lic2-"), ZENODO_DEFAULT_COMMUNITY="osprey")
class SidecarAndLockTests(FakeZenodoMixin, ProjectTestCase):
    def setUp(self):
        super().setUp()
        add_orcid_account(self.owner)
        self.project = self.public_project
        payload = zip_with({"LICENSE": MIT, "a.txt": "x"}).getvalue()
        ProjectAttachment.objects.create(project=self.project, file=ContentFile(payload, name="pump.zip"), filename="pump.zip", size_bytes=len(payload))
        self.deposit = publish_project_now(self.project, self.owner)

    def test_license_txt_sits_next_to_the_archive_on_zenodo(self):
        from projects.licensing import license_sidecar_text

        files = {f["filename"]: f for f in self.fz.deposition(self.deposit.deposition_id).files}
        self.assertIn("LICENSE.txt", files)
        self.assertIn("pump.zip", files)
        text = license_sidecar_text(self.project)
        self.assertEqual(files["LICENSE.txt"]["filesize"], len(text.encode("utf-8")))
        self.assertIn("SPDX-License-Identifier: MIT", text)
        self.assertIn("https://spdx.org/licenses/MIT.html", text)

    def test_license_is_read_only_after_publish(self):
        self.client.force_login(self.owner)
        page = self.client.get(reverse("projects:edit", args=[self.project.slug]))
        self.assertContains(page, "It changes with the next version.")
        data = self.project_form_post_data(action="save")
        data["title"] = self.project.title
        data["license_choice"] = "GPL-3.0"
        self.client.post(reverse("projects:edit", args=[self.project.slug]), data)
        self.project.refresh_from_db()
        self.assertEqual(self.project.license, "MIT")

    def test_new_version_can_change_the_license_and_is_checked(self):
        self.client.force_login(self.owner)
        url = reverse("projects:zenodo_new_version", args=[self.project.slug])
        page = self.client.get(url)
        self.assertContains(page, "License for this version")
        # Mismatch: archive says GPL, choice says MIT.
        response = self.client.post(url, {
            "action": "publish", "changelog": "v2", "license_choice": "MIT",
            "archive": SimpleUploadedFile("v2.zip", zip_with({"LICENSE": GPL3}).getvalue(), content_type="application/zip"),
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "The provided zip has GPL 3.0")
        # Matching: license changes to GPL for this version.
        response = self.client.post(url, {
            "action": "publish", "changelog": "v2", "license_choice": "GPL-3.0",
            "archive": SimpleUploadedFile("v2.zip", zip_with({"LICENSE": GPL3}).getvalue(), content_type="application/zip"),
        })
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.license, "GPL-3.0")
