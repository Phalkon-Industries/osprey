from __future__ import annotations

import io
import os
import tempfile
import unittest
import uuid
import zipfile
from datetime import timedelta
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
from django.utils import timezone

from people.models import Profile

from . import lineage
from .forms import ContributionFormSet, ProjectForm
from .models import (
    ArtifactLink,
    Contribution,
    LineageEdge,
    Project,
    ProjectAttachment,
    ProjectDeposit,
    ProjectDepositVersion,
    Tag,
    TagAssignment,
)
from .templatetags.osprey_md import render_markdown
from .testing.fake_zenodo import FakeZenodoMixin
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


# Isolated media root so file-writing tests never touch the dev media/
# tree (which may carry root-owned folders from old container runs).
_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="osprey-test-media-")


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
            "tags_input": "pump, controller",
            "contributions-TOTAL_FORMS": "1",
            "contributions-INITIAL_FORMS": "0",
            "contributions-MIN_NUM_FORMS": "1",
            "contributions-MAX_NUM_FORMS": "1000",
            "contributions-0-display_name": "Owner Person",
            "contributions-0-role": "Project lead",
            "contributions-0-affiliation": "",
            "contributions-0-orcid_id": (
                "0000-0001-2345-6789" if action == "publish" else ""
            ),
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
        # Credit alone no longer confers edit rights; a verified row with
        # the owner-granted editor flag does.
        self.assertFalse(self.private_project.editable_by(self.contributor))
        Contribution.objects.filter(
            project=self.private_project, user=self.contributor
        ).update(claim_status=Contribution.CLAIM_VERIFIED, editor=True)
        self.assertTrue(self.private_project.editable_by(self.contributor))
        self.assertFalse(self.private_project.editable_by(self.unrelated))
        self.assertTrue(self.private_project.publishable_by(self.owner))
        self.assertFalse(self.private_project.publishable_by(self.contributor))

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

    def test_contribution_save_never_auto_links(self):
        Profile.objects.filter(user=self.owner).update(
            orcid_placeholder="0000-0001-2345-6789"
        )

        contribution = Contribution.objects.create(
            project=self.public_project,
            orcid_id="0000-0001-2345-6789",
            display_name="Owner Person",
            role="Project lead",
        )

        # Linking requires the person's acceptance; nothing attaches on save.
        self.assertIsNone(contribution.user)
        self.assertEqual(contribution.claim_status, Contribution.CLAIM_UNCLAIMED)


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
                "license_choice": "CERN-OHL-S-2.0",
                "tags_input": "Pump, pump, CO2",
            }
        )

        self.assertTrue(form.is_valid(), form.errors.as_json())
        with patch("projects.forms.secrets.token_hex", return_value="abcd"):
            project = form.save()
        self.assertEqual(project.license, "CERN-OHL-S-2.0")
        self.assertEqual(project.slug, "field-test-pump-abcd")
        self.assertEqual(
            list(project.tags.values_list("name", flat=True)), ["co2", "pump"]
        )

    def test_license_list_is_curated_and_open_only(self):
        from projects.forms import COMMON_LICENSES

        ids = [code for code, _ in COMMON_LICENSES]
        # The 2026-09 lineup: copyleft picks first (the promoted side),
        # then permissive, then the four extras.
        self.assertEqual(
            ids,
            [
                "AGPL-3.0",
                "CERN-OHL-S-2.0",
                "CC-BY-SA-4.0",
                "MIT",
                "CERN-OHL-P-2.0",
                "CC-BY-4.0",
                "Apache-2.0",
                "GPL-3.0",
                "CERN-OHL-W-2.0",
                "CC0-1.0",
            ],
        )

    def test_dropdown_groups_lead_with_copyleft(self):
        rendered = str(ProjectForm()["license_choice"])
        copyleft_at = rendered.index("Copyleft (derivatives must stay open)")
        permissive_at = rendered.index("Permissive (closed derivatives allowed)")
        other_at = rendered.index("Other licenses")
        self.assertLess(copyleft_at, permissive_at)
        self.assertLess(permissive_at, other_at)

    def test_no_free_text_license_entry(self):
        form = ProjectForm()
        self.assertNotIn("license_custom", form.fields)
        rendered = str(form["license_choice"])
        self.assertNotIn("__other__", rendered)
        self.assertNotIn("Other (write below)", rendered)

    def test_legacy_license_survives_editing(self):
        self.private_project.license = "BSD-3-Clause"
        self.private_project.save(update_fields=["license"])
        form = ProjectForm(instance=self.private_project)
        rendered = str(form["license_choice"])
        self.assertIn("Current license: BSD-3-Clause", rendered)
        # And submitting with the legacy value keeps it.
        data = self.project_form_post_data(action="save")
        data["license_choice"] = "BSD-3-Clause"
        data["contributions-INITIAL_FORMS"] = "1"
        data["contributions-0-id"] = str(
            self.private_project.contributions.first().pk
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("projects:edit", args=[self.private_project.slug]), data
        )
        self.assertEqual(response.status_code, 302)
        self.private_project.refresh_from_db()
        self.assertEqual(self.private_project.license, "BSD-3-Clause")

    def test_license_guide_reflects_curated_list(self):
        response = self.client.get(reverse("license_guide"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "the firmware, and the documentation")
        body = response.content.decode()
        self.assertLess(
            body.index("Copyleft: derivatives must stay open"),
            body.index("Permissive: anything goes"),
        )
        self.assertContains(response, "Suggestion Box")
        self.assertNotContains(response, "TAPR")
        self.assertNotContains(response, "Unlicense")

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

    def test_form_tab_panels_are_siblings(self):
        # A duplicated <section> opener once nested the Lineage panel
        # inside "More details": the tab looked empty and publish could
        # never go all-green. Parse the page the way a browser does and
        # require every panel at top level, one per tab.
        from html.parser import HTMLParser

        class PanelNesting(HTMLParser):
            def __init__(self):
                super().__init__()
                self.depth = 0
                self.panels = []

            def handle_starttag(self, tag, attrs):
                if tag != "section":
                    return
                a = dict(attrs)
                if "data-form-panel" in a:
                    self.panels.append((a["data-form-panel"], self.depth))
                self.depth += 1

            def handle_endtag(self, tag):
                if tag == "section":
                    self.depth = max(0, self.depth - 1)

        self.client.force_login(self.owner)
        response = self.client.get(reverse("projects:new"))
        parser = PanelNesting()
        parser.feed(response.content.decode())
        names = [name for name, _ in parser.panels]
        self.assertEqual(
            names,
            ["basics", "description", "images", "files", "contributors", "details", "related"],
        )
        self.assertEqual(
            [d for _, d in parser.panels], [0] * 7,
            f"nested form panels: {parser.panels}",
        )

    def test_save_continue_returns_to_form_on_next_tab(self):
        self.client.force_login(self.owner)
        data = self.project_form_post_data(action="save_continue")
        data["title"] = "Sectioned Pump"
        data["next_tab"] = "description"
        data["marked_section"] = "basics"

        response = self.client.post(reverse("projects:new"), data)

        project = Project.objects.get(title="Sectioned Pump")
        self.assertEqual(project.visibility, Project.VISIBILITY_PRIVATE)
        self.assertRedirects(
            response,
            reverse("projects:edit", args=[project.slug])
            + "?tab=description&marked=basics",
        )
        # Unknown tab names never reach the redirect URL.
        data["title"] = "Sectioned Pump Two"
        data["next_tab"] = "evil"
        data["marked_section"] = "basics,alert(1)"
        response = self.client.post(reverse("projects:new"), data)
        project2 = Project.objects.get(title="Sectioned Pump Two")
        self.assertRedirects(
            response,
            reverse("projects:edit", args=[project2.slug]) + "?marked=basics",
        )

    def test_edit_and_new_version_buttons_show_on_every_project_tab(self):
        from projects.models import ProjectDeposit

        ProjectDeposit.objects.create(
            project=self.public_project,
            deposition_id="42",
            state=ProjectDeposit.STATE_PUBLISHED,
        )
        edit_url = reverse("projects:edit", args=[self.public_project.slug])
        nv_url = reverse("projects:zenodo_new_version", args=[self.public_project.slug])
        tab_urls = [
            reverse("projects:detail", args=[self.public_project.slug]),
            reverse("wiki:index", args=[self.public_project.slug]),
            reverse("conversations:index", args=[self.public_project.slug]),
            reverse("use_reports:index", args=[self.public_project.slug]),
            reverse("projects:lineage", args=[self.public_project.slug]),
            reverse("projects:versions", args=[self.public_project.slug]),
            reverse("projects:citations", args=[self.public_project.slug]),
        ]
        self.client.force_login(self.owner)
        for url in tab_urls:
            with self.subTest(url=url, who="owner"):
                response = self.client.get(url)
                self.assertContains(response, f'href="{edit_url}"')
                self.assertContains(response, f'href="{nv_url}"')
        self.client.force_login(self.unrelated)
        for url in tab_urls:
            with self.subTest(url=url, who="stranger"):
                response = self.client.get(url)
                self.assertNotContains(response, f'href="{edit_url}"')
                self.assertNotContains(response, f'href="{nv_url}"')

    def test_owner_row_cannot_be_removed_or_have_its_orcid_detached(self):
        add_orcid_account(self.owner, "0000-0001-2345-6789")
        self.client.force_login(self.owner)
        # New form: the pre-filled first row is the owner's.
        response = self.client.get(reverse("projects:new"))
        html = response.content.decode()
        owner_row = html[html.index('id="contrib-0"'):]
        owner_row = owner_row[:owner_row.index("</fieldset>")]
        self.assertIn("data-owner-row", owner_row)
        self.assertIn("The Project Owner must be listed as a contributor.", owner_row)
        self.assertNotIn("data-contributor-remove", owner_row)
        self.assertNotIn("data-contributor-orcid-remove", owner_row)
        # Edit form: the owner's saved row, next to a removable collaborator.
        Contribution.objects.create(
            project=self.private_project, display_name="Owner Person", role="Lead",
            orcid_id="0000-0001-2345-6789", user=self.owner, order=0,
        )
        Contribution.objects.create(
            project=self.private_project, display_name="Collab", role="Firmware", order=1,
        )
        response = self.client.get(reverse("projects:edit", args=[self.private_project.slug]))
        html = response.content.decode()
        rows = html.split('<fieldset class="contributor-row"')[1:]
        by_name = {("Owner Person" if "Owner Person" in r else "Collab"): r for r in rows if "Owner Person" in r or "Collab" in r}
        self.assertIn("data-owner-row", by_name["Owner Person"])
        self.assertNotIn("data-contributor-remove", by_name["Owner Person"])
        self.assertNotIn("data-owner-row", by_name["Collab"])
        self.assertIn("data-contributor-remove", by_name["Collab"])

    def test_contributors_tab_links_the_guidelines_page(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("projects:new"))
        self.assertContains(response, "OSPREY Contributor Guidelines")
        self.assertContains(response, reverse("contributor_guidelines"))
        page = self.client.get(reverse("contributor_guidelines"))
        self.assertContains(page, "Who to list")

    def test_image_input_states_accepted_types(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("projects:edit", args=[self.private_project.slug]))
        self.assertContains(response, 'accept="image/png,image/jpeg,image/gif,image/webp" data-image-input')
        self.assertContains(response, "PNG, JPEG, GIF or WebP.")

    def test_save_draft_creates_private_project_and_renders_detail(self):
        self.client.force_login(self.owner)
        data = self.project_form_post_data(action="draft")
        data["title"] = "Brand New Draft"

        response = self.client.post(reverse("projects:new"), data, follow=True)

        self.assertEqual(response.status_code, 200)
        project = Project.objects.get(title="Brand New Draft")
        self.assertEqual(project.visibility, Project.VISIBILITY_PRIVATE)
        self.assertEqual(project.created_by, self.owner)
        self.assertEqual(
            response.redirect_chain, [(project.get_absolute_url(), 302)]
        )
        self.assertContains(response, "Brand New Draft")

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

    @override_settings(ZENODO_JOBS_INLINE=True)
    @patch("projects.zenodo_jobs.publish_project_now")
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

    @override_settings(ZENODO_JOBS_INLINE=True)
    @patch("projects.zenodo_jobs.publish_project_now")
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

    @override_settings(ZENODO_JOBS_INLINE=True)
    @patch("projects.zenodo_jobs.publish_project_now")
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


class PublishRequiresLicenseTests(ProjectTestCase):
    # The browser marks License required; the server refuses too, so no
    # request can publish a project Zenodo would relabel CC BY 4.0.
    def setUp(self):
        super().setUp()
        add_orcid_account(self.owner, "0000-0001-2345-6789")
        self.client.force_login(self.owner)

    @override_settings(ZENODO_JOBS_INLINE=True)
    @patch("projects.zenodo_jobs.publish_project_now")
    def test_new_project_without_a_license_is_not_published(self, publish_mock):
        data = self.project_form_post_data(action="publish")
        data["title"] = "Unlicensed Pump"
        data["license_choice"] = ""
        response = self.client.post(reverse("projects:new"), data)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Choose a license", " ".join(response.context["form"].errors.get("license_choice", [])))
        publish_mock.assert_not_called()
        self.assertFalse(Project.objects.filter(title="Unlicensed Pump").exists())

    @override_settings(ZENODO_JOBS_INLINE=True)
    @patch("projects.zenodo_jobs.publish_project_now")
    def test_draft_without_a_sendable_license_is_not_published(self, publish_mock):
        for license in ["", "WTFPL"]:
            with self.subTest(license=license):
                Project.objects.filter(pk=self.private_project.pk).update(license=license)
                data = self.project_form_post_data(action="publish")
                data["license_choice"] = license
                data["contributions-INITIAL_FORMS"] = "1"
                data["contributions-0-id"] = str(self.private_project.contributions.first().pk)
                response = self.client.post(reverse("projects:edit", args=[self.private_project.slug]), data)
                self.assertEqual(response.status_code, 200)
                self.assertIn("Choose a license", " ".join(response.context["form"].errors.get("license_choice", [])))
                publish_mock.assert_not_called()
                self.private_project.refresh_from_db()
                self.assertEqual(self.private_project.visibility, Project.VISIBILITY_PRIVATE)

    @patch("projects.zenodo_jobs.publish_project_now")
    def test_saving_a_draft_without_a_license_still_works(self, publish_mock):
        data = self.project_form_post_data(action="draft")
        data["title"] = "Unlicensed Draft"
        data["license_choice"] = ""
        response = self.client.post(reverse("projects:new"), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Project.objects.get(title="Unlicensed Draft").license, "")


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
        self.assertEqual(metadata["upload_type"], "other")
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
        # Three sidecar uploads: osprey-project.json, CITATION.cff, LICENSE.txt.
        self.assertEqual(client.upload_to_bucket.call_count, 3)
        self.assertEqual(
            [c.args[1].filename for c in client.upload_to_bucket.call_args_list],
            ["osprey-project.json", "CITATION.cff", "LICENSE.txt"],
        )

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
@tag("live-zenodo")
class LiveZenodoSandboxTests(TestCase):
    """Hits the real Zenodo sandbox. Opt-in only:
    RUN_LIVE_ZENODO=1 manage.py test --tag=live-zenodo
    (a token alone used to be enough, which made the default suite
    create sandbox deposits on every run in dev)."""

    def setUp(self):
        if os.environ.get("RUN_LIVE_ZENODO") != "1" or not os.environ.get("ZENODO_ACCESS_TOKEN"):
            self.skipTest("set RUN_LIVE_ZENODO=1 and ZENODO_ACCESS_TOKEN for live Zenodo tests")
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
        # Three sidecar uploads: osprey-project.json, CITATION.cff, LICENSE.txt.
        self.assertEqual(client.upload_to_bucket.call_count, 3)
        self.assertEqual(
            [c.args[1].filename for c in client.upload_to_bucket.call_args_list],
            ["osprey-project.json", "CITATION.cff", "LICENSE.txt"],
        )

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

    @override_settings(ZENODO_JOBS_INLINE=True)
    @patch("projects.zenodo_jobs.publish_new_version_now")
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

    @override_settings(ZENODO_JOBS_INLINE=True)
    @patch("projects.zenodo_jobs.publish_new_version_now")
    def test_new_version_view_refuses_a_license_zenodo_cannot_take(self, service_mock):
        # A legacy license is offered as "Current license"; keeping it would
        # fail in the job. The page refuses up front, like the project form.
        self._published_deposit()
        Project.objects.filter(pk=self.public_project.pk).update(license="WTFPL")
        add_orcid_account(self.owner)
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("projects:zenodo_new_version", args=[self.public_project.slug]),
            {
                "action": "publish",
                "changelog": "Cleaned up wiring.",
                "license_choice": "WTFPL",
                "archive": SimpleUploadedFile(
                    "panda-v2.zip", b"PK\x03\x04stub", content_type="application/zip"
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Choose a license", " ".join(response.context["form"].errors.get("license_choice", [])))
        service_mock.assert_not_called()
        from .models import ZenodoJob

        self.assertFalse(ZenodoJob.objects.exists())

    @override_settings(ZENODO_JOBS_INLINE=True)
    @patch("projects.zenodo_jobs.publish_new_version_now")
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

    @override_settings(ZENODO_JOBS_INLINE=True)
    @patch("projects.zenodo_jobs.publish_project_now")
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


# --- Inline ORCID person search --------------------------------------------


class OrcidSearchHelperTests(TestCase):
    """Pure helpers in projects.orcid_search."""

    def test_credit_name_wins(self):
        from .orcid_search import normalize_row

        row = normalize_row(
            {
                "orcid-id": "0000-0001-2345-6789",
                "given-names": "Anna",
                "family-names": "Michel",
                "credit-name": "Anna P. M. Michel",
                "institution-name": ["WHOI"],
            }
        )
        self.assertEqual(row["display_name"], "Anna P. M. Michel")
        self.assertEqual(row["affiliation"], "WHOI")

    def test_given_family_fallback(self):
        from .orcid_search import normalize_row

        row = normalize_row(
            {
                "orcid-id": "0000-0001-2345-6789",
                "given-names": "Anna",
                "family-names": "Michel",
                "institution-name": ["WHOI", "MIT"],
            }
        )
        self.assertEqual(row["display_name"], "Anna Michel")
        # First institution wins for the inline affiliation field.
        self.assertEqual(row["affiliation"], "WHOI")
        self.assertEqual(row["institutions"], ["WHOI", "MIT"])

    def test_missing_name_falls_back_to_orcid_id(self):
        from .orcid_search import normalize_row

        row = normalize_row({"orcid-id": "0000-0001-2345-6789"})
        self.assertEqual(row["display_name"], "ORCID 0000-0001-2345-6789")
        self.assertEqual(row["affiliation"], "")

    def test_string_institution_normalized_to_first(self):
        from .orcid_search import normalize_row

        row = normalize_row(
            {
                "orcid-id": "0000-0001-2345-6789",
                "given-names": "A",
                "family-names": "B",
                "institution-name": "Only Place",
            }
        )
        self.assertEqual(row["affiliation"], "Only Place")
        self.assertEqual(row["institutions"], ["Only Place"])

    def test_expanded_search_blank_query_short_circuits(self):
        from .orcid_search import expanded_search

        self.assertEqual(expanded_search("  "), {"results": [], "error": ""})

    def test_expanded_search_handles_outage(self):
        from .orcid_search import expanded_search

        with patch("projects.orcid_search.urlopen") as mock_open:
            mock_open.side_effect = error.URLError("name resolution failed")
            data = expanded_search("anna michel")
        self.assertEqual(data["results"], [])
        self.assertIn("Could not reach ORCID", data["error"])


class OrcidSearchEndpointTests(TestCase):
    """The login-gated JSON endpoint feeding the inline search UI."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="alice")
        self.url = reverse("projects:orcid_search")

    def test_login_required(self):
        response = self.client.get(self.url + "?q=anna")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response["Location"])

    def test_returns_normalized_rows(self):
        from django.core.cache import cache

        cache.clear()
        self.client.force_login(self.user)
        fake_payload = {
            "expanded-result": [
                {
                    "orcid-id": "0000-0001-2345-6789",
                    "given-names": "Anna",
                    "family-names": "Michel",
                    "credit-name": "Anna P. M. Michel",
                    "institution-name": ["WHOI"],
                }
            ]
        }
        with patch("projects.orcid_search.expanded_search") as mock_search:
            mock_search.return_value = {
                "results": [
                    {
                        "orcid_id": "0000-0001-2345-6789",
                        "display_name": "Anna P. M. Michel",
                        "affiliation": "WHOI",
                        "institutions": ["WHOI"],
                        "given_names": "Anna",
                        "family_names": "Michel",
                        "credit_name": "Anna P. M. Michel",
                    }
                ],
                "error": "",
            }
            response = self.client.get(self.url + "?q=anna michel")
            mock_search.assert_called_once()
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["error"], "")
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["orcid_id"], "0000-0001-2345-6789")
        self.assertEqual(payload["results"][0]["display_name"], "Anna P. M. Michel")
        self.assertEqual(
            fake_payload["expanded-result"][0]["orcid-id"], "0000-0001-2345-6789"
        )

    def test_blank_query_returns_empty(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url + "?q=")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"results": [], "error": ""})


class ContributorOrcidFormTests(TestCase):
    """The form widget contract that locks ORCID iDs to the inline search."""

    def test_orcid_widget_is_hidden(self):
        import django.forms as forms
        from .forms import ContributionForm

        form = ContributionForm()
        widget = form.fields["orcid_id"].widget
        self.assertIsInstance(widget, forms.HiddenInput)

    def test_form_template_has_no_visible_orcid_input(self):
        from .forms import ContributionForm

        form = ContributionForm()
        rendered = str(form["orcid_id"])
        self.assertIn('type="hidden"', rendered)
        self.assertIn("data-contributor-orcid", rendered)


class ContributorOrcidTemplateTests(ProjectTestCase):
    """Integration: project form page exposes the inline ORCID UI."""

    def test_new_project_page_has_inline_orcid_controls(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("projects:new"))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("data-contributor-orcid-search", body)
        self.assertIn("data-contributor-orcid-remove", body)
        self.assertIn("data-orcid-search-overlay", body)
        self.assertIn("orcid-search/", body)
        # ORCID iD must only render as a hidden input on the form.
        self.assertNotIn("ORCID iD (optional)", body)


@override_settings(MEDIA_ROOT=_TEST_MEDIA_ROOT)
class DraftDataLossRegressionTests(ProjectTestCase):
    """Regressions for the draft data-loss bug tracked in planning/to-do.md.

    The reported failure: a form POST carrying typed work plus a zip dies,
    the browser lands on an error page, and everything typed is gone. Each
    test pins one path that must either succeed or re-render the bound
    form; none of them may escalate to a 500.
    """

    def _zip_upload(self, name: str = "archive.zip", payload_bytes: int = 3 * 1024 * 1024):
        # Bigger than FILE_UPLOAD_MAX_MEMORY_SIZE (2 MiB) so Django parses
        # the upload through a TemporaryUploadedFile on disk, matching how
        # a real project archive arrives. os.urandom doesn't compress, so
        # the zip stays over the threshold.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("payload.bin", os.urandom(payload_bytes))
        buffer.seek(0)
        return SimpleUploadedFile(name, buffer.read(), content_type="application/zip")

    def _edit_post_data(self, **overrides) -> dict:
        data = self.project_form_post_data(action="save")
        data["contributions-INITIAL_FORMS"] = "1"
        data["contributions-0-id"] = str(self.private_project.contributions.first().pk)
        data.update(overrides)
        return data

    def test_edit_draft_with_large_zip_saves_everything(self):
        self.client.force_login(self.owner)
        data = self._edit_post_data(title="Edited With Zip")
        data["attachment_files"] = self._zip_upload()

        response = self.client.post(
            reverse("projects:edit", args=[self.private_project.slug]), data
        )

        self.assertEqual(response.status_code, 302)
        self.private_project.refresh_from_db()
        self.assertEqual(self.private_project.title, "Edited With Zip")
        attachment = self.private_project.attachments.get()
        self.assertGreater(attachment.size_bytes, 2 * 1024 * 1024)
        with attachment.file.open("rb") as fh:
            self.assertEqual(fh.read(2), b"PK")

    @override_settings(ZENODO_JOBS_INLINE=True)
    @patch("projects.zenodo_jobs.publish_project_now")
    def test_unexpected_publish_crash_on_new_keeps_draft(self, publish_mock):
        publish_mock.side_effect = RuntimeError("simulated crash mid-upload")
        add_orcid_account(self.owner)
        self.client.force_login(self.owner)
        data = self.project_form_post_data(action="publish")
        data["title"] = "Crash Survivor"

        response = self.client.post(reverse("projects:new"), data)

        # Must not 500: the draft is saved, the user is told, the work survives.
        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(title="Crash Survivor")
        self.assertEqual(project.visibility, Project.VISIBILITY_PRIVATE)

    @override_settings(ZENODO_JOBS_INLINE=True)
    @patch("projects.zenodo_jobs.publish_project_now")
    def test_unexpected_publish_crash_on_edit_keeps_changes(self, publish_mock):
        publish_mock.side_effect = RuntimeError("simulated crash mid-upload")
        add_orcid_account(self.owner)
        self.client.force_login(self.owner)
        data = self._edit_post_data(action="publish", title="Edited Crash Survivor")
        data["contributions-0-orcid_id"] = "0000-0001-2345-6789"

        response = self.client.post(
            reverse("projects:edit", args=[self.private_project.slug]), data
        )

        self.assertEqual(response.status_code, 302)
        self.private_project.refresh_from_db()
        self.assertEqual(self.private_project.title, "Edited Crash Survivor")
        self.assertEqual(
            self.private_project.visibility, Project.VISIBILITY_PRIVATE
        )

    def test_tampered_management_form_rerenders_instead_of_500(self):
        self.client.force_login(self.owner)
        data = self._edit_post_data(title="Typed And Precious")
        del data["contributions-TOTAL_FORMS"]

        response = self.client.post(
            reverse("projects:edit", args=[self.private_project.slug]), data
        )

        # A broken management form (JS glitch, truncated POST) must come
        # back as the bound form with the typed data still in it.
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Typed And Precious")

    def test_tampered_management_form_on_new_rerenders_instead_of_500(self):
        self.client.force_login(self.owner)
        data = self.project_form_post_data(action="draft")
        data["title"] = "New And Precious"
        del data["contributions-TOTAL_FORMS"]

        response = self.client.post(reverse("projects:new"), data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New And Precious")

    @override_settings(
        ZENODO_USE_SANDBOX=True,
        ZENODO_ACCESS_TOKEN="fake-token",
        ZENODO_API_BASE_URL="https://sandbox.zenodo.org",
    )
    @patch("projects.zenodo.ZenodoClient")
    def test_new_version_changelog_survives_zenodo_failure(self, client_class):
        from .zenodo import start_new_version_for_deposit

        client = client_class.from_settings.return_value
        client.create_new_version.side_effect = ZenodoError("zenodo is down")
        deposit = ProjectDeposit.objects.create(
            project=self.public_project,
            provider=ProjectDeposit.PROVIDER_ZENODO,
            state=ProjectDeposit.STATE_PUBLISHED,
            deposition_id="123",
        )

        with self.assertRaises(ZenodoError):
            start_new_version_for_deposit(
                deposit, changelog="Fixed the seal spec", user=self.owner
            )

        # The user's changelog must be stored before the first network
        # call, so a failed or killed request can't eat it.
        deposit.refresh_from_db()
        self.assertEqual(deposit.pending_changelog, "Fixed the seal spec")


@override_settings(MEDIA_ROOT=_TEST_MEDIA_ROOT)
class AttachmentHandlingTests(ProjectTestCase):
    """The single-zip attachment rules on the project form."""

    def _edit(self, **overrides):
        data = self.project_form_post_data(action="save")
        data["contributions-INITIAL_FORMS"] = "1"
        data["contributions-0-id"] = str(
            self.private_project.contributions.first().pk
        )
        data.update(overrides)
        self.client.force_login(self.owner)
        return self.client.post(
            reverse("projects:edit", args=[self.private_project.slug]),
            data,
            follow=True,
        )

    def _small_zip(self, name="a.zip") -> SimpleUploadedFile:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("f.txt", "hello")
        return SimpleUploadedFile(
            name, buffer.getvalue(), content_type="application/zip"
        )

    def test_non_zip_upload_is_rejected_with_message(self):
        response = self._edit(
            attachment_files=SimpleUploadedFile(
                "notes.txt", b"plain text", content_type="text/plain"
            )
        )
        self.assertContains(response, "only .zip archives are accepted")
        self.assertEqual(self.private_project.attachments.count(), 0)

    def test_zip_detected_by_content_type_without_extension(self):
        response = self._edit(attachment_files=self._small_zip(name="archive.bin"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.private_project.attachments.count(), 1)

    def test_oversize_upload_is_skipped_with_message(self):
        with patch("projects.views.MAX_ATTACHMENT_BYTES", 10):
            response = self._edit(attachment_files=self._small_zip())
        self.assertContains(response, "exceeds")
        self.assertEqual(self.private_project.attachments.count(), 0)

    def test_new_zip_replaces_previous_draft_attachment(self):
        self._edit(attachment_files=self._small_zip(name="first.zip"))
        self._edit(attachment_files=self._small_zip(name="second.zip"))
        attachments = list(self.private_project.attachments.all())
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0].filename, "second.zip")

    def test_published_attachment_survives_delete_request(self):
        published = ProjectAttachment.objects.create(
            project=self.private_project,
            filename="published.zip",
            published_to_zenodo=True,
        )
        draft = ProjectAttachment.objects.create(
            project=self.private_project,
            filename="draft.zip",
            published_to_zenodo=False,
        )
        self._edit(attachment_delete=[str(published.pk), str(draft.pk)])
        remaining = set(
            self.private_project.attachments.values_list("filename", flat=True)
        )
        self.assertEqual(remaining, {"published.zip"})


class MaturityLadderTests(ProjectTestCase):
    def test_ladder_has_ten_cumulative_levels(self):
        from .models import MATURITY_LEVELS

        self.assertEqual(sorted(MATURITY_LEVELS), list(range(1, 11)))

    def test_form_renders_ladder_choices_and_help(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("projects:new"))
        self.assertContains(response, "Project maturity (1–10)")
        self.assertContains(response, "Works reliably in the lab under test conditions")
        self.assertContains(response, "Trusted where failure is not an option")
        self.assertContains(response, "Levels build on each other")
        self.assertNotContains(response, "self-rating")

    def test_detail_page_leads_with_the_words(self):
        self.public_project.self_rating = 6
        self.public_project.self_rating_note = "Two seasons on the mooring."
        self.public_project.save()
        response = self.client.get(
            reverse("projects:detail", args=[self.public_project.slug])
        )
        self.assertContains(response, "Maturity")
        self.assertContains(response, "Works reliably in service conditions")
        self.assertContains(response, "(6/10, self-assessed)")
        self.assertContains(response, "Two seasons on the mooring.")

    def test_label_property_handles_unset_rating(self):
        self.assertEqual(self.public_project.self_rating_label, "")


class SummaryRequiredTests(ProjectTestCase):
    def test_form_rejects_missing_summary(self):
        self.client.force_login(self.owner)
        data = self.project_form_post_data(action="draft")
        data["summary"] = ""
        response = self.client.post(reverse("projects:new"), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required")
        self.assertFalse(Project.objects.filter(title="Submitted Pump").exists())

    def test_form_marks_summary_required(self):
        import re

        self.client.force_login(self.owner)
        response = self.client.get(reverse("projects:new"))
        body = response.content.decode()
        match = re.search(r"<[^>]*name=\"summary\"[^>]*>", body)
        self.assertIsNotNone(match)
        self.assertIn("required", match.group(0))


@override_settings(MEDIA_ROOT=_TEST_MEDIA_ROOT)
class PruneDraftArchivesTests(ProjectTestCase):
    def _attachment(self, *, days_old: int, published: bool = False):
        attachment = ProjectAttachment.objects.create(
            project=self.private_project,
            file=SimpleUploadedFile("archive.zip", b"PK\x03\x04zipbytes"),
            published_to_zenodo=published,
        )
        ProjectAttachment.objects.filter(pk=attachment.pk).update(
            created_at=timezone.now() - timedelta(days=days_old)
        )
        return attachment

    def test_prunes_stale_draft_archives_only(self):
        stale = self._attachment(days_old=31)
        fresh = self._attachment(days_old=5)
        published = self._attachment(days_old=90, published=True)

        out = StringIO()
        call_command("prune_draft_archives", stdout=out)

        remaining = set(
            ProjectAttachment.objects.values_list("pk", flat=True)
        )
        self.assertNotIn(stale.pk, remaining)
        self.assertIn(fresh.pk, remaining)
        self.assertIn(published.pk, remaining)
        self.assertIn("pruned: 1", out.getvalue())

    def test_ignores_rows_without_a_file(self):
        cleared = ProjectAttachment.objects.create(
            project=self.private_project,
            filename="old.zip",
            published_to_zenodo=False,
        )
        ProjectAttachment.objects.filter(pk=cleared.pk).update(
            created_at=timezone.now() - timedelta(days=90)
        )

        call_command("prune_draft_archives")

        self.assertTrue(
            ProjectAttachment.objects.filter(pk=cleared.pk).exists()
        )


class LineageClaimTests(ProjectTestCase):
    def setUp(self):
        super().setUp()
        self.child = Project.objects.create(
            slug="derived-pod",
            title="Derived Pod",
            summary="A pod derived from the pump.",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.unrelated,
        )
        self.draft_child = Project.objects.create(
            slug="draft-pod",
            title="Draft Pod",
            summary="Not yet published.",
            visibility=Project.VISIBILITY_PRIVATE,
            created_by=self.unrelated,
        )

    def _add_version(self, project, index=1):
        deposit, _ = ProjectDeposit.objects.get_or_create(
            project=project, defaults={"deposition_id": f"dep-{project.pk}"}
        )
        return ProjectDepositVersion.objects.create(
            deposit=deposit,
            version_index=index,
            published_at=timezone.now(),
        )

    def _notifications(self, **filters):
        from notifications.models import Notification

        return Notification.objects.filter(**filters)

    def test_resolve_target_accepts_slug_url_and_permalink(self):
        self.assertEqual(
            lineage.resolve_target("public-pump"), self.public_project
        )
        self.assertEqual(
            lineage.resolve_target(
                "https://osprey.phalkon.io/projects/public-pump/"
            ),
            self.public_project,
        )
        self.assertEqual(
            lineage.resolve_target(
                f"https://osprey.phalkon.io/p/{self.public_project.public_id}/"
            ),
            self.public_project,
        )
        self.assertIsNone(lineage.resolve_target("nope-not-here"))

    def test_declare_validations(self):
        _, err = lineage.declare(
            self.child, "derived-pod", "derived_from", self.unrelated
        )
        self.assertIn("itself", err)
        _, err = lineage.declare(
            self.child, "private-pump", "derived_from", self.unrelated
        )
        self.assertIn("isn't published", err)
        _, err = lineage.declare(self.child, "public-pump", "bogus", self.unrelated)
        self.assertEqual(err, "Pick a relation.")
        _, err = lineage.declare(self.child, "zzz-none", "uses", self.unrelated)
        self.assertIn("No OSPREY project", err)

    def test_declare_on_public_child_goes_live_and_notifies_parent(self):
        parent_version = self._add_version(self.public_project)

        edge, err = lineage.declare(
            self.child, "public-pump", "derived_from", self.unrelated
        )

        self.assertIsNone(err)
        self.assertIsNotNone(edge.claimed_at)
        self.assertEqual(edge.status, LineageEdge.STATUS_ACTIVE)
        self.assertEqual(edge.parent_version, parent_version)
        note = self._notifications(user=self.owner, kind="lineage_claimed").get()
        self.assertIn("is derived from your project", note.title)
        self.assertIn("dispute", note.body)

    def test_declare_on_draft_child_stays_dormant(self):
        edge, err = lineage.declare(
            self.draft_child, "public-pump", "uses", self.unrelated
        )

        self.assertIsNone(err)
        self.assertIsNone(edge.claimed_at)
        self.assertFalse(self._notifications(kind="lineage_claimed").exists())

    def test_publish_activation_pins_child_and_notifies(self):
        edge, _ = lineage.declare(
            self.draft_child, "public-pump", "derived_from", self.unrelated
        )
        self.draft_child.visibility = Project.VISIBILITY_PUBLIC
        self.draft_child.save()
        version = self._add_version(self.draft_child)

        count = lineage.activate_pending(self.draft_child)

        edge.refresh_from_db()
        self.assertEqual(count, 1)
        self.assertEqual(edge.child_version, version)
        self.assertIsNotNone(edge.claimed_at)
        self.assertTrue(
            self._notifications(user=self.owner, kind="lineage_claimed").exists()
        )

    def test_repeat_claims_between_versions_are_separate_facts(self):
        first, _ = lineage.declare(self.child, "public-pump", "uses", self.unrelated)
        self._add_version(self.child)

        second, err = lineage.declare(
            self.child, "public-pump", "uses", self.unrelated
        )

        self.assertIsNone(err)
        self.assertEqual(second.status, LineageEdge.STATUS_ACTIVE)
        self.assertNotEqual(first.pk, second.pk)

    def test_same_team_claims_do_not_self_notify(self):
        mine = Project.objects.create(
            slug="my-fork",
            title="My Fork",
            summary="Fork of my own pump.",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.owner,
        )

        edge, err = lineage.declare(mine, "public-pump", "derived_from", self.owner)

        self.assertIsNone(err)
        self.assertEqual(edge.status, LineageEdge.STATUS_ACTIVE)
        self.assertFalse(self._notifications(kind="lineage_claimed").exists())

    def test_respond_permissions_and_dispute_reason(self):
        edge, _ = lineage.declare(
            self.child, "public-pump", "derived_from", self.unrelated
        )

        self.assertIsNotNone(lineage.respond(edge, self.unrelated, "dispute"))

        err = lineage.respond(edge, self.owner, "dispute", "Never saw these files")
        self.assertIsNone(err)
        edge.refresh_from_db()
        self.assertEqual(edge.status, LineageEdge.STATUS_DISPUTED)
        self.assertEqual(edge.dispute_reason, "Never saw these files")
        note = self._notifications(
            user=self.unrelated, kind="lineage_responded", title__icontains="disputed"
        ).get()
        self.assertIn("disputed", note.title)

        err = lineage.respond(edge, self.owner, "retract")
        self.assertIsNone(err)
        edge.refresh_from_db()
        self.assertEqual(edge.status, LineageEdge.STATUS_ACTIVE)
        self.assertEqual(edge.dispute_reason, "")
        self.assertTrue(
            self._notifications(
                user=self.unrelated,
                kind="lineage_responded",
                title__icontains="retracted",
            ).exists()
        )

    def test_withdraw_keeps_row_and_notifies_parent(self):
        edge, _ = lineage.declare(self.child, "public-pump", "uses", self.unrelated)

        self.assertIsNotNone(lineage.withdraw(edge, self.owner))
        self.assertIsNone(lineage.withdraw(edge, self.unrelated))

        edge.refresh_from_db()
        self.assertEqual(edge.status, LineageEdge.STATUS_WITHDRAWN)
        self.assertTrue(
            self._notifications(user=self.owner, kind="lineage_withdrawn").exists()
        )

    def test_duplicate_claim_blocked(self):
        lineage.declare(self.child, "public-pump", "uses", self.unrelated)
        _, err = lineage.declare(self.child, "public-pump", "uses", self.unrelated)
        self.assertIn("already", err)

    def test_edit_form_post_declares_edges(self):
        self.client.force_login(self.unrelated)
        data = self.project_form_post_data()
        data["lineage_target"] = ["public-pump"]
        data["lineage_relation"] = ["uses"]

        response = self.client.post(
            reverse("projects:edit", args=[self.child.slug]), data
        )

        self.assertEqual(response.status_code, 302)
        edge = LineageEdge.objects.get(child=self.child)
        self.assertEqual(edge.parent, self.public_project)
        self.assertEqual(edge.relation, "uses")
        self.assertEqual(edge.declared_by, self.unrelated)

    def test_respond_and_withdraw_views(self):
        edge, _ = lineage.declare(
            self.child, "public-pump", "derived_from", self.unrelated
        )

        self.client.force_login(self.owner)
        respond_url = reverse(
            "projects:lineage_respond", args=[self.public_project.slug, edge.pk]
        )
        response = self.client.post(
            respond_url, {"action": "dispute", "reason": "Wrong files"}
        )
        self.assertEqual(response.status_code, 302)
        edge.refresh_from_db()
        self.assertEqual(edge.status, LineageEdge.STATUS_DISPUTED)

        response = self.client.post(respond_url, {"action": "retract"})
        self.assertEqual(response.status_code, 302)
        edge.refresh_from_db()
        self.assertEqual(edge.status, LineageEdge.STATUS_ACTIVE)

        self.client.force_login(self.unrelated)
        response = self.client.post(
            reverse("projects:lineage_withdraw", args=[self.child.slug, edge.pk])
        )
        self.assertEqual(response.status_code, 302)
        edge.refresh_from_db()
        self.assertEqual(edge.status, LineageEdge.STATUS_WITHDRAWN)

    def test_lineage_page_hides_dormant_inbound_and_offers_actions(self):
        live, _ = lineage.declare(
            self.child, "public-pump", "derived_from", self.unrelated
        )
        lineage.declare(self.draft_child, "public-pump", "uses", self.unrelated)

        self.client.force_login(self.owner)
        url = reverse("projects:lineage", args=[self.public_project.slug])
        response = self.client.get(url)

        self.assertContains(response, "Derived Pod")
        self.assertNotContains(response, "Draft Pod")
        self.assertContains(response, "Manage lineage edges")
        self.assertNotContains(response, ">Dispute<")

        response = self.client.get(url, {"manage": "1"})
        self.assertNotContains(response, ">Accept<")
        self.assertContains(response, "Dispute")

    def test_lineage_lookup_endpoint(self):
        self._add_version(self.public_project, index=1)
        self._add_version(self.public_project, index=2)

        response = self.client.get(reverse("projects:lineage_lookup"), {"q": "public-pump"})
        self.assertEqual(response.status_code, 302)  # login required

        self.client.force_login(self.unrelated)
        response = self.client.get(reverse("projects:lineage_lookup"), {"q": "public-pump"})
        data = response.json()
        self.assertTrue(data["found"])
        self.assertEqual(data["title"], "Public Pump")
        self.assertEqual([v["label"] for v in data["versions"]], ["v2 (latest)", "v1"])

        response = self.client.get(reverse("projects:lineage_lookup"), {"q": "private-pump"})
        self.assertFalse(response.json()["found"])

        response = self.client.get(reverse("projects:lineage_lookup"), {"q": "nope"})
        self.assertFalse(response.json()["found"])

    def test_declare_with_version_pin_override(self):
        v1 = self._add_version(self.public_project, index=1)
        self._add_version(self.public_project, index=2)

        edge, err = lineage.declare(
            self.child, "public-pump", "uses", self.unrelated,
            parent_version_id=v1.pk,
        )

        self.assertIsNone(err)
        self.assertEqual(edge.parent_version, v1)

    def test_edit_form_post_with_version_pin(self):
        v1 = self._add_version(self.public_project, index=1)
        self._add_version(self.public_project, index=2)
        self.client.force_login(self.unrelated)
        data = self.project_form_post_data()
        data["lineage_target"] = ["public-pump"]
        data["lineage_relation"] = ["derived_from"]
        data["lineage_version"] = [str(v1.pk)]

        response = self.client.post(
            reverse("projects:edit", args=[self.child.slug]), data
        )

        self.assertEqual(response.status_code, 302)
        edge = LineageEdge.objects.get(child=self.child)
        self.assertEqual(edge.parent_version, v1)

    def test_deferred_claims_stay_dormant_until_activation(self):
        edge, err = lineage.declare(
            self.child, "public-pump", "uses", self.unrelated, defer=True
        )

        self.assertIsNone(err)
        self.assertIsNone(edge.claimed_at)
        self.assertIsNone(edge.child_version)
        self.assertFalse(self._notifications(kind="lineage_claimed").exists())

        version = self._add_version(self.child)
        count = lineage.activate_pending(self.child)

        edge.refresh_from_db()
        self.assertEqual(count, 1)
        self.assertEqual(edge.child_version, version)
        self.assertIsNotNone(edge.claimed_at)
        self.assertTrue(
            self._notifications(user=self.owner, kind="lineage_claimed").exists()
        )

    def test_withdraw_requires_confirmation_page(self):
        edge, _ = lineage.declare(self.child, "public-pump", "uses", self.unrelated)
        url = reverse(
            "projects:lineage_withdraw", args=[self.child.slug, edge.pk]
        )

        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(url).status_code, 404)

        self.client.force_login(self.unrelated)
        response = self.client.get(url)
        self.assertContains(response, "Withdraw this lineage link?")
        edge.refresh_from_db()
        self.assertNotEqual(edge.status, LineageEdge.STATUS_WITHDRAWN)

        response = self.client.post(url)
        edge.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(edge.status, LineageEdge.STATUS_WITHDRAWN)

    def test_diagram_reaches_two_generations_and_collapses_versions(self):
        root = Project.objects.create(
            slug="root-pump",
            title="Root Pump",
            summary="The ancestor of everything.",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.owner,
        )
        grandchild = Project.objects.create(
            slug="grandchild-pod",
            title="Grandchild Pod",
            summary="Built around the derived pod.",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.unrelated,
        )
        lineage.declare(self.public_project, "root-pump", "derived_from", self.owner)
        lineage.declare(self.child, "public-pump", "derived_from", self.unrelated)
        lineage.declare(grandchild, "derived-pod", "uses", self.unrelated)
        # A second pinned edge between the same pair collapses into the
        # same diagram line, with both relation labels.
        self._add_version(self.child)
        lineage.declare(self.child, "public-pump", "uses", self.unrelated)

        response = self.client.get(
            reverse("projects:lineage", args=[self.child.slug])
        )

        self.assertContains(response, "Root Pump")  # grandparent tier
        self.assertContains(response, "Grandchild Pod")  # grandchild tier
        self.assertContains(response, "modified into · used in")


class ContributorClaimingTests(ProjectTestCase):
    def setUp(self):
        super().setUp()
        self.collab = get_user_model().objects.create_user(username="collab")
        add_orcid_account(self.collab, "0000-0002-1111-2222")
        self.row = Contribution.objects.create(
            project=self.public_project,
            display_name="Collab Person",
            role="Firmware",
            orcid_id="0000-0002-1111-2222",
            order=1,
        )

    def _notifications(self, **filters):
        from notifications.models import Notification

        return Notification.objects.filter(**filters)

    def test_confirmation_request_notifies_and_flips_status(self):
        from projects import claiming

        err = claiming.request_confirmation(self.row, self.owner)

        self.row.refresh_from_db()
        self.assertIsNone(err)
        self.assertEqual(self.row.claim_status, Contribution.CLAIM_INVITED)
        self.assertIsNone(self.row.user)  # nothing links before acceptance
        note = self._notifications(
            user=self.collab, kind="contributor_listed"
        ).get()
        self.assertIn("listed you as a contributor", note.title)

        # Asking again while pending is refused.
        err = claiming.request_confirmation(self.row, self.owner)
        self.assertIn("already requested", err)

    def test_declined_rows_cannot_be_asked_again(self):
        from projects import claiming

        claiming.request_confirmation(self.row, self.owner)
        claiming.decline(self.row, self.collab)
        self.row.refresh_from_db()
        self.assertEqual(self.row.claim_status, Contribution.CLAIM_DECLINED)

        err = claiming.request_confirmation(self.row, self.owner)
        self.assertIn("declined", err)

    def test_external_invite_queues_ephemeral_email_without_storing(self):
        from notifications.models import QueuedEmail
        from projects import claiming

        row = Contribution.objects.create(
            project=self.public_project,
            display_name="Not Here Yet",
            role="CAD",
            orcid_id="0000-0003-9999-0000",
            order=2,
        )

        err = claiming.request_confirmation(row, self.owner)
        self.assertIn("No OSPREY account", err)

        err = claiming.send_osprey_invite(row, self.owner, "them@example.org")
        self.assertIsNone(err)
        queued = QueuedEmail.objects.get(group="invite")
        self.assertIsNone(queued.user)
        self.assertEqual(queued.to_address, "them@example.org")
        self.assertTrue(queued.ephemeral)
        self.assertIn("credited", queued.subject.lower())
        # The invite implies the confirmation request: the row waits for
        # them the moment they join, drafts included.
        row.refresh_from_db()
        self.assertEqual(row.claim_status, Contribution.CLAIM_INVITED)

        # Re-sending (re-entered address) is allowed.
        err = claiming.send_osprey_invite(row, self.owner, "them@example.org")
        self.assertIsNone(err)
        self.assertEqual(QueuedEmail.objects.filter(group="invite").count(), 2)

    def test_accept_links_and_arms_editor_pregrant(self):
        from projects import claiming

        self.row.editor = True
        self.row.save(update_fields=["editor"])
        claiming.request_confirmation(self.row, self.owner)

        self.assertIsNotNone(claiming.accept(self.row, self.unrelated))

        err = claiming.accept(self.row, self.collab)
        self.row.refresh_from_db()
        self.assertIsNone(err)
        self.assertEqual(self.row.user, self.collab)
        self.assertEqual(self.row.claim_status, Contribution.CLAIM_VERIFIED)
        self.assertTrue(self.public_project.editable_by(self.collab))
        self.assertFalse(self.public_project.publishable_by(self.collab))
        self.assertTrue(
            self._notifications(
                user=self.owner, kind="contributor_claim_resolved"
            ).exists()
        )

    def test_decline_with_report_files_moderation_report(self):
        from moderation.models import Report
        from projects import claiming

        claiming.request_confirmation(self.row, self.owner)
        err = claiming.decline(self.row, self.collab, report_reason="Spam listing")

        self.assertIsNone(err)
        report = Report.objects.get()
        self.assertEqual(report.reporter, self.collab)
        self.assertIn("Spam listing", report.reason)

    def test_publish_sweep_invites_matching_rows(self):
        from projects import claiming

        count = claiming.sweep_on_publish(self.public_project)

        self.row.refresh_from_db()
        self.assertEqual(count, 1)
        self.assertEqual(self.row.claim_status, Contribution.CLAIM_INVITED)

    def test_signup_sweep_flips_public_rows(self):
        from projects import claiming

        late = get_user_model().objects.create_user(username="latecomer")
        add_orcid_account(late, "0000-0004-5555-6666")
        row = Contribution.objects.create(
            project=self.public_project,
            display_name="Late Comer",
            role="Testing",
            orcid_id="0000-0004-5555-6666",
            order=3,
        )
        draft_row = Contribution.objects.create(
            project=self.private_project,
            display_name="Late Comer",
            role="Testing",
            orcid_id="0000-0004-5555-6666",
            order=1,
        )

        count = claiming.sweep_on_signup(late)

        row.refresh_from_db()
        draft_row.refresh_from_db()
        self.assertEqual(count, 1)
        self.assertEqual(row.claim_status, Contribution.CLAIM_INVITED)
        self.assertEqual(draft_row.claim_status, Contribution.CLAIM_UNCLAIMED)

    def test_claims_page_accept_flow(self):
        from projects import claiming

        claiming.request_confirmation(self.row, self.owner)
        self.client.force_login(self.collab)

        response = self.client.get(reverse("contributor_claims"))
        self.assertContains(response, "Public Pump")

        response = self.client.post(
            reverse("contributor_claims"),
            {"contribution_id": str(self.row.pk), "action": "accept"},
        )
        self.row.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.row.user, self.collab)

    def test_leave_draft_and_removal_dispute(self):
        from moderation.models import Report
        from projects import claiming

        draft_row = Contribution.objects.create(
            project=self.private_project,
            display_name="Collab Person",
            role="Firmware",
            orcid_id="0000-0002-1111-2222",
            user=self.collab,
            claim_status=Contribution.CLAIM_VERIFIED,
            order=1,
        )
        self.assertIsNone(claiming.leave_draft(draft_row, self.collab))
        draft_row.refresh_from_db()
        self.assertIsNone(draft_row.user)
        self.assertEqual(draft_row.claim_status, Contribution.CLAIM_UNCLAIMED)

        public_row = Contribution.objects.create(
            project=self.public_project,
            display_name="Collab Person 2",
            role="Docs",
            orcid_id="0000-0002-1111-2233",
            user=self.collab,
            claim_status=Contribution.CLAIM_VERIFIED,
            order=4,
        )
        self.assertIsNotNone(claiming.leave_draft(public_row, self.collab))
        self.assertIsNone(
            claiming.request_removal(public_row, self.collab, "Wrong person")
        )
        public_row.refresh_from_db()
        self.assertEqual(public_row.claim_status, Contribution.CLAIM_DISPUTED)
        self.assertTrue(Report.objects.filter(reporter=self.collab).exists())

    def test_manage_listings_page_acts_on_a_selection(self):
        from moderation.models import Report

        draft_row = Contribution.objects.create(
            project=self.private_project,
            display_name="Collab Person",
            role="Firmware",
            orcid_id="0000-0002-1111-2222",
            user=self.collab,
            claim_status=Contribution.CLAIM_VERIFIED,
            order=1,
        )
        public_row = Contribution.objects.create(
            project=self.public_project,
            display_name="Collab Person 2",
            role="Docs",
            orcid_id="0000-0002-1111-2233",
            user=self.collab,
            claim_status=Contribution.CLAIM_VERIFIED,
            order=4,
        )
        self.client.force_login(self.collab)
        # The credits page lists quietly and points at the manage page.
        response = self.client.get(reverse("contributor_claims"))
        self.assertContains(response, "Manage my listings")
        self.assertNotContains(response, "Ask staff to remove me")
        response = self.client.get(reverse("contributor_claims_manage"))
        self.assertContains(response, f'value="{draft_row.pk}"')
        self.assertContains(response, f'value="{public_row.pk}"')

        # One request for both: the draft is simply left, the published
        # one becomes a staff-mediated dispute with the reason attached.
        response = self.client.post(
            reverse("contributor_claims_manage"),
            {
                "action": "request_removal",
                "contribution_ids": [str(draft_row.pk), str(public_row.pk)],
                "reason": "Not my work.",
            },
            follow=True,
        )
        self.assertContains(response, "Removal requested for 2 projects")
        draft_row.refresh_from_db()
        public_row.refresh_from_db()
        self.assertIsNone(draft_row.user)
        self.assertEqual(public_row.claim_status, Contribution.CLAIM_DISPUTED)
        self.assertTrue(Report.objects.filter(reporter=self.collab, reason__icontains="Not my work").exists())

    def test_declined_credit_disappears_from_public_credit_everywhere(self):
        from projects import claiming
        from projects.zenodo import metadata_for_project
        from projects.views import _build_citation_text

        row = Contribution.objects.create(
            project=self.public_project, display_name="Declining Person", role="Docs",
            orcid_id="0000-0002-1111-2233", order=5,
        )
        decliner = get_user_model().objects.create_user(username="decliner")
        add_orcid_account(decliner, "0000-0002-1111-2233")
        self.assertIsNone(claiming.request_confirmation(row, self.owner))
        self.assertIsNone(claiming.decline(row, decliner))

        response = self.client.get(reverse("projects:detail", args=[self.public_project.slug]))
        self.assertNotContains(response, "Declining Person")
        for url in (reverse("projects:list"), reverse("home"), "/api/v1/projects/"):
            self.assertNotContains(self.client.get(url), "Declining Person", msg_prefix=url)
        creators = [c["name"] for c in metadata_for_project(self.public_project)["creators"]]
        self.assertNotIn("Declining Person", creators)
        self.assertNotIn("Declining Person", _build_citation_text(self.public_project, None))
        # The owner still sees the row (and its status) on the form.
        self.client.force_login(self.owner)
        response = self.client.get(reverse("projects:edit", args=[self.public_project.slug]))
        self.assertContains(response, "Declining Person")

    def test_listed_person_can_quietly_view_draft(self):
        draft_row = Contribution.objects.create(
            project=self.private_project,
            display_name="Collab Person",
            role="Firmware",
            orcid_id="0000-0002-1111-2222",
            order=1,
        )

        # Listed by ORCID iD: view access, no notification, no link.
        self.assertTrue(self.private_project.viewable_by(self.collab))
        self.assertFalse(self.private_project.editable_by(self.collab))
        self.assertFalse(
            self._notifications(user=self.collab).exists()
        )

        # Declining gives the quiet view access up.
        draft_row.claim_status = Contribution.CLAIM_DECLINED
        draft_row.save(update_fields=["claim_status"])
        self.assertFalse(self.private_project.viewable_by(self.collab))

    def test_ownership_transfer_flow(self):
        self.row.user = self.collab
        self.row.claim_status = Contribution.CLAIM_VERIFIED
        self.row.save(update_fields=["user", "claim_status"])

        # Owner offers the transfer through the edit form.
        self.client.force_login(self.owner)
        data = self.project_form_post_data()
        data["contributions-INITIAL_FORMS"] = "0"
        data["contributions-TOTAL_FORMS"] = "1"
        data["transfer_to"] = str(self.row.pk)
        response = self.client.post(
            reverse("projects:edit", args=[self.public_project.slug]), data
        )
        self.public_project.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.public_project.pending_owner, self.collab)
        self.assertTrue(
            self._notifications(
                user=self.collab, kind="ownership_transfer_offered"
            ).exists()
        )

        # Recipient accepts; old owner keeps access as a verified editor.
        self.client.force_login(self.collab)
        response = self.client.post(
            reverse(
                "projects:ownership_transfer_respond",
                args=[self.public_project.slug],
            ),
            {"action": "accept"},
        )
        self.public_project.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.public_project.created_by, self.collab)
        self.assertIsNone(self.public_project.pending_owner)
        self.assertTrue(self.public_project.editable_by(self.owner))
        self.assertFalse(self.public_project.publishable_by(self.owner))
        self.assertTrue(
            self._notifications(
                user=self.owner, kind="ownership_transfer_resolved"
            ).exists()
        )

    def test_editor_cannot_publish_via_edit_form(self):
        self.row.user = self.collab
        self.row.claim_status = Contribution.CLAIM_VERIFIED
        self.row.editor = True
        self.row.save(update_fields=["user", "claim_status", "editor"])
        add_orcid_account(self.collab)  # already has one; ensures verified path
        self.client.force_login(self.collab)
        data = self.project_form_post_data(action="publish")
        data["contributions-INITIAL_FORMS"] = "0"

        response = self.client.post(
            reverse("projects:edit", args=[self.private_project.slug]), data
        )

        # Editors can't even open this draft (not listed on it), but on
        # projects they CAN edit, publish is still owner-only: check the
        # guard directly too.
        self.assertFalse(self.private_project.publishable_by(self.collab))
        self.assertIn(response.status_code, (200, 404))

    def test_one_pass_invite_and_editor_from_new_project_form(self):
        self.client.force_login(self.owner)
        data = self.project_form_post_data()
        data.update({
            "contributions-TOTAL_FORMS": "2",
            "contributions-1-display_name": "Collab Person",
            "contributions-1-role": "Firmware",
            "contributions-1-affiliation": "",
            "contributions-1-orcid_id": "0000-0002-1111-2222",
            "contributions-1-credit_statement": "",
            "contributions-1-order": "1",
            "contrib_confirm": ["1"],
            "contrib_editor": ["1"],
        })

        response = self.client.post(reverse("projects:new"), data)

        self.assertEqual(response.status_code, 302)
        project = Project.objects.get(title="Submitted Pump")
        row = project.contributions.get(display_name="Collab Person")
        self.assertEqual(row.claim_status, Contribution.CLAIM_INVITED)
        self.assertTrue(row.editor)
        self.assertIsNone(row.user)
        self.assertTrue(
            self._notifications(
                user=self.collab, kind="contributor_listed"
            ).exists()
        )

    def test_owner_row_cannot_be_deleted(self):
        add_orcid_account(self.owner, "0000-0001-2345-6789")
        owner_row = Contribution.objects.create(
            project=self.public_project,
            user=self.owner,
            display_name="Owner Person",
            role="Project lead",
            orcid_id="0000-0001-2345-6789",
            claim_status=Contribution.CLAIM_VERIFIED,
            order=0,
        )
        self.client.force_login(self.owner)
        data = self.project_form_post_data()
        data.update({
            "contributions-INITIAL_FORMS": "1",
            "contributions-0-id": str(owner_row.pk),
            "contributions-0-DELETE": "on",
        })

        response = self.client.post(
            reverse("projects:edit", args=[self.public_project.slug]), data
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            Contribution.objects.filter(pk=owner_row.pk).exists()
        )

    def test_editors_cannot_touch_the_contributor_list(self):
        # collab becomes an accepted Project Editor.
        self.row.user = self.collab
        self.row.claim_status = Contribution.CLAIM_VERIFIED
        self.row.editor = True
        self.row.save(update_fields=["user", "claim_status", "editor"])
        victim = Contribution.objects.create(
            project=self.public_project,
            display_name="Third Person",
            role="Docs",
            orcid_id="0000-0005-7777-8888",
            order=2,
        )
        before = self.public_project.contributions.count()

        self.client.force_login(self.collab)
        data = self.project_form_post_data()
        data["title"] = "Edited By Editor"
        data.update({
            # Contributor list is owner-only: an added row, a deletion,
            # an editor grant, and a transfer must all be ignored.
            "contributions-TOTAL_FORMS": "2",
            "contributions-INITIAL_FORMS": "1",
            "contributions-0-id": str(victim.pk),
            "contributions-0-display_name": "Third Person",
            "contributions-0-role": "Docs",
            "contributions-0-orcid_id": "0000-0005-7777-8888",
            "contributions-0-order": "0",
            "contributions-0-DELETE": "on",
            "contributions-1-display_name": "Added By Editor",
            "contributions-1-role": "Testing",
            "contributions-1-orcid_id": "0000-0006-1234-5678",
            "contributions-1-order": "1",
            "contrib_editor": ["1"],
            "transfer_to": str(self.row.pk),
        })

        response = self.client.post(
            reverse("projects:edit", args=[self.public_project.slug]), data
        )

        self.assertEqual(response.status_code, 302)
        self.public_project.refresh_from_db()
        self.assertEqual(self.public_project.title, "Edited By Editor")
        self.assertEqual(
            self.public_project.contributions.count(), before
        )
        self.assertFalse(
            self.public_project.contributions.filter(
                display_name="Added By Editor"
            ).exists()
        )
        victim.refresh_from_db()  # not deleted, not granted
        self.assertFalse(victim.editor)
        self.assertIsNone(self.public_project.pending_owner)


@override_settings(MEDIA_ROOT=_TEST_MEDIA_ROOT)
class DraftDeleteTests(ProjectTestCase):
    """Owner deletes a draft; published projects have a DOI and stay."""

    def _attach(self, project):
        return ProjectAttachment.objects.create(
            project=project,
            file=SimpleUploadedFile("draft.zip", b"PK\x03\x04zipbytes"),
        )

    def test_owner_sees_confirmation_page(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("projects:delete", args=[self.private_project.slug])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delete this draft?")
        self.assertContains(response, "Private Pump")
        self.assertTrue(Project.objects.filter(pk=self.private_project.pk).exists())

    def test_owner_post_removes_rows_and_files(self):
        attachment = self._attach(self.private_project)
        path = attachment.file.path
        self.assertTrue(os.path.exists(path))
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("projects:delete", args=[self.private_project.slug]),
            follow=True,
        )

        self.assertEqual(response.redirect_chain[0], (reverse("people:me"), 302))
        self.assertFalse(Project.objects.filter(pk=self.private_project.pk).exists())
        self.assertFalse(ProjectAttachment.objects.filter(pk=attachment.pk).exists())
        self.assertFalse(os.path.exists(path))
        self.assertContains(response, "Deleted the draft")

    def test_editors_contributors_and_strangers_get_404(self):
        Contribution.objects.filter(project=self.private_project).update(
            claim_status="verified", editor=True
        )
        url = reverse("projects:delete", args=[self.private_project.slug])
        for user in (self.contributor, self.unrelated):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url).status_code, 404)
        self.assertTrue(Project.objects.filter(pk=self.private_project.pk).exists())

    def test_anonymous_is_sent_to_sign_in(self):
        response = self.client.post(
            reverse("projects:delete", args=[self.private_project.slug])
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_published_project_is_refused(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("projects:delete", args=[self.public_project.slug]),
            follow=True,
        )
        self.assertTrue(Project.objects.filter(pk=self.public_project.pk).exists())
        self.assertEqual(
            response.redirect_chain[0], (self.public_project.get_absolute_url(), 302)
        )
        self.assertContains(response, "Published projects can&#x27;t be deleted.")

    def test_draft_mid_publish_is_refused(self):
        from .models import ZenodoJob

        ZenodoJob.objects.create(
            project=self.private_project,
            kind=ZenodoJob.KIND_PUBLISH,
            status=ZenodoJob.STATUS_QUEUED,
            requested_by=self.owner,
        )
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("projects:delete", args=[self.private_project.slug]),
            follow=True,
        )
        self.assertTrue(Project.objects.filter(pk=self.private_project.pk).exists())
        self.assertContains(response, "being published")

    def test_edit_form_offers_delete_only_to_the_owner_of_a_draft(self):
        delete_url = reverse("projects:delete", args=[self.private_project.slug])
        self.client.force_login(self.owner)
        response = self.client.get(reverse("projects:edit", args=[self.private_project.slug]))
        self.assertContains(response, delete_url)
        response = self.client.get(reverse("projects:edit", args=[self.public_project.slug]))
        self.assertNotContains(response, "Delete draft")

        Contribution.objects.filter(project=self.private_project).update(
            claim_status="verified", editor=True
        )
        self.client.force_login(self.contributor)
        response = self.client.get(reverse("projects:edit", args=[self.private_project.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, delete_url)


@override_settings(MEDIA_ROOT=_TEST_MEDIA_ROOT)
class DraftDeleteZenodoTests(FakeZenodoMixin, ProjectTestCase):
    """Deleting a draft also removes its never-published Zenodo deposition."""

    def _orphan_deposition(self):
        created = ZenodoClient.from_settings().create_deposition()
        ProjectDeposit.objects.create(
            project=self.private_project,
            deposition_id=str(created["id"]),
            state=ProjectDeposit.STATE_ERROR,
            last_error="Injected failure",
            created_by=self.owner,
        )
        return int(created["id"])

    def test_orphan_deposition_is_deleted_on_zenodo(self):
        dep_id = self._orphan_deposition()
        self.assertIn(dep_id, self.fz.depositions)
        self.client.force_login(self.owner)

        self.client.post(reverse("projects:delete", args=[self.private_project.slug]))

        self.assertNotIn(dep_id, self.fz.depositions)
        self.assertFalse(Project.objects.filter(pk=self.private_project.pk).exists())

    def test_zenodo_outage_does_not_block_the_deletion(self):
        dep_id = self._orphan_deposition()
        self.fz.add_rule(match=f"/api/deposit/depositions/{dep_id}", mode="status", status=500)
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("projects:delete", args=[self.private_project.slug]), follow=True
        )

        self.assertFalse(Project.objects.filter(pk=self.private_project.pk).exists())
        self.assertIn(dep_id, self.fz.depositions)
        self.assertContains(response, "Deleted the draft")

    def test_published_deposition_is_never_deleted(self):
        client = ZenodoClient.from_settings()
        created = client.create_deposition()
        dep_id = int(created["id"])
        self.fz.depositions[dep_id].state = "done"
        with self.assertRaises(ZenodoError):
            client.delete_deposition(str(dep_id))
        self.assertIn(dep_id, self.fz.depositions)


class JobCrashOutsideRunnerTests(ProjectTestCase):
    """A crash between claiming a job and its own failure guard still lands
    on the job, so a broken loop can't leave work 'running' forever."""

    def test_crash_while_loading_the_job_counts_as_an_attempt(self):
        from unittest.mock import patch

        from django.utils import timezone

        from projects import zenodo_jobs
        from projects.models import ZenodoJob

        job = ZenodoJob.objects.create(
            project=self.private_project, kind=ZenodoJob.KIND_PUBLISH, status=ZenodoJob.STATUS_QUEUED,
            requested_by=self.owner, next_attempt_at=timezone.now(),
        )
        with patch("projects.zenodo_jobs.run_job", side_effect=RuntimeError("column projects_project.source does not exist")):
            zenodo_jobs.run_due_jobs()
        job.refresh_from_db()
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.status, ZenodoJob.STATUS_QUEUED)  # scheduled for a retry, not stranded
        self.assertGreater(job.next_attempt_at, timezone.now())
        self.assertIn("projects_project.source", job.last_error)


class SlowJobAlertTests(ProjectTestCase):
    def test_staff_hear_once_about_a_job_waiting_over_ten_minutes(self):
        from datetime import timedelta

        from django.utils import timezone

        from notifications.models import Notification
        from projects import zenodo_jobs
        from projects.models import ZenodoJob

        job = ZenodoJob.objects.create(
            project=self.private_project, kind=ZenodoJob.KIND_PUBLISH, status=ZenodoJob.STATUS_QUEUED,
            requested_by=self.owner, next_attempt_at=timezone.now() + timedelta(hours=1), last_error="HTTP 503",
        )
        self.assertEqual(zenodo_jobs.alert_slow_jobs(), 0)  # just created
        ZenodoJob.objects.filter(pk=job.pk).update(created_at=timezone.now() - timedelta(minutes=11))
        self.assertEqual(zenodo_jobs.alert_slow_jobs(), 1)
        notices = Notification.objects.filter(kind="zenodo_job_slow", user=self.staff)
        self.assertEqual(notices.count(), 1)
        self.assertIn("Private Pump", notices.get().title)
        self.assertIn("HTTP 503", notices.get().body)
        self.assertEqual(zenodo_jobs.alert_slow_jobs(), 0)  # once per job
        self.client.force_login(self.owner)
        page = self.client.get(self.private_project.get_absolute_url())
        self.assertContains(page, "Staff has been notified and will look into it.")
