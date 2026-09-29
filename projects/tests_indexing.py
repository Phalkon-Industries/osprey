"""The importer: line detection, source adapters, gates, staff page, conversion."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from notifications.models import Notification
from projects import lineage, zenodo_register
from projects.indexing import service
from projects.indexing.records import detect, normalize_license
from projects.models import LineageEdge, Project
from projects.testing.fake_github import FakeGitHubMixin
from projects.testing.fake_journals import HARDWAREX_DOI, JOH_DOI, FakeJournalsMixin
from projects.testing.fake_zenodo import FakeZenodoMixin
from projects.tests import add_orcid_account
from projects.tests_registered import COAUTHOR_ORCID, OWNER_ORCID, RECORD


class DetectTests(TestCase):
    def test_lines_classify_by_source(self):
        self.assertEqual(detect("https://github.com/acme/pump.git"), ("github", "acme/pump"))
        self.assertEqual(detect("github.com/acme/pump/"), ("github", "acme/pump"))
        self.assertEqual(detect("https://doi.org/10.5281/zenodo.3781524"), ("zenodo", "10.5281/zenodo.3781524"))
        self.assertEqual(detect("https://zenodo.org/records/19398871"), ("zenodo", "10.5281/zenodo.19398871"))
        self.assertEqual(detect("doi:10.1016/j.ohx.2026.e00839"), ("hardwarex", "10.1016/j.ohx.2026.e00839"))
        self.assertEqual(detect("10.5206/joh.v10i1.24875"), ("joh", "10.5206/joh.v10i1.24875"))
        self.assertEqual(detect("https://doi.org/10.1016/S2468-0672(17)30019-6"), ("hardwarex", "10.1016/S2468-0672(17)30019-6"))
        self.assertEqual(detect("10.1000/xyz123")[0], "doi")
        self.assertEqual(detect("https://example.org/pump")[0], "url")
        self.assertEqual(detect("   "), ("", ""))

    def test_summary_from_readme_skips_headings_badges_and_html(self):
        from projects.indexing.records import summary_from_readme

        readme = (
            "# OpenCTD\n\n"
            "[![build](https://img.shields.io/x.svg)](https://ci)\n"
            "<img src=\"logo.png\" width=200>\n\n"
            "## About\n\n"
            "The **OpenCTD** is a low-cost, open-source CTD for researchers and citizen scientists. "
            "It is built from off-the-shelf parts.\n\nSecond paragraph.\n"
        )
        self.assertEqual(
            summary_from_readme(readme),
            "The OpenCTD is a low-cost, open-source CTD for researchers and citizen scientists. It is built from off-the-shelf parts.",
        )
        self.assertEqual(summary_from_readme("# Title only\n"), "")
        self.assertEqual(summary_from_readme(""), "")
        self.assertTrue(len(summary_from_readme("word " * 200)) <= 280)

    def test_license_normalization(self):
        self.assertEqual(normalize_license("CERN-OHL-S v2.0"), "CERN-OHL-S-2.0")
        self.assertEqual(normalize_license("GNU General Public License v3"), "GPL-3.0")
        self.assertEqual(normalize_license("MIT License"), "MIT")
        self.assertEqual(normalize_license("CC BY 4.0"), "CC-BY-4.0")
        self.assertEqual(normalize_license("CC BY-NC-ND 4.0"), "")
        self.assertEqual(normalize_license("NOASSERTION"), "")
        self.assertEqual(normalize_license(""), "")


@override_settings(ZENODO_DEFAULT_COMMUNITY="osprey")
class ResolveTests(FakeJournalsMixin, FakeGitHubMixin, FakeZenodoMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username="submitter")

    def test_github_repo_with_license_is_live(self):
        self.gh.seed_repo("acme/pump", description="A pump.", license="MIT", topics=["pump", "ocean"], readme="# Pump\n\nBuild notes.")
        p = service.resolve("https://github.com/acme/pump")
        self.assertEqual(p.outcome, service.LIVE, p.message)
        self.assertEqual(p.record.license, "MIT")
        self.assertEqual(p.record.readme, "# Pump\n\nBuild notes.")
        self.assertEqual(p.record.files_url, "https://github.com/acme/pump")
        self.assertEqual([a.name for a in p.record.authors], ["acme"])

    def test_github_summary_falls_back_to_the_readme(self):
        self.gh.seed_repo("acme/quiet", description="", license="MIT", readme="# Quiet\n\nA pump with no About text.\n")
        p = service.resolve("github.com/acme/quiet")
        self.assertEqual(p.record.summary, "A pump with no About text.")
        self.gh.seed_repo("acme/loud", description="About text wins.", license="MIT", readme="# Loud\n\nReadme prose.\n")
        self.assertEqual(service.resolve("github.com/acme/loud").record.summary, "About text wins.")

    def test_github_repo_without_license_is_held(self):
        self.gh.seed_repo("acme/bare", description="No license.", license="")
        p = service.resolve("github.com/acme/bare")
        self.assertEqual(p.outcome, service.HELD)
        self.assertIn("License", p.message)
        self.gh.seed_repo("acme/odd", license="NOASSERTION")
        self.assertEqual(service.resolve("github.com/acme/odd").outcome, service.HELD)

    def test_github_missing_repo_is_an_error(self):
        p = service.resolve("https://github.com/acme/nope")
        self.assertEqual(p.outcome, service.ERROR)
        self.assertIn("doesn't exist", p.message)

    def test_zenodo_record_with_open_license_and_files_is_live(self):
        rec = self.fz.seed_published(RECORD, files=[("pump-v1.zip", 4096)])
        p = service.resolve(f"https://doi.org/{rec.doi}")
        self.assertEqual(p.outcome, service.LIVE, p.message)
        self.assertEqual(p.record.license, "CC-BY-4.0")
        self.assertEqual(p.record.concept_id, str(rec.conceptrecid))
        self.assertEqual(p.record.authors[0].name, "Job Owner")
        self.assertEqual(p.record.authors[0].orcid, OWNER_ORCID)

    def test_zenodo_record_with_unknown_license_or_no_files_is_held(self):
        rec = self.fz.seed_published({**RECORD, "license": {"id": "proprietary-x"}}, files=[("a.zip", 10)])
        p = service.resolve(rec.doi)
        self.assertEqual(p.outcome, service.HELD)
        self.assertIn("License: proprietary-x", p.message)
        rec2 = self.fz.seed_published({**RECORD, "title": "No files"}, files=[])
        p2 = service.resolve(rec2.doi)
        self.assertEqual(p2.outcome, service.HELD)
        self.assertIn("Files:", p2.message)

    def test_creator_is_sent_to_register_instead(self):
        add_orcid_account(self.user, COAUTHOR_ORCID)
        rec = self.fz.seed_published(RECORD, files=[("a.zip", 10)])
        p = service.resolve(rec.doi, self.user)
        self.assertEqual(p.outcome, service.YOURS)
        self.assertEqual(service.resolve(rec.doi).outcome, service.LIVE)

    def test_existing_projects_and_entries_are_reported_not_duplicated(self):
        owner = get_user_model().objects.create_user(username="owner")
        add_orcid_account(owner, OWNER_ORCID)
        rec = self.fz.seed_published(RECORD, files=[("a.zip", 10)])
        registered = zenodo_register.register_record(rec.doi, owner)
        p = service.resolve(rec.doi)
        self.assertEqual(p.outcome, service.EXISTS)
        self.assertEqual(p.existing, registered)
        self.gh.seed_repo("acme/pump", license="MIT")
        first = service.index_record(service.resolve("github.com/acme/pump").record)
        p = service.resolve("https://github.com/ACME/pump")
        self.assertEqual(p.outcome, service.EXISTS)
        self.assertEqual(p.existing, first)

    def test_github_repo_already_linked_from_a_project_is_reported(self):
        from projects.models import ArtifactLink

        native = Project.objects.create(
            slug="pump-native", title="Pump (native)", artifact_type="Hardware", field="Oceanography",
            license="MIT", visibility=Project.VISIBILITY_PUBLIC, created_by=self.user,
            canonical_url="https://github.com/Acme/Pump.git",
        )
        self.gh.seed_repo("acme/pump", license="MIT")
        p = service.resolve("https://github.com/acme/pump/")
        self.assertEqual(p.outcome, service.EXISTS)
        self.assertEqual(p.existing, native)
        # An artifact link counts too, and the owned project wins over an index entry.
        other = Project.objects.create(
            slug="rig", title="Rig", artifact_type="Hardware", field="Oceanography", license="MIT",
            visibility=Project.VISIBILITY_PUBLIC, created_by=self.user,
        )
        ArtifactLink.objects.create(project=other, kind="github", url="https://github.com/acme/rig", label="Rig")
        self.gh.seed_repo("acme/rig", license="MIT")
        self.assertEqual(service.resolve("github.com/acme/rig").existing, other)
        # No false positives on a longer path or a different owner.
        self.gh.seed_repo("acme/pump-docs", license="MIT")
        self.assertEqual(service.resolve("github.com/acme/pump-docs").outcome, service.LIVE)
        self.gh.seed_repo("other/pump", license="MIT")
        self.assertEqual(service.resolve("github.com/other/pump").outcome, service.LIVE)

    def test_unsupported_sources_say_so(self):
        for line in ("10.1000/other", "https://example.org/x"):
            self.assertEqual(service.resolve(line).outcome, service.UNSUPPORTED, line)


@override_settings(ZENODO_DEFAULT_COMMUNITY="osprey")
class IndexRecordTests(FakeJournalsMixin, FakeGitHubMixin, FakeZenodoMixin, TestCase):
    def test_index_creates_live_or_held_entries_without_notifying_anyone(self):
        staff = get_user_model().objects.create_user(username="staff", is_staff=True)
        self.gh.seed_repo("acme/pump", description="A pump.", license="MIT", topics=["pump"])
        self.gh.seed_repo("acme/bare", license="")
        before = Notification.objects.count()
        live = service.index_record(service.resolve("github.com/acme/pump").record)
        held = service.index_record(service.resolve("github.com/acme/bare").record)
        self.assertEqual(live.index_state, Project.INDEX_LIVE)
        self.assertEqual(live.visibility, Project.VISIBILITY_PUBLIC)
        self.assertEqual(live.origin, Project.ORIGIN_INDEXED)
        self.assertEqual(list(live.tags.values_list("name", flat=True)), ["pump"])
        self.assertEqual(held.index_state, Project.INDEX_HELD)
        self.assertEqual(held.visibility, Project.VISIBILITY_PRIVATE)
        self.assertEqual(Notification.objects.count(), before)
        self.assertTrue(live.publishable_by(staff) is False)

    def test_reindex_updates_in_place_and_keeps_claimed_rows(self):
        rec = self.fz.seed_published(RECORD, files=[("a.zip", 10)])
        entry = service.index_record(service.resolve(rec.doi).record)
        row = entry.contributions.get(orcid_id=COAUTHOR_ORCID)
        coauthor = get_user_model().objects.create_user(username="co")
        row.user = coauthor
        row.claim_status = "verified"
        row.save()
        # A second resolve reports EXISTS, so re-index through the adapter directly.
        from projects.indexing import zenodo_source
        again = service.index_record(zenodo_source.fetch(rec.doi))
        self.assertEqual(again.pk, entry.pk)
        self.assertEqual(again.contributions.count(), 3)
        self.assertEqual(again.contributions.get(orcid_id=COAUTHOR_ORCID).user, coauthor)

    def test_staff_hold_override(self):
        self.gh.seed_repo("acme/pump", license="MIT")
        entry = service.index_record(service.resolve("github.com/acme/pump").record, hold=True)
        self.assertEqual(entry.index_state, Project.INDEX_HELD)


@override_settings(ZENODO_DEFAULT_COMMUNITY="osprey")
class StaffPageTests(FakeJournalsMixin, FakeGitHubMixin, FakeZenodoMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(username="staff", is_staff=True)
        self.other_staff = get_user_model().objects.create_user(username="staff2", is_staff=True)
        self.user = get_user_model().objects.create_user(username="plain")
        self.url = reverse("index_staff")

    def test_staff_only(self):
        self.assertIn(self.client.get(self.url).status_code, (302, 403))
        self.client.force_login(self.user)
        self.assertIn(self.client.get(self.url).status_code, (302, 403))
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_preview_then_import_creates_entries_and_one_staff_notice(self):
        self.gh.seed_repo("acme/pump", description="A pump.", license="MIT")
        self.gh.seed_repo("acme/bare", license="")
        self.client.force_login(self.staff)
        lines = "https://github.com/acme/pump\nhttps://github.com/acme/bare\nhttps://github.com/acme/nope\n"
        response = self.client.post(self.url, {"action": "preview", "lines": lines})
        self.assertContains(response, "live")
        self.assertContains(response, "held")
        self.assertContains(response, "doesn&#x27;t exist")
        before = Notification.objects.count()
        response = self.client.post(
            self.url,
            {"action": "import", "lines": lines, "import": ["https://github.com/acme/pump", "https://github.com/acme/bare"]},
            follow=True,
        )
        self.assertContains(response, "Indexed 1 live and 1 held.")
        self.assertEqual(Project.objects.filter(origin=Project.ORIGIN_INDEXED).count(), 2)
        notices = Notification.objects.filter(kind="index_run")
        self.assertEqual(notices.count(), 2)  # one per staff account, one run
        self.assertEqual(Notification.objects.count(), before + 2)
        self.assertContains(response, "Held for review")
        self.assertContains(response, "bare")

    def test_approve_with_edits_and_decline(self):
        self.gh.seed_repo("acme/bare", license="")
        self.gh.seed_repo("acme/other", license="")
        held = service.index_record(service.resolve("github.com/acme/bare").record)
        gone = service.index_record(service.resolve("github.com/acme/other").record)
        self.client.force_login(self.staff)
        self.client.post(self.url, {"action": "approve", "project_id": held.pk, "license": "CERN-OHL-S-2.0", "files_url": "https://example.org/files", "title": "", "summary": ""})
        held.refresh_from_db()
        self.assertEqual(held.index_state, Project.INDEX_LIVE)
        self.assertEqual(held.visibility, Project.VISIBILITY_PUBLIC)
        self.assertEqual(held.license, "CERN-OHL-S-2.0")
        self.assertEqual(held.files_url_source, "staff")
        self.assertEqual(held.gate_license["approved_by"], "staff")
        self.assertEqual(self.client.get(held.get_absolute_url()).status_code, 200)
        self.client.post(self.url, {"action": "decline", "project_id": gone.pk, "reason": "not hardware"})
        self.assertFalse(Project.objects.filter(pk=gone.pk).exists())


@override_settings(ZENODO_DEFAULT_COMMUNITY="osprey")
class ConversionTests(FakeZenodoMixin, TestCase):
    def test_registering_an_indexed_zenodo_entry_converts_it_in_place(self):
        owner = get_user_model().objects.create_user(username="owner")
        add_orcid_account(owner, OWNER_ORCID)
        rec = self.fz.seed_published(RECORD, files=[("pump-v1.zip", 4096)])
        entry = service.index_record(service.resolve(rec.doi).record)
        slug, pk = entry.slug, entry.pk
        child = Project.objects.create(slug="child", title="Child", artifact_type="Hardware", field="Oceanography", license="MIT", visibility=Project.VISIBILITY_PUBLIC, created_by=owner)
        edge, err = lineage.declare(child, entry.slug, "derived_from", owner)
        self.assertIsNone(err, err)

        project = zenodo_register.register_record(rec.doi, owner)

        self.assertEqual(project.pk, pk)
        self.assertEqual(project.slug, slug)
        self.assertEqual(project.origin, Project.ORIGIN_REGISTERED)
        self.assertEqual(project.created_by, owner)
        self.assertTrue(project.is_public)
        self.assertEqual(project.deposits.count(), 1)
        self.assertEqual(project.deposits.first().concept_id, str(rec.conceptrecid))
        self.assertEqual(project.deposits.first().versions.count(), 1)
        owner_row = project.contributions.get(orcid_id=OWNER_ORCID)
        self.assertEqual(owner_row.user, owner)
        self.assertEqual(owner_row.claim_status, "verified")
        self.assertEqual(project.contributions.count(), 3)
        edge.refresh_from_db()
        self.assertEqual(edge.parent_id, pk)
        self.assertEqual(edge.status, LineageEdge.STATUS_ACTIVE)
        self.assertContains(self.client.get(project.get_absolute_url()), "Cite this project")
        # Registering again is refused as a duplicate, as for any registered project.
        with self.assertRaises(zenodo_register.RegistrationError):
            zenodo_register.register_record(rec.doi, owner)


@override_settings(ZENODO_DEFAULT_COMMUNITY="osprey")
class PublicSubmitTests(FakeJournalsMixin, FakeGitHubMixin, FakeZenodoMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(username="staff", is_staff=True)
        self.user = get_user_model().objects.create_user(username="suggester")
        self.url = reverse("projects:index_submit")

    def test_signed_in_only_and_linked_from_the_submit_page(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertContains(self.client.get(reverse("projects:new")), self.url)

    def test_preview_then_submit_lists_live_holds_the_rest_and_tells_staff(self):
        self.gh.seed_repo("acme/pump", description="A pump.", license="MIT")
        self.gh.seed_repo("acme/bare", license="")
        self.client.force_login(self.user)
        lines = "https://github.com/acme/pump\nhttps://github.com/acme/bare\n10.1000/other.2026.1\nhttps://example.org/somewhere\n"
        response = self.client.post(self.url, {"action": "preview", "lines": lines})
        # Rows carry no verdicts; submitters never learn which would pass the gates.
        self.assertNotContains(response, "Goes to staff for review.")
        self.assertContains(response, "can't read this source yet", count=2)
        self.assertNotContains(response, "Goes live")
        self.assertNotContains(response, "no license file")
        self.assertEqual(Project.objects.filter(origin=Project.ORIGIN_INDEXED).count(), 0)

        # Step two: the unsupported lines need title, license and files from the submitter.
        response = self.client.post(self.url, {"action": "submit", "lines": lines}, follow=True)
        self.assertContains(response, "Title is required.")
        self.assertEqual(Project.objects.filter(origin=Project.ORIGIN_INDEXED).count(), 0)
        response = self.client.post(
            self.url,
            {
                "action": "submit", "lines": lines,
                "summary_0": "A pump I have built twice.",
                "title_2": "Resistance welding rig", "license_2": "CERN-OHL-S-2.0",
                "files_url_2": "https://doi.org/10.17632/8tb37yjp9m.3", "summary_2": "Welds thermoplastic composites.",
                "title_3": "Somewhere pump", "license_3": "MIT", "files_url_3": "https://example.org/somewhere/files",
                "authors_0": "Ada Lovelace, Grace Hopper",
                "authors_2": "Jonas Frank Reis; Luis Rogerio de Oliveira Hein",
            },
            follow=True,
        )
        self.assertContains(response, "Sent 4 to staff for review. You&#x27;ll hear back in your inbox.")
        live = Project.objects.get(source="github", external_id="acme/pump")
        self.assertEqual(live.listed_by, self.user)
        self.assertEqual(live.index_state, Project.INDEX_HELD)  # public submissions always wait for staff
        self.assertEqual(self.client.get(live.get_absolute_url()).status_code, 404)
        held = Project.objects.filter(origin=Project.ORIGIN_INDEXED, index_state=Project.INDEX_HELD)
        self.assertEqual(held.count(), 4)
        self.assertEqual(set(held.values_list("source", flat=True)), {"github", "other"})
        request_row = held.get(external_id="10.1000/other.2026.1")
        self.assertEqual(request_row.canonical_url, "https://doi.org/10.1000/other.2026.1")
        self.assertEqual(request_row.listed_by, self.user)
        self.assertEqual(request_row.title, "Resistance welding rig")
        self.assertEqual(request_row.license, "CERN-OHL-S-2.0")
        self.assertEqual(request_row.files_url_source, "submitter")
        self.assertEqual(request_row.gate_license["where"], "chosen by @suggester; not checked against the source")
        self.assertEqual(request_row.source_metadata["submitted"]["by"], "suggester")
        self.assertEqual(list(request_row.contributions.values_list("display_name", flat=True)), ["Jonas Frank Reis", "Luis Rogerio de Oliveira Hein"])
        # GitHub authors stay the repository owner, whatever was typed.
        self.assertEqual(list(live.contributions.values_list("display_name", flat=True)), ["acme"])
        self.assertNotIn("authors", live.source_metadata["submitted"])
        self.assertEqual(live.summary, "A pump I have built twice.")
        self.assertEqual(live.source_metadata["submitted"]["summary"], "A pump I have built twice.")
        self.client.force_login(self.staff)
        queue = self.client.get(reverse("index_staff"))
        self.assertContains(queue, "chosen by @suggester")
        notice = Notification.objects.get(user=self.staff, kind="index_run")
        self.assertEqual(notice.title, "Index submission from @suggester: 4 to review")
        # Staff approve the gate-passing one with no edits; it keeps the source's license.
        self.client.force_login(self.staff)
        self.client.post(reverse("index_staff"), {"action": "approve", "project_id": live.pk})
        live.refresh_from_db()
        self.assertEqual(live.index_state, Project.INDEX_LIVE)
        self.assertEqual(live.license, "MIT")
        self.assertContains(self.client.get(live.get_absolute_url()), "Listed by")
        self.client.force_login(self.user)
        self.assertEqual(
            list(Notification.objects.filter(user=self.user).values_list("title", flat=True)),
            ["Added to the index: pump"],
        )

    def test_zenodo_and_github_authors_are_not_editable(self):
        rec = self.fz.seed_published(RECORD, files=[("a.zip", 10)])
        self.gh.seed_repo("acme/pump", license="MIT")
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"action": "preview", "lines": f"{rec.doi}\ngithub.com/acme/pump"})
        self.assertContains(response, "Authors: Job Owner, Co Author, Person Nameonly")
        self.assertContains(response, "Authors: acme")
        self.assertNotContains(response, 'name="authors_')
        self.client.post(self.url, {"action": "submit", "lines": rec.doi, "authors_0": "Someone Else"})
        entry = Project.objects.get(source="zenodo")
        self.assertEqual(entry.contributions.count(), 3)
        self.assertEqual(entry.contributions.filter(orcid_id=OWNER_ORCID).count(), 1)

    def test_start_over_keeps_the_lines_in_the_box(self):
        self.gh.seed_repo("acme/pump", license="MIT")
        self.client.force_login(self.user)
        lines = "github.com/acme/pump\nhttps://example.org/x"
        response = self.client.post(self.url, {"action": "edit", "lines": lines})
        self.assertContains(response, "<textarea")
        self.assertContains(response, "github.com/acme/pump\nhttps://example.org/x")
        self.assertNotContains(response, 'name="action" value="submit"')

    def test_suggested_image_is_fetched_and_stored_or_dropped(self):
        import tempfile
        from django.test import override_settings as _os

        self.gh.seed_repo("acme/pump", license="MIT")
        self.client.force_login(self.user)
        with _os(MEDIA_ROOT=tempfile.mkdtemp(prefix="osprey-index-img-")):
            self.client.post(self.url, {
                "action": "submit",
                "lines": "github.com/acme/pump\nhttps://example.org/rig",
                "image_url_0": f"{self.gh.url}/image.png",
                "title_1": "Rig", "license_1": "MIT", "files_url_1": "https://example.org/rig/files",
                "image_url_1": f"{self.gh.url}/not-an-image",
            })
            pump = Project.objects.get(external_id="acme/pump")
            self.assertTrue(pump.cover_image.name.endswith(".webp"))
            self.assertEqual(pump.cover_image_url, "")
            self.assertEqual(pump.source_metadata["submitted"]["image_url"], f"{self.gh.url}/image.png")
            rig = Project.objects.get(title="Rig")
            self.assertFalse(rig.cover_image)
            self.assertEqual(rig.cover_image_url, "")
            self.client.force_login(self.staff)
            queue = self.client.get(reverse("index_staff"))
            self.assertContains(queue, "Suggested cover")
            self.assertContains(queue, pump.cover_image.url)

    def test_submitter_hears_the_decision(self):
        self.gh.seed_repo("acme/bare", license="")
        self.gh.seed_repo("acme/other", license="")
        self.client.force_login(self.user)
        self.client.post(self.url, {"action": "submit", "lines": "github.com/acme/bare\ngithub.com/acme/other"})
        bare = Project.objects.get(external_id="acme/bare")
        other = Project.objects.get(external_id="acme/other")
        self.client.force_login(self.staff)
        self.client.post(reverse("index_staff"), {"action": "approve", "project_id": bare.pk, "license": "MIT"})
        self.client.post(reverse("index_staff"), {"action": "decline", "project_id": other.pk, "reason": "Not research hardware."})
        titles = list(Notification.objects.filter(user=self.user, kind="index_decision").order_by("id").values_list("title", "body"))
        self.assertEqual(titles[0][0], "Added to the index: bare")
        self.assertEqual(titles[1], ("Not added to the index: other", "Not research hardware."))

    @override_settings(INDEX_SUBMIT_MAX_LINES=2)
    def test_lines_are_capped_per_submission(self):
        for name in ("a", "b", "c"):
            self.gh.seed_repo(f"acme/{name}", license="MIT")
        self.client.force_login(self.user)
        self.client.post(self.url, {"action": "submit", "lines": "github.com/acme/a\ngithub.com/acme/b\ngithub.com/acme/c"})
        self.assertEqual(Project.objects.filter(origin=Project.ORIGIN_INDEXED).count(), 2)

    @override_settings(RATELIMIT_ENABLE=True, RATELIMIT_INDEX_SUBMIT="1/d")
    def test_daily_limit(self):
        self.gh.seed_repo("acme/a", license="MIT")
        self.gh.seed_repo("acme/b", license="MIT")
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(self.url, {"action": "submit", "lines": "github.com/acme/a"}).status_code, 302)
        self.assertEqual(self.client.post(self.url, {"action": "submit", "lines": "github.com/acme/b"}).status_code, 403)

    def test_resubmitting_reports_exists_and_makes_nothing(self):
        self.gh.seed_repo("acme/pump", license="MIT")
        self.client.force_login(self.user)
        self.client.post(self.url, {"action": "submit", "lines": "github.com/acme/pump"})
        response = self.client.post(self.url, {"action": "submit", "lines": "github.com/acme/pump"}, follow=True)
        self.assertContains(response, "Nothing new to add.")
        self.assertEqual(Project.objects.filter(origin=Project.ORIGIN_INDEXED).count(), 1)


class JournalAdapterTests(FakeJournalsMixin, FakeGitHubMixin, TestCase):
    def test_hardwarex_reads_spec_table_abstract_and_authors(self):
        from projects.indexing import hardwarex_source

        self.fj.seed_hardwarex_fixture()
        rec = hardwarex_source.fetch(HARDWAREX_DOI)
        self.assertEqual(rec.title, "Open-source modular resistance welding equipment for thermoplastic composites")
        self.assertEqual(rec.license, "CERN-OHL-S-2.0")
        self.assertTrue(rec.gate_license.ok)
        self.assertEqual(rec.files_url, "http://doi.org/10.17632/8tb37yjp9m.3")
        self.assertEqual(rec.files_url_source, "spec_table")
        self.assertTrue(rec.gate_files.ok)
        self.assertEqual(rec.oshwa_uid, "BR000022")
        self.assertTrue(rec.readme.startswith("This paper presents"))
        self.assertEqual(rec.authors[0].name, "Jonas Frank Reis")
        self.assertEqual(rec.authors[0].orcid, "0000-0002-4540-4098")
        self.assertEqual(len(rec.authors), 6)
        self.assertEqual(rec.published_on.year, 2026)
        self.assertEqual(rec.raw["pmcid"], "PMC13594966")
        self.assertIn("Resistance welding", rec.keywords)
        p = service.resolve(f"https://doi.org/{HARDWAREX_DOI}")
        self.assertEqual(p.outcome, service.LIVE, p.message)

    def test_declared_license_is_checked_against_a_linked_github_repo(self):
        fulltext = (
            '<article><front><article-meta><abstract><p>A rig.</p></abstract></article-meta></front>'
            '<body><table-wrap><table><tbody>'
            '<tr><td>Open source license</td><td>MIT</td></tr>'
            '<tr><td>Source file repository</td><td>https://github.com/acme/rig</td></tr>'
            '</tbody></table></table-wrap></body></article>'
        )
        self.fj.seed_crossref_work("10.1016/j.ohx.2025.e00002", title="Rig", authors=[], year=2025, pmcid="PMC1", fulltext_xml=fulltext)
        # Repo says GPL: the declared MIT can't be trusted, hold it with both named.
        self.gh.seed_repo("acme/rig", license="GPL-3.0")
        p = service.resolve("10.1016/j.ohx.2025.e00002")
        self.assertEqual(p.outcome, service.HELD)
        self.assertEqual(p.record.license, "")
        self.assertIn("declared MIT but the linked github says GPL-3.0", p.record.gate_license.found)
        # Repo agrees: live, license MIT.
        self.gh.seed_repo("acme/rig", license="MIT")
        p = service.resolve("10.1016/j.ohx.2025.e00002")
        self.assertEqual(p.outcome, service.LIVE, p.message)
        self.assertEqual(p.record.license, "MIT")
        # Repo has no license file: keep the declaration but ask for a confirmation.
        self.gh.seed_repo("acme/rig", license="")
        p = service.resolve("10.1016/j.ohx.2025.e00002")
        self.assertEqual(p.outcome, service.HELD)
        self.assertTrue(p.record.gate_license.ok)
        self.assertTrue(p.record.gate_license.confirm)
        self.assertEqual(p.record.license, "MIT")

    def test_hardwarex_without_europepmc_full_text_is_held(self):
        self.fj.seed_crossref_work("10.1016/j.ohx.2025.e00001", title="Unlisted rig", authors=[{"given": "A", "family": "Person"}])
        p = service.resolve("10.1016/j.ohx.2025.e00001")
        self.assertEqual(p.outcome, service.HELD)
        self.assertIn("not in Europe PMC", p.record.gate_license.found)
        self.assertEqual(p.record.title, "Unlisted rig")

    def test_hardwarex_retries_through_a_503(self):
        self.fj.seed_hardwarex_fixture()
        self.fj.fail_next("/epmc/PMC13594966/fullTextXML", [503, 503])
        p = service.resolve(HARDWAREX_DOI)
        self.assertEqual(p.outcome, service.LIVE, p.message)
        self.assertEqual(sum(1 for r in self.fj.requests if "fullTextXML" in r), 3)

    def test_hardwarex_list_new(self):
        from projects.indexing import hardwarex_source
        from datetime import date

        self.fj.seed_hardwarex_fixture()
        self.fj.seed_crossref_work("10.1016/j.ohx.2020.e00100", title="Old", authors=[], year=2020)
        self.assertEqual(hardwarex_source.list_new(date(2026, 1, 1)), [HARDWAREX_DOI])
        self.assertEqual(set(hardwarex_source.list_new(date(2019, 1, 1))), {HARDWAREX_DOI, "10.1016/j.ohx.2020.e00100"})

    def test_joh_reads_the_feed_and_is_always_held(self):
        from projects.indexing import joh_source

        self.fj.seed_joh_fixture()
        rec = joh_source.fetch(JOH_DOI)
        self.assertTrue(rec.title.startswith("A 6 Degree of Freedom Spark Assisted"))
        self.assertEqual([a.name for a in rec.authors], ["Zhaohan Zheng", "Rolf Wüthrich"])
        self.assertTrue(rec.readme.startswith("Spark Assisted Chemical Engraving"))
        self.assertEqual(rec.text_license, "CC-BY-4.0")
        self.assertEqual(rec.license, "")
        self.assertEqual(rec.files_url, "https://zenodo.org/records/19053838")
        self.assertEqual(rec.files_url_source, "body_scan")
        self.assertNotIn("token=", " ".join(rec.raw["candidate_links"]))
        self.assertIn("https://github.com/EGE-Group-Concordia-University/SACE_setup.git", rec.raw["candidate_links"])
        self.assertEqual(rec.published_on.isoformat(), "2026-09-07")
        p = service.resolve(JOH_DOI)
        self.assertEqual(p.outcome, service.HELD)
        self.assertIn("no hardware license", p.message)
        self.assertTrue(p.record.gate_files.ok)
        self.assertTrue(p.record.gate_files.confirm)
        # The harvest is cached: a second DOI lookup doesn't refetch the feed.
        before = len(self.fj.requests)
        service.resolve(JOH_DOI)
        self.assertEqual(len(self.fj.requests), before)

    def test_joh_license_comes_from_the_linked_repository_when_it_has_one(self):
        self.fj.seed_joh_fixture()
        self.gh.seed_repo("EGE-Group-Concordia-University/SACE_setup", license="GPL-3.0", description="SACE machining center files")
        p = service.resolve(JOH_DOI)
        self.assertEqual(p.outcome, service.HELD)  # the body-scanned link still needs a human yes
        self.assertEqual(p.record.license, "GPL-3.0")
        self.assertTrue(p.record.gate_license.ok)
        self.assertIn("from linked github", p.record.gate_license.found)
        self.assertEqual(p.record.files_url, "https://github.com/EGE-Group-Concordia-University/SACE_setup")
        self.assertTrue(p.record.gate_files.confirm)
        entry = service.index_record(p.record, hold=True)
        self.assertEqual(service.review_state(entry), "confirm")
        self.assertEqual(entry.license, "GPL-3.0")

    def test_joh_unknown_doi_and_list_new(self):
        from projects.indexing import joh_source
        from datetime import date

        self.fj.seed_joh_fixture()
        p = service.resolve("10.5206/joh.v1i1.999")
        self.assertEqual(p.outcome, service.ERROR)
        self.assertIn("isn't in the Journal of Open Hardware feed", p.message)
        self.assertEqual(joh_source.list_new(date(2026, 9, 1)), [JOH_DOI])
        self.assertEqual(joh_source.list_new(date(2026, 9, 30)), [])


@override_settings(ZENODO_DEFAULT_COMMUNITY="osprey")
class StaffJournalFlowTests(FakeJournalsMixin, FakeGitHubMixin, FakeZenodoMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(username="staff", is_staff=True)
        self.user = get_user_model().objects.create_user(username="suggester")
        self.url = reverse("index_staff")

    def test_fetch_new_previews_only_unseen_articles(self):
        self.fj.seed_hardwarex_fixture()
        self.fj.seed_crossref_work("10.1016/j.ohx.2020.e00100", title="Old rig", authors=[], year=2020)
        self.client.force_login(self.staff)
        response = self.client.post(self.url, {"action": "fetch_new", "source": "hardwarex"})
        self.assertContains(response, "from hardwarex since 2017-01-01")
        self.assertContains(response, "Old rig")
        self.assertContains(response, "resistance welding")
        # Import the live one, then fetch again: only the old one comes back, and only newer-than-newest is asked for.
        self.client.post(self.url, {"action": "import", "lines": f"{HARDWAREX_DOI}\n10.1016/j.ohx.2020.e00100", "import": [HARDWAREX_DOI]})
        self.assertTrue(Project.objects.filter(external_id=HARDWAREX_DOI, index_state=Project.INDEX_LIVE).exists())
        response = self.client.post(self.url, {"action": "fetch_new", "source": "hardwarex"}, follow=True)
        self.assertContains(response, "Nothing new from hardwarex since 2026-12-01")

    def test_public_journal_request_is_rechecked_into_a_real_entry(self):
        self.fj.seed_hardwarex_fixture()
        self.client.force_login(self.user)
        # Simulate a request row that arrived before the adapter existed.
        preview = service.Preview(line=HARDWAREX_DOI, source="hardwarex", external_id=HARDWAREX_DOI, outcome=service.UNSUPPORTED)
        row = service.request_row(preview, listed_by=self.user, submitted=service.Submitted(title="typed title", license="MIT", files_url="https://example.org/f"))
        self.client.force_login(self.staff)
        response = self.client.post(self.url, {"action": "recheck", "project_id": row.pk}, follow=True)
        self.assertContains(response, "Still held for your approval")
        row.refresh_from_db()
        self.assertEqual(row.index_state, Project.INDEX_HELD)
        self.assertTrue(row.title.startswith("Open-source modular resistance welding"))
        self.assertEqual(row.license, "CERN-OHL-S-2.0")
        self.assertEqual(row.files_url_source, "spec_table")
        self.assertEqual(row.listed_by, self.user)
        self.assertEqual(row.contributions.count(), 6)
        self.assertEqual(Project.objects.filter(external_id=HARDWAREX_DOI).count(), 1)

    def test_joh_batch_lands_held_for_the_manual_pass(self):
        self.fj.seed_joh_fixture()
        self.client.force_login(self.staff)
        response = self.client.post(self.url, {"action": "fetch_new", "source": "joh"})
        self.assertContains(response, "Spark Assisted")
        self.client.post(self.url, {"action": "import", "lines": JOH_DOI, "import": [JOH_DOI]})
        entry = Project.objects.get(external_id=JOH_DOI)
        self.assertEqual(entry.index_state, Project.INDEX_HELD)
        self.assertEqual(entry.files_url, "https://zenodo.org/records/19053838")
        queue = self.client.get(self.url)
        self.assertContains(queue, "confirm it is the project&#x27;s own")
        # No repo license could be read (nothing seeded), so it failed the license check.
        self.assertContains(queue, "Failed a check")
        self.assertContains(queue, "no hardware license in structured data")


class IndexJournalCommandTests(FakeJournalsMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.staff = get_user_model().objects.create_user(username="staff", is_staff=True)

    def _run(self, *args, **kwargs):
        from io import StringIO
        from django.core.management import call_command

        out, err = StringIO(), StringIO()
        call_command("index_journal", *args, stdout=out, stderr=err, pause=0, **kwargs)
        return out.getvalue(), err.getvalue()

    def test_sweep_creates_live_and_held_and_is_resumable(self):
        self.fj.seed_hardwarex_fixture()
        self.fj.seed_crossref_work("10.1016/j.ohx.2025.e00001", title="Unlisted rig", authors=[], year=2025)
        out, err = self._run("hardwarex", since="2017-01-01")
        self.assertIn("2 listed since 2017-01-01, 2 new", out)
        self.assertIn("done: live 1, held 1, errors 0", out)
        self.assertEqual(Project.objects.filter(source="hardwarex", index_state="live").count(), 1)
        self.assertEqual(Project.objects.filter(source="hardwarex", index_state="held").count(), 1)
        self.assertEqual(Notification.objects.filter(user=self.staff, kind="index_run").count(), 1)
        self.assertEqual(Notification.objects.filter(user=self.staff, kind="index_run").get().title, "Indexed 1 live, 1 held")
        # Rerun: nothing new, no second notice.
        out, err = self._run("hardwarex", since="2017-01-01")
        self.assertIn("0 new", out)
        self.assertEqual(Notification.objects.filter(kind="index_run").count(), 1)

    def test_dry_run_and_limit_create_nothing_or_less(self):
        self.fj.seed_hardwarex_fixture()
        self.fj.seed_crossref_work("10.1016/j.ohx.2025.e00001", title="Unlisted rig", authors=[], year=2025)
        out, _ = self._run("hardwarex", since="2017-01-01", dry_run=True)
        self.assertIn("(dry run)", out)
        self.assertIn("live  10.1016/j.ohx.2026.e00839", out)
        self.assertEqual(Project.objects.filter(source="hardwarex").count(), 0)
        self._run("hardwarex", since="2017-01-01", limit=1)
        self.assertEqual(Project.objects.filter(source="hardwarex").count(), 1)

    def test_joh_sweep_is_all_held(self):
        self.fj.seed_joh_fixture()
        out, _ = self._run("joh", since="2017-01-01")
        self.assertIn("done: live 0, held 1", out)
        self.assertEqual(Project.objects.get(source="joh").index_state, Project.INDEX_HELD)


@override_settings(ZENODO_DEFAULT_COMMUNITY="osprey")
class QueueGroupingTests(FakeJournalsMixin, FakeGitHubMixin, FakeZenodoMixin, TestCase):
    def test_queue_groups_by_what_staff_must_do(self):
        staff = get_user_model().objects.create_user(username="staff", is_staff=True)
        user = get_user_model().objects.create_user(username="suggester")
        self.gh.seed_repo("acme/pump", license="MIT", description="A pump.")
        self.gh.seed_repo("acme/bare", license="")
        ready = service.index_record(service.resolve("github.com/acme/pump").record, hold=True)
        failed = service.index_record(service.resolve("github.com/acme/bare").record)
        typed = service.request_row(
            service.Preview(line="https://example.org/rig", source="url", external_id="https://example.org/rig", outcome=service.UNSUPPORTED),
            listed_by=user, submitted=service.Submitted(title="Rig", license="MIT", files_url="https://example.org/rig/files"),
        )
        self.assertEqual(service.review_state(ready), "ready")
        self.assertEqual(service.review_state(failed), "failed")
        self.assertEqual(service.review_state(typed), "confirm")
        self.client.force_login(staff)
        page = self.client.get(reverse("index_staff"))
        self.assertContains(page, "1 ready, 1 to confirm, 1 failed a check")
        body = page.content.decode()
        self.assertLess(body.index("Ready to approve"), body.index("Needs a confirmation"))
        self.assertLess(body.index("Needs a confirmation"), body.index("Failed a check"))
        self.assertContains(page, "License: no license file")
        self.assertContains(page, "check-ok")
        self.assertContains(page, "check-confirm")
        self.assertContains(page, "check-fail")
