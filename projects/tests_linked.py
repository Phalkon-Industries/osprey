"""Linked projects: OSPREY projects backed by the authors' own Zenodo
record, driven through the fake Zenodo."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from notifications.models import Notification
from projects import zenodo_jobs, zenodo_link
from projects.models import Contribution, Project, ProjectDeposit, ProjectDepositVersion, ZenodoJob
from projects.testing.fake_zenodo import FakeZenodoMixin
from projects.tests import add_orcid_account
from projects.zenodo import ZenodoError

OWNER_ORCID = "0000-0001-2345-6789"
COAUTHOR_ORCID = "0000-0002-1111-2222"

RECORD = {
    "title": "Deep Sea Peristaltic Pump",
    "upload_type": "other",
    "description": "<p>A pump that <b>works</b> at depth.</p><p>Second paragraph.</p>",
    "creators": [
        {"name": "Owner, Job", "orcid": OWNER_ORCID, "affiliation": "WHOI"},
        {"name": "Author, Co", "orcid": COAUTHOR_ORCID},
        {"name": "Nameonly, Person"},
    ],
    "license": {"id": "cc-by-4.0"},
    "publication_date": "2026-03-04",
    "access_right": "open",
}


@override_settings(ZENODO_DEFAULT_COMMUNITY="osprey")
class LinkedProjectBase(FakeZenodoMixin, TestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.owner = User.objects.create_user(username="link-owner")
        add_orcid_account(self.owner, OWNER_ORCID)
        self.staff = User.objects.create_user(username="link-staff", is_staff=True)
        self.record = self.fz.seed_published(RECORD, files=[("pump-v1.zip", 4096)])

    def link(self, doi=None, user=None):
        return zenodo_link.link_project(doi or self.record.doi, user or self.owner)


class LinkProjectTests(LinkedProjectBase):
    def test_link_creates_a_public_project_from_the_record(self):
        project = self.link()
        self.assertEqual(project.origin, Project.ORIGIN_LINKED)
        self.assertEqual(project.visibility, Project.VISIBILITY_PUBLIC)
        self.assertEqual(project.title, "Deep Sea Peristaltic Pump")
        self.assertEqual(project.summary, "A pump that works at depth. Second paragraph.")
        self.assertEqual(project.license, "CC-BY-4.0")
        self.assertEqual(project.doi, self.record.conceptdoi)
        self.assertEqual(project.created_by, self.owner)
        self.assertFalse(project.accepts_publish)

        rows = list(project.contributions.order_by("order"))
        self.assertEqual([r.display_name for r in rows], ["Job Owner", "Co Author", "Person Nameonly"])
        self.assertEqual(rows[0].user, self.owner)
        self.assertEqual(rows[0].claim_status, Contribution.CLAIM_VERIFIED)
        self.assertEqual(rows[1].orcid_id, COAUTHOR_ORCID)
        self.assertIsNone(rows[1].user)
        self.assertEqual(rows[2].orcid_id, "")

        deposit = ProjectDeposit.objects.get(project=project)
        self.assertFalse(deposit.managed)
        self.assertEqual(deposit.state, ProjectDeposit.STATE_PUBLISHED)
        self.assertEqual(deposit.record_id, str(self.record.id))
        self.assertEqual(deposit.concept_id, str(self.record.conceptrecid))
        self.assertEqual(deposit.doi, self.record.doi)
        self.assertIsNotNone(deposit.zenodo_synced_at)
        versions = list(ProjectDepositVersion.objects.filter(deposit=deposit))
        self.assertEqual([v.version_index for v in versions], [1])
        self.assertEqual(versions[0].doi, self.record.doi)
        # It is a new public OSPREY project: staff and the sitewide notice hear.
        self.assertTrue(Notification.objects.filter(user=self.staff, kind="project_published").exists())
        # Nothing was written to Zenodo.
        self.assertEqual([p for p in self.fz.paths("POST") + self.fz.paths("PUT")], [])

    def test_accepts_concept_doi_version_doi_and_pasted_url(self):
        for text in (self.record.conceptdoi, f"https://doi.org/{self.record.doi}", f"doi:{self.record.doi}"):
            with self.subTest(text=text):
                project = self.link(text)
                self.assertEqual(project.doi, self.record.conceptdoi)
                project.delete()

    def test_requires_the_submitter_among_the_creators(self):
        stranger = get_user_model().objects.create_user(username="stranger")
        add_orcid_account(stranger, "0000-0003-9999-0000")
        with self.assertRaises(zenodo_link.LinkError) as caught:
            self.link(user=stranger)
        self.assertIn("isn't listed on that record as a creator", str(caught.exception))
        self.assertEqual(Project.objects.count(), 0)

    def test_requires_orcid_sign_in(self):
        plain = get_user_model().objects.create_user(username="plain")
        with self.assertRaises(zenodo_link.LinkError) as caught:
            self.link(user=plain)
        self.assertIn("Sign in with ORCID", str(caught.exception))

    def test_refuses_duplicates_and_unknown_records(self):
        self.link()
        with self.assertRaises(zenodo_link.LinkError) as caught:
            self.link()
        self.assertIn("already on OSPREY", str(caught.exception))
        with self.assertRaises(zenodo_link.LinkError) as caught:
            self.link("10.5072/zenodo.123456789")
        self.assertIn("no record", str(caught.exception))

    def test_refuses_a_license_osprey_does_not_accept(self):
        nc = self.fz.seed_published({**RECORD, "license": {"id": "cc-by-nc-4.0"}})
        with self.assertRaises(zenodo_link.LinkError) as caught:
            self.link(nc.doi)
        self.assertIn("cc-by-nc-4.0", str(caught.exception))
        self.assertIn("isn't one OSPREY accepts", str(caught.exception))
        none = self.fz.seed_published({k: v for k, v in RECORD.items() if k != "license"})
        with self.assertRaises(zenodo_link.LinkError) as caught:
            self.link(none.doi)
        self.assertIn("no license", str(caught.exception))
        self.assertEqual(Project.objects.count(), 0)

    def test_publish_and_new_version_are_refused_for_linked(self):
        project = self.link()
        with self.assertRaises(ValueError):
            zenodo_jobs.enqueue_publish(project, self.owner)
        with self.assertRaises(ValueError):
            zenodo_jobs.enqueue_new_version(project, self.owner, changelog="x")


class RefreshTests(LinkedProjectBase):
    def test_refresh_picks_up_new_version_title_and_creator(self):
        project = self.link()
        v2 = self.fz.seed_published(
            {**RECORD, "title": "Deep Sea Peristaltic Pump, Mk II",
             "creators": RECORD["creators"] + [{"name": "New, Person", "orcid": "0000-0004-0000-0001"}]},
            concept=self.record.conceptrecid,
        )
        changes = zenodo_link.refresh_linked(project)
        project.refresh_from_db()
        self.assertEqual(changes["versions_added"], 1)
        self.assertEqual(changes["creators_added"], 1)
        self.assertIn("title", changes["fields"])
        self.assertEqual(project.title, "Deep Sea Peristaltic Pump, Mk II")
        deposit = ProjectDeposit.objects.get(project=project)
        self.assertEqual(deposit.record_id, str(v2.id))
        self.assertEqual(deposit.doi, v2.doi)
        self.assertEqual([v.version_index for v in deposit.versions.order_by("version_index")], [1, 2])
        self.assertEqual(project.contributions.count(), 4)
        # Idempotent.
        again = zenodo_link.refresh_linked(project)
        self.assertEqual((again["versions_added"], again["creators_added"], again["fields"]), (0, 0, []))

    def test_refresh_command_sweeps_linked_projects(self):
        project = self.link()
        self.fz.seed_published(RECORD, concept=self.record.conceptrecid)
        call_command("refresh_linked_projects")
        self.assertEqual(ProjectDeposit.objects.get(project=project).versions.count(), 2)


class CommunityTests(LinkedProjectBase):
    def test_community_requests_are_accepted_only_for_known_records(self):
        project = self.link()
        ours = self.fz.submit_to_community(self.record.id)
        other = self.fz.seed_published({**RECORD, "title": "Someone else's thing", "creators": [{"name": "Else, Someone"}]})
        theirs = self.fz.submit_to_community(other.id)
        self.assertEqual(zenodo_link.accept_community_requests(), 1)
        self.assertEqual(self.fz.community_requests[ours.id].status, "accepted")
        self.assertEqual(self.fz.community_requests[theirs.id].status, "submitted")
        self.assertIn("osprey", self.fz.deposition(self.record.id).communities)
        self.assertTrue(ProjectDeposit.objects.get(project=project).in_community)
        # Refresh reads membership from the record too.
        ProjectDeposit.objects.filter(project=project).update(in_community=None)
        zenodo_link.refresh_linked(project)
        self.assertTrue(ProjectDeposit.objects.get(project=project).in_community)
        call_command("accept_community_requests")  # nothing left; no error


class LinkedViewTests(LinkedProjectBase):
    def test_link_page_requires_login_and_creates_on_post(self):
        url = reverse("projects:link")
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.owner)
        response = self.client.get(url)
        self.assertContains(response, "Link your Zenodo record")
        response = self.client.post(url, {"doi": f"https://doi.org/{self.record.doi}"})
        project = Project.objects.get(origin=Project.ORIGIN_LINKED)
        self.assertRedirects(response, reverse("projects:edit", args=[project.slug]) + "?tab=basics")

    def test_link_page_shows_the_reason_when_it_cannot_link(self):
        stranger = get_user_model().objects.create_user(username="link-stranger")
        add_orcid_account(stranger, "0000-0003-9999-0000")
        self.client.force_login(stranger)
        response = self.client.post(reverse("projects:link"), {"doi": self.record.doi})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "isn&#x27;t listed on that record as a creator")
        self.assertEqual(Project.objects.count(), 0)

    def test_submit_page_points_at_linking(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("projects:new"))
        self.assertContains(response, reverse("projects:link"))

    def test_edit_form_shows_record_block_and_hides_files_and_publish(self):
        project = self.link()
        self.client.force_login(self.owner)
        response = self.client.get(reverse("projects:edit", args=[project.slug]))
        self.assertContains(response, "Your Zenodo record")
        self.assertContains(response, "Refresh from Zenodo")
        self.assertContains(response, "OSPREY community on Zenodo")
        self.assertContains(response, reverse("linked_projects_guide"))
        self.assertNotContains(response, 'data-form-tab="files"')
        self.assertNotContains(response, 'value="publish" data-publish-button')
        self.assertContains(response, 'name="title" value="Deep Sea Peristaltic Pump" readonly')
        self.assertContains(response, "Authors come from your Zenodo record")

    def test_saving_a_linked_project_keeps_zenodo_fields_and_touches_nothing_without_a_grant(self):
        project = self.link()
        self.client.force_login(self.owner)
        calls_before = len(self.fz.paths())
        data = {
            "title": "Renamed on OSPREY (ignored)",
            "summary": "Now with a maturity rating.",
            "readme": "# Pump",
            "artifact_type": "Hardware",
            "field": "Oceanography",
            "license_choice": "MIT",
            "self_rating": "6",
            "cover_image_focal_x": "50",
            "cover_image_focal_y": "50",
            "cover_image_zoom": "1",
            "action": "publish",  # ignored for linked projects
        }
        response = self.client.post(reverse("projects:edit", args=[project.slug]), data)
        self.assertEqual(response.status_code, 302)
        project.refresh_from_db()
        self.assertEqual(project.title, "Deep Sea Peristaltic Pump")
        self.assertEqual(project.license, "CC-BY-4.0")
        self.assertEqual(project.self_rating, 6)
        self.assertEqual(project.field, "Oceanography")
        self.assertEqual(len(self.fz.paths()), calls_before)
        self.assertEqual(ZenodoJob.objects.count(), 0)

    def test_refresh_view_and_new_version_refusal(self):
        project = self.link()
        self.client.force_login(self.owner)
        self.fz.seed_published(RECORD, concept=self.record.conceptrecid)
        response = self.client.post(reverse("projects:zenodo_refresh", args=[project.slug]), follow=True)
        self.assertContains(response, "Refreshed from Zenodo: 1 new version.")
        response = self.client.get(reverse("projects:zenodo_new_version", args=[project.slug]), follow=True)
        self.assertContains(response, "published on Zenodo")
        # Detail page carries the provenance line and no New version button.
        response = self.client.get(reverse("projects:detail", args=[project.slug]))
        self.assertContains(response, "Record managed on Zenodo by its authors. Registered on OSPREY by @link-owner.")
        # The about page renders and is linked from the link page.
        self.assertContains(self.client.get(reverse("linked_projects_guide")), "How linked projects work")
        self.assertNotContains(response, reverse("projects:zenodo_new_version", args=[project.slug]))
        self.assertContains(response, reverse("projects:edit", args=[project.slug]))
        # A stranger cannot refresh.
        stranger = get_user_model().objects.create_user(username="refresh-stranger")
        self.client.force_login(stranger)
        self.assertEqual(self.client.post(reverse("projects:zenodo_refresh", args=[project.slug])).status_code, 404)


# --- real Zenodo shapes, captured as fixtures -----------------------------------

import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402

from django.test import tag  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "testing" / "fixtures"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


class RealRecordShapeTests(TestCase):
    """The reader against JSON captured from zenodo.org, so drift in the
    real API shows up here before it shows up for a user."""

    def test_phrog_record_parses(self):
        record = _fixture("zenodo_record_phrog.json")
        creators = zenodo_link.creators_of(record)
        self.assertEqual(creators, [{"name": "Jonathan A. Pfeifer", "orcid": "0000-0002-6155-4846", "affiliation": ""}])
        self.assertEqual(zenodo_link.display_name(creators[0]["name"]), "Jonathan A. Pfeifer")
        self.assertEqual(zenodo_link.license_of(record), "CC-BY-4.0")
        self.assertEqual(zenodo_link.version_index(record), 0)
        self.assertEqual(str(record["conceptrecid"]), "22815611")
        self.assertEqual(zenodo_link._published_at(record).year, 2026)
        self.assertEqual(zenodo_link._version_label(record, 1), "v1")  # no metadata.version on the record
        self.assertIn({"id": "osprey"}, record["metadata"]["communities"])

    def test_multiversion_record_parses(self):
        record = _fixture("zenodo_record_multiversion.json")
        creators = zenodo_link.creators_of(record)
        self.assertEqual(len(creators), 2)
        self.assertTrue(all(c["orcid"] for c in creators))
        self.assertEqual(zenodo_link.display_name(creators[0]["name"]), "Guillaume Maze")
        self.assertEqual(zenodo_link.license_of(record), "eupl-1.2")  # unknown to OSPREY: the link would refuse it
        self.assertEqual(zenodo_link.version_index(record), 19)
        self.assertEqual(zenodo_link._version_label(record, 20), "v1.4.0")

    def test_versions_listing_is_newest_first_and_paginated(self):
        listing = _fixture("zenodo_versions_multiversion.json")
        hits = listing["hits"]["hits"]
        self.assertEqual(listing["hits"]["total"], 20)
        self.assertEqual(len(hits), 5)  # one page
        self.assertIn("next", listing["links"])
        indexes = [zenodo_link.version_index(h) for h in hits]
        self.assertEqual(indexes, sorted(indexes, reverse=True))
        # The reader sorts oldest first by Zenodo's own index.
        self.assertEqual([zenodo_link.version_index(h) for h in sorted(hits, key=zenodo_link.version_sort_key)], [15, 16, 17, 18, 19])


# --- more link edge cases ----------------------------------------------------------


class LinkEdgeCaseTests(LinkedProjectBase):
    def _versions(self, count):
        records = [self.record]
        for i in range(2, count + 1):
            records.append(self.fz.seed_published({**RECORD, "title": f"Pump v{i}"}, concept=self.record.conceptrecid))
        return records

    def test_version_doi_resolves_to_latest_and_versions_come_in_order(self):
        records = self._versions(3)
        project = self.link(records[1].doi)  # pasted the middle version
        deposit = ProjectDeposit.objects.get(project=project)
        self.assertEqual(deposit.record_id, str(records[2].id))
        self.assertEqual(deposit.doi, records[2].doi)
        self.assertEqual(project.title, "Pump v3")
        rows = list(deposit.versions.order_by("version_index"))
        self.assertEqual([r.version_index for r in rows], [1, 2, 3])
        self.assertEqual([r.record_id for r in rows], [str(r.id) for r in records])

    def test_more_versions_than_one_page(self):
        self.fz.max_page_size = 5  # force the reader through links.next
        records = self._versions(12)
        project = self.link(records[0].doi)
        deposit = ProjectDeposit.objects.get(project=project)
        self.assertEqual(deposit.versions.count(), 12)
        self.assertEqual(deposit.record_id, str(records[-1].id))
        listing_calls = [p for p in self.fz.paths("GET") if p.endswith("/versions")]
        self.assertGreaterEqual(len(listing_calls), 3)  # 12 versions, 5 per page

    def test_orcid_url_form_and_duplicate_creators(self):
        rec = self.fz.seed_published({**RECORD, "creators": [
            {"name": "Owner, Job", "orcid": f"https://orcid.org/{OWNER_ORCID}"},
            {"name": "Owner, Job", "orcid": OWNER_ORCID},  # listed twice
            {"name": "Given Family", "affiliation": None},  # no comma, no affiliation
            {"name": ""},  # empty name is skipped
        ]})
        project = self.link(rec.doi)
        rows = list(project.contributions.order_by("order"))
        self.assertEqual([r.display_name for r in rows], ["Job Owner", "Given Family"])
        self.assertEqual(rows[0].orcid_id, OWNER_ORCID)
        self.assertEqual(rows[0].user, self.owner)
        self.assertEqual(rows[1].affiliation, "")

    def test_restricted_records_are_refused(self):
        rec = self.fz.seed_published({**RECORD, "access_right": "restricted"})
        with self.assertRaises(zenodo_link.LinkError) as caught:
            self.link(rec.doi)
        self.assertIn("open-access", str(caught.exception))

    def test_summary_and_readme_come_from_the_description(self):
        long_text = "<p>" + "word " * 100 + "</p>"
        rec = self.fz.seed_published({**RECORD, "description": long_text})
        project = self.link(rec.doi)
        self.assertLessEqual(len(project.summary), 280)
        self.assertNotIn("<p>", project.readme)
        plain = self.fz.seed_published({**RECORD, "description": "Plain text.\n\nTwo paragraphs.",
                                        "creators": [{"name": "Owner, Job", "orcid": OWNER_ORCID}]})
        project.delete()
        project = self.link(plain.doi)
        self.assertEqual(project.readme, "Plain text.\n\nTwo paragraphs.")

    @override_settings(ZENODO_TIMEOUT_SECONDS=1)
    def test_outages_during_link_create_nothing(self):
        for mode, extra, expect in (
            ("status", {"status": 503}, "HTTP 503"),
            ("hang", {"delay_s": 2.5}, "Couldn't reach Zenodo"),
            ("drop", {}, "Couldn't reach Zenodo"),
            ("garbage", {"status": 200}, "isn't a Zenodo record"),
        ):
            with self.subTest(mode=mode):
                self.fz.add_rule(match=f"/api/records/{self.record.id}", mode=mode, **extra)
                with self.assertRaises(zenodo_link.LinkError) as caught:
                    self.link()
                self.assertIn(expect, str(caught.exception))
                self.assertEqual(Project.objects.count(), 0)
                self.assertEqual(Contribution.objects.count(), 0)


class RefreshEdgeCaseTests(LinkedProjectBase):
    def test_refresh_keeps_a_row_whose_creator_disappeared(self):
        project = self.link()
        self.fz.seed_published({**RECORD, "creators": [RECORD["creators"][0]]}, concept=self.record.conceptrecid)
        zenodo_link.refresh_linked(project)
        self.assertEqual(project.contributions.count(), 3)  # nothing removed

    @override_settings(ZENODO_TIMEOUT_SECONDS=1)
    def test_refresh_during_an_outage_changes_nothing(self):
        project = self.link()
        deposit = ProjectDeposit.objects.get(project=project)
        synced = deposit.zenodo_synced_at
        self.fz.add_rule(match="/api/records/", mode="hang", delay_s=2.5)
        with self.assertRaises(zenodo_link.LinkError):
            zenodo_link.refresh_linked(project)
        deposit.refresh_from_db()
        self.assertEqual(deposit.zenodo_synced_at, synced)

    def test_refresh_of_a_vanished_record(self):
        project = self.link()
        self.fz.add_rule(match="/api/records/", mode="status", status=404, times=5)
        with self.assertRaises(zenodo_link.LinkError) as caught:
            zenodo_link.refresh_linked(project)
        self.assertIn("no record", str(caught.exception))

    def test_refresh_command_continues_past_a_failing_record(self):
        good = self.link()
        other_owner = get_user_model().objects.create_user(username="other-owner")
        add_orcid_account(other_owner, "0000-0005-5555-5555")
        bad_record = self.fz.seed_published({**RECORD, "title": "Other", "creators": [{"name": "Other, Owner", "orcid": "0000-0005-5555-5555"}]})
        zenodo_link.link_project(bad_record.doi, other_owner)
        self.fz.add_rule(match=f"/api/records/{bad_record.conceptrecid}", mode="status", status=500, times=5)
        from io import StringIO
        out, err = StringIO(), StringIO()
        call_command("refresh_linked_projects", stdout=out, stderr=err)
        self.assertIn("refreshed 1, failed 1", out.getvalue())
        self.assertIn("HTTP 500", err.getvalue())
        self.assertIsNotNone(ProjectDeposit.objects.get(project=good).zenodo_synced_at)


class CommunityEdgeCaseTests(LinkedProjectBase):
    def test_poll_survives_an_outage(self):
        self.link()
        self.fz.add_rule(match="/api/requests", mode="status", status=502)
        self.assertEqual(zenodo_link.accept_community_requests(), 0)  # no exception

    def test_native_projects_records_are_accepted_too(self):
        native = Project.objects.create(slug="native-pump", title="Native", summary="x", artifact_type="Hardware",
                                        field="Oceanography", license="MIT", visibility=Project.VISIBILITY_PUBLIC, created_by=self.owner)
        Contribution.objects.create(project=native, display_name="Job Owner", role="Lead", orcid_id=OWNER_ORCID)
        ProjectDeposit.objects.create(project=native, deposition_id=str(self.record.id), record_id=str(self.record.id),
                                      concept_id=str(self.record.conceptrecid), doi=self.record.doi, state=ProjectDeposit.STATE_PUBLISHED)
        req = self.fz.submit_to_community(self.record.id)
        self.assertEqual(zenodo_link.accept_community_requests(), 1)
        self.assertEqual(self.fz.community_requests[req.id].status, "accepted")
        # Already-accepted requests are not touched again.
        self.assertEqual(zenodo_link.accept_community_requests(), 0)

    @override_settings(ZENODO_DEFAULT_COMMUNITY="")
    def test_no_community_configured_means_no_polling(self):
        self.link()
        self.fz.submit_to_community(self.record.id)
        self.assertEqual(zenodo_link.accept_community_requests(), 0)
        self.assertNotIn("/api/requests", self.fz.paths("GET"))


class LinkedInteractionsTests(LinkedProjectBase):
    def _post_data(self, project, **extra):
        data = {
            "summary": "Waits its turn.", "readme": "# x", "artifact_type": "Hardware", "field": "Oceanography",
            "license_choice": "MIT", "self_rating": "4", "cover_image_focal_x": "50", "cover_image_focal_y": "50",
            "cover_image_zoom": "1", "title": project.title, "action": "save",
        }
        data.update(extra)
        return data

    def test_linked_project_can_be_a_lineage_parent_and_child(self):
        from projects.models import LineageEdge

        linked = self.link()
        # A native draft derives from the linked project; the claim wakes on publish.
        draft = Project.objects.create(slug="derived-pump", title="Derived", summary="x", artifact_type="Hardware",
                                       field="Oceanography", license="MIT", visibility=Project.VISIBILITY_PRIVATE, created_by=self.owner)
        Contribution.objects.create(project=draft, display_name="Job Owner", role="Lead", orcid_id=OWNER_ORCID, user=self.owner)
        from django.core.files.base import ContentFile
        from projects.models import ProjectAttachment
        ProjectAttachment.objects.create(project=draft, file=ContentFile(b"PK\\x03\\x04", name="a.zip"), filename="a.zip", size_bytes=4)
        self.client.force_login(self.owner)
        data = self._post_data(draft, **{
            "contributions-TOTAL_FORMS": "1", "contributions-INITIAL_FORMS": "1", "contributions-MIN_NUM_FORMS": "1",
            "contributions-MAX_NUM_FORMS": "1000", "contributions-0-id": str(draft.contributions.get().pk),
            "contributions-0-display_name": "Job Owner", "contributions-0-role": "Lead", "contributions-0-orcid_id": OWNER_ORCID,
            "contributions-0-order": "0", "lineage_target": [linked.slug], "lineage_relation": ["derived_from"], "lineage_version": [""],
        })
        self.assertEqual(self.client.post(reverse("projects:edit", args=[draft.slug]), data).status_code, 302)
        edge = LineageEdge.objects.get(child=draft, parent=linked)
        self.assertIsNone(edge.claimed_at)
        zenodo_jobs.enqueue_publish(draft, self.owner)
        zenodo_jobs.run_due_jobs()
        edge.refresh_from_db()
        self.assertIsNotNone(edge.claimed_at)

        # The linked project (public already) uses the now-published native one: live at once.
        data = self._post_data(linked, lineage_target=[draft.slug], lineage_relation=["uses"], lineage_version=[""])
        self.assertEqual(self.client.post(reverse("projects:edit", args=[linked.slug]), data).status_code, 302)
        edge2 = LineageEdge.objects.get(child=linked, parent=draft)
        self.assertIsNotNone(edge2.claimed_at)

    def test_editor_sees_the_record_block_but_only_owner_or_staff_refreshes(self):
        project = self.link()
        editor = get_user_model().objects.create_user(username="linked-editor")
        add_orcid_account(editor, COAUTHOR_ORCID)
        row = project.contributions.get(orcid_id=COAUTHOR_ORCID)
        row.user = editor; row.claim_status = Contribution.CLAIM_VERIFIED; row.editor = True
        row.save()
        self.client.force_login(editor)
        response = self.client.get(reverse("projects:edit", args=[project.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Your Zenodo record")
        self.assertEqual(self.client.post(reverse("projects:zenodo_refresh", args=[project.slug])).status_code, 404)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(reverse("projects:zenodo_refresh", args=[project.slug])).status_code, 302)

    def test_link_notifies_coauthors_with_accounts_and_sitewide_subscribers(self):
        coauthor = get_user_model().objects.create_user(username="coauthor")
        add_orcid_account(coauthor, COAUTHOR_ORCID)
        reader = get_user_model().objects.create_user(username="reader")  # default weekly notices
        self.link()
        self.assertTrue(Notification.objects.filter(user=coauthor, kind="contributor_listed").exists())
        self.assertTrue(Notification.objects.filter(user=reader, kind="new_project_published").exists())

    def test_detail_citation_and_versions_page(self):
        records = LinkEdgeCaseTests._versions(self, 2)
        project = self.link(records[0].doi)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("projects:detail", args=[project.slug]))
        self.assertContains(response, "OSPREY; Zenodo.")
        self.assertContains(response, records[1].doi)  # latest version DOI in the citation
        self.assertContains(response, "(Version v2)")
        versions = self.client.get(reverse("projects:versions", args=[project.slug]))
        self.assertContains(versions, records[0].doi)
        self.assertContains(versions, records[1].doi)


@tag("live-zenodo")
class LiveLinkedReadTests(TestCase):
    """Read-only against production Zenodo: the reader still understands
    real records. Run by hand: RUN_LIVE_ZENODO=1 manage.py test --tag=live-zenodo"""

    def setUp(self):
        if os.environ.get("RUN_LIVE_ZENODO") != "1":
            self.skipTest("set RUN_LIVE_ZENODO=1 for live reads")

    def test_phrog_record_reads_from_production(self):
        reader = zenodo_link.ZenodoRecordReader(base_url="https://zenodo.org")
        record = zenodo_link.resolve_record("10.5281/zenodo.22815612", reader)
        self.assertEqual(str(record["conceptrecid"]), "22815611")
        creators = zenodo_link.creators_of(record)
        self.assertIn("0000-0002-6155-4846", [c["orcid"] for c in creators])
        self.assertEqual(zenodo_link.license_of(record), "CC-BY-4.0")
        versions = reader.versions(record["id"])
        self.assertGreaterEqual(len(versions), 1)
        self.assertEqual(versions[-1]["id"], record["id"])
