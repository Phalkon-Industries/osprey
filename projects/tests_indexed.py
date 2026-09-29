"""Indexed entries: ownerless catalog records. See
planning/features/indexed-entries.md."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from moderation.views import _purge_user_content
from projects import lineage
from projects.management.commands.seed_indexed_examples import seed_indexed_examples
from projects.models import Contribution, LineageEdge, Project
from projects.views import _build_source_citation_text


class IndexedBase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.staff = User.objects.create_user(username="staff", is_staff=True)
        self.user = User.objects.create_user(username="reader")
        entries = {p.source: p for p in seed_indexed_examples()}
        self.hardwarex = entries["hardwarex"]
        self.joh = entries["joh"]  # held
        self.zenodo = entries["zenodo"]
        self.github = entries["github"]  # no DOI
        self.native = Project.objects.create(
            slug="native-pump",
            title="Native Pump",
            summary="Published through OSPREY.",
            artifact_type="Hardware",
            field="Oceanography",
            license="MIT",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.user,
        )


class SeedTests(IndexedBase):
    def test_seed_is_idempotent_and_marks_held_entries_private(self):
        before = Project.objects.filter(origin=Project.ORIGIN_INDEXED).count()
        seed_indexed_examples()
        self.assertEqual(Project.objects.filter(origin=Project.ORIGIN_INDEXED).count(), before)
        self.assertEqual(before, 4)
        self.assertEqual(self.joh.visibility, Project.VISIBILITY_PRIVATE)
        self.assertTrue(self.joh.is_held)
        self.assertEqual(self.hardwarex.visibility, Project.VISIBILITY_PUBLIC)
        self.assertEqual(self.hardwarex.contributions.count(), 6)
        self.assertEqual(self.hardwarex.oshwa_uid, "BR000022")

    def test_unique_per_source_and_external_id(self):
        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            Project.objects.create(
                slug="dupe",
                title="Dupe",
                origin=Project.ORIGIN_INDEXED,
                source=Project.SOURCE_GITHUB,
                external_id="OceanographyforEveryone/OpenCTD",
            )


class GuardTests(IndexedBase):
    def test_nobody_owns_or_publishes_an_entry(self):
        for entry in (self.hardwarex, self.github):
            self.assertTrue(entry.is_indexed)
            self.assertFalse(entry.accepts_publish)
            self.assertFalse(entry.publishable_by(self.staff))
            self.assertFalse(entry.editable_by(self.user))
            self.assertTrue(entry.editable_by(self.staff))

    def test_credited_author_with_editor_flag_still_cannot_edit(self):
        row = self.hardwarex.contributions.first()
        row.user = self.user
        row.claim_status = Contribution.CLAIM_VERIFIED
        row.editor = True
        row.save()
        self.assertFalse(self.hardwarex.editable_by(self.user))

    def test_edit_versions_wiki_and_new_version_are_closed(self):
        self.client.force_login(self.staff)
        for name in ("projects:edit", "projects:versions"):
            self.assertEqual(
                self.client.get(reverse(name, args=[self.github.slug])).status_code, 404, name
            )
        # The new-version view turns non-native projects away with a message.
        response = self.client.get(reverse("projects:zenodo_new_version", args=[self.github.slug]))
        self.assertIn(response.status_code, (302, 404))
        self.assertEqual(
            self.client.get(reverse("wiki:index", args=[self.github.slug])).status_code, 404
        )

    def test_held_entry_is_staff_only(self):
        url = self.joh.get_absolute_url()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "held for review")

    def test_purging_a_credited_author_keeps_the_entry(self):
        row = self.zenodo.contributions.first()
        row.user = self.user
        row.claim_status = Contribution.CLAIM_VERIFIED
        row.save()
        _purge_user_content(self.user)
        self.assertTrue(Project.objects.filter(pk=self.zenodo.pk).exists())


class EntryPageTests(IndexedBase):
    def test_article_entry_shows_source_files_authors_and_citation(self):
        response = self.client.get(self.hardwarex.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Indexed &middot; HardwareX")
        self.assertContains(response, "View original")
        # Files live on the Files tab; the overview doesn't repeat the link.
        self.assertNotContains(response, "Design files")
        self.assertContains(response, reverse("projects:files", args=[self.hardwarex.slug]))  # the tab itself
        self.assertContains(response, "Authors as listed by HardwareX")
        self.assertContains(response, "Submitted to the index by")
        body = response.content.decode()
        self.assertLess(body.index("Cite the original"), body.index("Submitted to the index by"))
        self.assertContains(response, "Cite the original")
        self.assertContains(response, "Text from HardwareX, CC BY-NC-ND 4.0")
        self.assertNotContains(response, "Cite this project")
        self.assertNotContains(response, ">Wiki<")
        self.assertNotContains(response, ">Versions<")
        self.assertNotContains(response, "New version")

    def test_staff_see_no_edit_button_on_an_entry(self):
        self.client.force_login(self.staff)
        response = self.client.get(self.github.get_absolute_url())
        self.assertNotContains(response, reverse("projects:edit", args=[self.github.slug]))

    def test_repo_entry_without_doi_has_no_citation_block(self):
        response = self.client.get(self.github.get_absolute_url())
        self.assertContains(response, "Indexed &middot; GitHub")
        self.assertNotContains(response, "Cite the original")
        self.assertContains(response, "https://github.com/OceanographyforEveryone/OpenCTD")

    def test_missing_files_link_says_so_on_the_files_tab(self):
        Project.objects.filter(pk=self.github.pk).update(files_url="")
        response = self.client.get(reverse("projects:files", args=[self.github.slug]))
        self.assertContains(response, "not yet linked")

    def test_signed_in_readers_can_follow_an_entry(self):
        self.client.force_login(self.user)
        response = self.client.get(self.zenodo.get_absolute_url())
        self.assertContains(response, "Follow Project")

    def test_source_citation_format(self):
        text = _build_source_citation_text(self.hardwarex)
        self.assertTrue(text.startswith("Jonas Frank Reis, Luís Felipe Barbosa Marques"))
        self.assertIn("et al. (2026).", text)
        self.assertIn("HardwareX. https://doi.org/10.1016/j.ohx.2026.e00839", text)
        text = _build_source_citation_text(self.zenodo)
        self.assertIn("Zenodo. https://doi.org/10.5281/zenodo.3781524", text)


class ListingTests(IndexedBase):
    def test_kind_filter(self):
        everything = self.client.get(reverse("projects:list"))
        self.assertContains(everything, "OpenCTD")
        self.assertContains(everything, "Native Pump")
        self.assertNotContains(everything, "Spark Assisted")  # held
        only_projects = self.client.get(reverse("projects:list") + "?kind=projects")
        self.assertNotContains(only_projects, "OpenCTD")
        self.assertContains(only_projects, "Native Pump")
        only_indexed = self.client.get(reverse("projects:list") + "?kind=indexed")
        self.assertContains(only_indexed, "OpenCTD")
        self.assertContains(only_indexed, "Indexed &middot; GitHub")
        self.assertNotContains(only_indexed, "Native Pump")

    def test_home_strip_excludes_entries(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Native Pump")
        self.assertNotContains(response, "OpenCTD")

    def test_api_exposes_origin_and_filters_on_it(self):
        response = self.client.get("/api/v1/projects/?origin=indexed")
        data = response.json()
        self.assertEqual({d["origin"] for d in data}, {"indexed"})
        self.assertNotIn("Spark Assisted", str(data))
        self.assertIn("files_url", data[0])
        response = self.client.get("/api/v1/projects/")
        self.assertIn("native", {d["origin"] for d in response.json()})

    def test_api_hides_staff_hidden_projects(self):
        Project.objects.filter(pk=self.native.pk).update(is_staff_hidden=True)
        data = self.client.get("/api/v1/projects/").json()
        self.assertNotIn("native-pump", {d["slug"] for d in data})


class LineageTests(IndexedBase):
    def test_entry_can_be_a_parent_and_the_edge_cannot_be_disputed(self):
        edge, err = lineage.declare(self.native, self.github.slug, "derived_from", self.user)
        self.assertIsNone(err, err)
        self.assertEqual(edge.status, LineageEdge.STATUS_ACTIVE)
        page = self.client.get(reverse("projects:lineage", args=[self.github.slug]))
        self.assertContains(page, "Native Pump")
        self.assertContains(page, "can't be disputed")
        self.assertNotContains(page, "Dispute</summary>")
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("projects:lineage_respond", args=[self.github.slug, edge.pk]),
            {"action": "dispute", "reason": "no"},
        )
        self.assertEqual(response.status_code, 302)
        edge.refresh_from_db()
        self.assertEqual(edge.status, LineageEdge.STATUS_ACTIVE)
        self.assertEqual(
            lineage.respond(edge, self.staff, "dispute", "no"),
            "Indexed entries have no team on OSPREY, so links to them can't be disputed.",
        )

    def test_held_entry_is_not_a_valid_parent(self):
        _, err = lineage.declare(self.native, self.joh.slug, "derived_from", self.user)
        self.assertIsNotNone(err)


class CommunityTabCopyTests(IndexedBase):
    def test_discussion_and_use_reports_say_the_authors_are_probably_absent(self):
        for name in ("conversations:index", "use_reports:index"):
            response = self.client.get(reverse(name, args=[self.github.slug]))
            self.assertEqual(response.status_code, 200, name)
            self.assertContains(response, "This is an indexed entry.")
            native = self.client.get(reverse(name, args=[self.native.slug]))
            self.assertNotContains(native, "This is an indexed entry.")
