"""The Zenodo job queue, driven through the fake Zenodo.

Covers: the request enqueues instead of calling Zenodo; the runner
publishes, syncs, and versions; outages retry with backoff; unfixable
errors fail fast and tell the owner; the owner's page and the staff
page reflect all of it.
"""

from __future__ import annotations

import io
import shutil
import tempfile
import zipfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification
from projects import zenodo_jobs
from projects.models import (
    Contribution,
    Project,
    ProjectAttachment,
    ProjectDeposit,
    ProjectDepositVersion,
    ZenodoJob,
)
from projects.testing.fake_zenodo import FakeZenodoMixin
from projects.tests import add_orcid_account


def _zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("README.md", "# job")
    return buffer.getvalue()


class ZenodoJobBase(FakeZenodoMixin, TestCase):
    def setUp(self):
        super().setUp()
        self._media = tempfile.mkdtemp()
        media = override_settings(MEDIA_ROOT=self._media)
        media.enable()
        self.addCleanup(media.disable)
        self.addCleanup(shutil.rmtree, self._media, ignore_errors=True)
        User = get_user_model()
        self.owner = User.objects.create_user(username="job-owner")
        add_orcid_account(self.owner)
        self.staff = User.objects.create_user(username="job-staff", is_staff=True)
        self.project = self._draft("queued-pump")

    def _draft(self, slug: str) -> Project:
        project = Project.objects.create(
            slug=slug,
            title=slug.replace("-", " ").title(),
            summary="Waits its turn.",
            readme="# queued",
            artifact_type="Hardware",
            field="Oceanography",
            license="MIT",
            visibility=Project.VISIBILITY_PRIVATE,
            created_by=self.owner,
        )
        Contribution.objects.create(project=project, display_name="Job Owner", role="Project lead", orcid_id="0000-0001-2345-6789")
        payload = _zip()
        ProjectAttachment.objects.create(project=project, file=ContentFile(payload, name="a.zip"), filename="a.zip", size_bytes=len(payload))
        return project

    def _run(self):
        return zenodo_jobs.run_due_jobs()

    def _due_now(self, job):
        ZenodoJob.objects.filter(pk=job.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))


class PublishJobTests(ZenodoJobBase):
    def test_publish_runs_in_the_queue_not_the_request(self):
        job = zenodo_jobs.enqueue_publish(self.project, self.owner)
        self.assertEqual(job.status, ZenodoJob.STATUS_QUEUED)
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Project.VISIBILITY_PRIVATE)
        self.assertEqual(self.fz.paths(), [])  # nothing touched Zenodo yet

        self.assertEqual(self._run(), 1)

        job.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_DONE)
        self.assertEqual(self.project.visibility, Project.VISIBILITY_PUBLIC)
        self.assertTrue(self.project.doi.startswith("10.5072/zenodo."))
        deposit = ProjectDeposit.objects.get(project=self.project)
        self.assertEqual(deposit.state, ProjectDeposit.STATE_PUBLISHED)
        self.assertEqual(self.fz.deposition(deposit.deposition_id).state, "done")
        self.assertTrue(Notification.objects.filter(user=self.owner, kind="deposit_published").exists())
        self.assertTrue(Notification.objects.filter(user=self.staff, kind="project_published").exists())

    def test_enqueue_is_idempotent_while_pending(self):
        first = zenodo_jobs.enqueue_publish(self.project, self.owner)
        second = zenodo_jobs.enqueue_publish(self.project, self.owner)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ZenodoJob.objects.count(), 1)

    def test_outage_retries_with_backoff_and_keeps_the_draft(self):
        self.fz.add_rule(match="actions/publish", mode="status", status=503, times=1)
        job = zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._run()
        job.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_QUEUED)
        self.assertEqual(job.attempts, 1)
        self.assertIn("HTTP 503", job.last_error)
        self.assertGreater(job.next_attempt_at, timezone.now() + timedelta(seconds=30))
        self.assertEqual(self.project.visibility, Project.VISIBILITY_PRIVATE)
        self.assertFalse(Notification.objects.filter(kind="deposit_failed").exists())
        # Not due yet: the runner leaves it alone.
        self.assertEqual(self._run(), 0)
        # Due: second attempt succeeds.
        self._due_now(job)
        self.assertEqual(self._run(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_DONE)
        self.assertEqual(job.attempts, 2)
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Project.VISIBILITY_PUBLIC)

    @override_settings(ZENODO_TIMEOUT_SECONDS=0.3)
    def test_hang_counts_as_an_outage(self):
        self.fz.add_rule(match="actions/publish", mode="hang", delay_s=0.9)
        job = zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._run()
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_QUEUED)
        self.assertIn("timed out", job.last_error)

    def test_unfixable_error_fails_fast_and_tells_the_owner(self):
        # A 4xx is Zenodo saying the deposit itself is wrong; retrying
        # the same thing would not help, so the job fails on the spot.
        self.fz.add_rule(match="actions/publish", mode="status", status=400, message="Validation error.")
        job = zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._run()
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_FAILED)
        self.assertEqual(job.attempts, 1)
        self.assertIn("HTTP 400", job.last_error)
        note = Notification.objects.get(user=self.owner, kind="deposit_failed")
        self.assertIn("could not be published", note.title)
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Project.VISIBILITY_PRIVATE)

    def test_gives_up_after_max_attempts(self):
        self.fz.add_rule(match="actions/publish", mode="status", status=502, times=99)
        job = zenodo_jobs.enqueue_publish(self.project, self.owner)
        for _ in range(zenodo_jobs.MAX_ATTEMPTS):
            self._due_now(job)
            self._run()
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_FAILED)
        self.assertEqual(job.attempts, zenodo_jobs.MAX_ATTEMPTS)
        self.assertTrue(Notification.objects.filter(user=self.owner, kind="deposit_failed").exists())

    def test_retry_requeues_a_failed_job(self):
        self.fz.add_rule(match="actions/publish", mode="status", status=502, times=99)
        job = zenodo_jobs.enqueue_publish(self.project, self.owner)
        for _ in range(zenodo_jobs.MAX_ATTEMPTS):
            self._due_now(job)
            self._run()
        self.fz.rules.clear()
        zenodo_jobs.retry(job)
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_QUEUED)
        self._run()
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_DONE)

    def test_stale_running_job_is_handed_back_to_the_queue(self):
        job = zenodo_jobs.enqueue_publish(self.project, self.owner)
        ZenodoJob.objects.filter(pk=job.pk).update(
            status=ZenodoJob.STATUS_RUNNING,
            updated_at=timezone.now() - timedelta(hours=1),
        )
        self.assertEqual(self._run(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_DONE)

    @override_settings(ZENODO_JOBS_INLINE=True)
    def test_inline_mode_runs_at_enqueue(self):
        job = zenodo_jobs.enqueue_publish(self.project, self.owner)
        self.assertEqual(job.status, ZenodoJob.STATUS_DONE)
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Project.VISIBILITY_PUBLIC)

    def test_management_command_runs_due_jobs(self):
        zenodo_jobs.enqueue_publish(self.project, self.owner)
        call_command("run_zenodo_jobs")
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Project.VISIBILITY_PUBLIC)


class MetadataAndVersionJobTests(ZenodoJobBase):
    def _published(self) -> Project:
        zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._run()
        self.project.refresh_from_db()
        return self.project

    def test_metadata_sync_coalesces_and_updates_zenodo(self):
        project = self._published()
        project.title = "Queued Pump, renamed"
        project.save(update_fields=["title"])
        first = zenodo_jobs.enqueue_metadata_sync(project)
        second = zenodo_jobs.enqueue_metadata_sync(project)
        self.assertEqual(first.pk, second.pk)
        self._run()
        first.refresh_from_db()
        self.assertEqual(first.status, ZenodoJob.STATUS_DONE)
        deposit = ProjectDeposit.objects.get(project=project)
        self.assertEqual(self.fz.deposition(deposit.deposition_id).metadata["title"], "Queued Pump, renamed")
        # Syncs are invisible plumbing: no owner notification either way.
        self.assertFalse(Notification.objects.filter(kind__in=["deposit_published", "deposit_failed"], user=self.owner).count() > 1)

    def test_metadata_sync_keeps_the_original_publication_date(self):
        # A sync edits the record; it must not re-date it to the sync day.
        project = self._published()
        deposit = ProjectDeposit.objects.get(project=project)
        self.fz.deposition(deposit.deposition_id).metadata["publication_date"] = "2026-09-01"
        project.title = "Queued Pump, renamed"
        project.save(update_fields=["title"])
        zenodo_jobs.enqueue_metadata_sync(project)
        self._run()
        metadata = self.fz.deposition(deposit.deposition_id).metadata
        self.assertEqual(metadata["title"], "Queued Pump, renamed")
        self.assertEqual(metadata["publication_date"], "2026-09-01")

    def test_new_version_job_publishes_a_second_version(self):
        project = self._published()
        payload = _zip()
        ProjectAttachment.objects.create(project=project, file=ContentFile(payload, name="v2.zip"), filename="v2.zip", size_bytes=len(payload))
        job = zenodo_jobs.enqueue_new_version(project, self.owner, changelog="Second spin.")
        self._run()
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_DONE, job.last_error)
        deposit = ProjectDeposit.objects.get(project=project)
        self.assertEqual([v.version_index for v in ProjectDepositVersion.objects.filter(deposit=deposit).order_by("version_index")], [1, 2])
        self.assertTrue(Notification.objects.filter(user=self.owner, kind="deposit_published", title__startswith="New version").exists())

    def _second_version_after_failure(self, match):
        project = self._published()
        payload = _zip()
        ProjectAttachment.objects.create(project=project, file=ContentFile(payload, name="v2.zip"), filename="v2.zip", size_bytes=len(payload))
        self.fz.add_rule(match=match, mode="status", status=503, times=1)
        job = zenodo_jobs.enqueue_new_version(project, self.owner, changelog="Second spin.")
        self._run()
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_QUEUED, job.last_error)
        self._due_now(job)
        self._run()
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_DONE, job.last_error)
        deposit = ProjectDeposit.objects.get(project=project)
        versions = list(ProjectDepositVersion.objects.filter(deposit=deposit).order_by("version_index"))
        self.assertEqual([v.version_index for v in versions], [1, 2])
        self.assertEqual(versions[1].changelog, "Second spin.")
        self.assertEqual(deposit.state, ProjectDeposit.STATE_PUBLISHED)
        # One draft was opened on Zenodo, not one per attempt.
        self.assertEqual(len([p for p in self.fz.paths("POST") if p.endswith("/actions/newversion")]), 1)

    def test_new_version_resumes_after_a_failed_upload(self):
        self._second_version_after_failure("/api/files/")

    def test_new_version_resumes_after_a_failed_publish(self):
        self._second_version_after_failure("actions/publish")

    def test_health_summary(self):
        self._published()
        self.fz.add_rule(match="actions/edit", mode="status", status=500, times=1)
        zenodo_jobs.enqueue_metadata_sync(self.project)
        self._run()
        health = zenodo_jobs.health()
        self.assertIsNotNone(health["last_success_at"])
        self.assertEqual(health["recent_failures"], 1)
        self.assertEqual(health["queued"], 1)


class ZenodoQueueViewTests(ZenodoJobBase):
    def test_publish_post_enqueues_and_page_shows_pending(self):
        self.client.force_login(self.owner)
        contribution = self.project.contributions.get()
        data = {
            "title": self.project.title,
            "summary": "Waits its turn.",
            "readme": "# queued",
            "artifact_type": "Hardware",
            "field": "Oceanography",
            "license_choice": "MIT",
            "self_rating": "4",
            "cover_image_focal_x": "50",
            "cover_image_focal_y": "50",
            "cover_image_zoom": "1",
            "contributions-TOTAL_FORMS": "1",
            "contributions-INITIAL_FORMS": "1",
            "contributions-MIN_NUM_FORMS": "1",
            "contributions-MAX_NUM_FORMS": "1000",
            "contributions-0-id": str(contribution.pk),
            "contributions-0-display_name": "Job Owner",
            "contributions-0-role": "Project lead",
            "contributions-0-orcid_id": "0000-0001-2345-6789",
            "contributions-0-order": "0",
            "action": "publish",
        }
        response = self.client.post(reverse("projects:edit", args=[self.project.slug]), data, follow=True)
        self.assertContains(response, "OSPREY will mint the DOI")
        self.assertContains(response, "Publishing.")  # the owner's status block
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Project.VISIBILITY_PRIVATE)
        self.assertEqual(ZenodoJob.objects.filter(project=self.project, kind=ZenodoJob.KIND_PUBLISH).count(), 1)
        self.assertEqual(self.fz.paths(), [])
        # While queued, the edit form offers no second Publish.
        response = self.client.get(reverse("projects:edit", args=[self.project.slug]))
        self.assertContains(response, "Publishing in progress")
        self.assertNotContains(response, 'value="publish" data-publish-button')  # the JS selectors still mention it

    def test_failed_job_offers_try_again_to_owner_only(self):
        self.fz.add_rule(match="actions/publish", mode="status", status=400, message="Validation error.", times=1)
        zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._run()
        self.client.force_login(self.owner)
        response = self.client.get(reverse("projects:detail", args=[self.project.slug]))
        self.assertContains(response, "Publishing did not go through")
        self.assertContains(response, "Try again")
        # Retry requeues; the injected failure is spent, so it goes through.
        self.client.post(reverse("projects:zenodo_retry", args=[self.project.slug]))
        self.assertEqual(ZenodoJob.objects.get(project=self.project).status, ZenodoJob.STATUS_QUEUED)
        self._run()
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Project.VISIBILITY_PUBLIC)
        # A stranger sees no status block and cannot retry.
        stranger = get_user_model().objects.create_user(username="stranger")
        self.client.force_login(stranger)
        response = self.client.get(reverse("projects:detail", args=[self.project.slug]))
        self.assertNotContains(response, "zenodo-status")
        self.assertEqual(self.client.post(reverse("projects:zenodo_retry", args=[self.project.slug])).status_code, 404)

    def test_staff_queue_page_lists_and_retries(self):
        self.fz.add_rule(match="actions/publish", mode="status", status=502, times=99)
        job = zenodo_jobs.enqueue_publish(self.project, self.owner)
        for _ in range(zenodo_jobs.MAX_ATTEMPTS):
            self._due_now(job)
            self._run()
        self.client.force_login(self.staff)
        response = self.client.get(reverse("zenodo_jobs"))
        self.assertContains(response, "Zenodo queue")
        self.assertContains(response, "Failed")
        self.assertContains(response, "HTTP 502")
        self.fz.rules.clear()
        self.client.post(reverse("zenodo_jobs"), {"job_id": job.pk})
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_QUEUED)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse("zenodo_jobs")).status_code, 302)  # staff only

    def test_saving_a_public_project_queues_a_metadata_sync(self):
        zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._run()
        self.project.refresh_from_db()
        self.client.force_login(self.owner)
        contribution = self.project.contributions.get()
        data = {
            "title": "Queued Pump, edited",
            "summary": "Waits its turn.",
            "readme": "# queued",
            "artifact_type": "Hardware",
            "field": "Oceanography",
            "license_choice": "MIT",
            "self_rating": "4",
            "cover_image_focal_x": "50",
            "cover_image_focal_y": "50",
            "cover_image_zoom": "1",
            "contributions-TOTAL_FORMS": "1",
            "contributions-INITIAL_FORMS": "1",
            "contributions-MIN_NUM_FORMS": "1",
            "contributions-MAX_NUM_FORMS": "1000",
            "contributions-0-id": str(contribution.pk),
            "contributions-0-display_name": "Job Owner",
            "contributions-0-role": "Project lead",
            "contributions-0-orcid_id": "0000-0001-2345-6789",
            "contributions-0-order": "0",
            "action": "save",
        }
        calls_before = len(self.fz.paths())
        response = self.client.post(reverse("projects:edit", args=[self.project.slug]), data, follow=True)
        self.assertContains(response, "sync within a minute")
        self.assertContains(response, "Zenodo metadata sync pending")
        self.assertEqual(len(self.fz.paths()), calls_before)  # the save itself made no Zenodo calls
        self.assertEqual(ZenodoJob.objects.filter(kind=ZenodoJob.KIND_METADATA_SYNC).count(), 1)
        self._run()
        deposit = ProjectDeposit.objects.get(project=self.project)
        self.assertEqual(self.fz.deposition(deposit.deposition_id).metadata["title"], "Queued Pump, edited")



class RecordShapeTests(ZenodoJobBase):
    def test_metadata_carries_osprey_roles_and_honest_upload_type(self):
        from projects.zenodo import metadata_for_project

        metadata = metadata_for_project(self.project)
        self.assertEqual(metadata["upload_type"], "other")  # hardware design files
        self.assertEqual(
            metadata["contributors"],
            [
                {"name": "OSPREY", "type": "HostingInstitution"},
                {"name": "OSPREY", "type": "Distributor"},
            ],
        )

    def test_every_license_on_the_form_reaches_zenodo(self):
        # Every license the submission form offers must translate to a
        # Zenodo license id, or the record is deposited license-less and
        # Zenodo applies its own default.
        from projects.forms import COMMON_LICENSES
        from projects.zenodo import _zenodo_license

        for key, _label in COMMON_LICENSES:
            self.project.license = key
            with self.subTest(license=key):
                self.assertTrue(_zenodo_license(self.project), f"{key} has no Zenodo id")

    def test_citation_names_both_publishers(self):
        from projects.views import _build_bibtex, _build_citation_text

        zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._run()
        self.project.refresh_from_db()
        deposit = ProjectDeposit.objects.get(project=self.project)
        text = _build_citation_text(self.project, deposit)
        self.assertIn("Queued Pump (Version v1). OSPREY; Zenodo.", text)
        self.assertNotIn("[", text)
        self.assertIn(" OSPREY; Zenodo. https://doi.org/10.5072/zenodo.", text)
        self.assertNotIn("\u00b7", text)
        bib = _build_bibtex(self.project, deposit)
        self.assertIn("publisher = {OSPREY; Zenodo}", bib)
        self.assertIn("Record on OSPREY:", bib)

    def test_resync_command_queues_one_sync_per_published_project(self):
        zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._run()
        self._draft("still-a-draft")  # unpublished: not queued
        call_command("resync_zenodo_metadata")
        self.assertEqual(ZenodoJob.objects.filter(kind=ZenodoJob.KIND_METADATA_SYNC).count(), 1)
        self._run()
        deposit = ProjectDeposit.objects.get(project=self.project)
        fake_dep = self.fz.deposition(deposit.deposition_id)
        self.assertEqual(fake_dep.metadata["contributors"][0]["type"], "HostingInstitution")


class NoLicenseNeverReachesZenodoTests(ZenodoJobBase):
    # Zenodo applies CC BY 4.0 to an open record sent without a license,
    # so OSPREY refuses before making any call at all.
    def _published(self) -> Project:
        zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._run()
        self.project.refresh_from_db()
        return self.project

    def _assert_refused(self, job, calls_before):
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_FAILED)
        self.assertEqual(job.attempts, 1)
        self.assertIn("license", job.last_error)
        self.assertEqual(self.fz.paths()[calls_before:], [])

    def test_publish_without_a_license_sends_nothing(self):
        for license in ["", "WTFPL"]:
            with self.subTest(license=license):
                ZenodoJob.objects.all().delete()
                Project.objects.filter(pk=self.project.pk).update(license=license)
                calls_before = len(self.fz.paths())
                job = zenodo_jobs.enqueue_publish(self.project, self.owner)
                self._run()
                self._assert_refused(job, calls_before)
                self.assertTrue(Notification.objects.filter(user=self.owner, kind="deposit_failed").exists())
                self.assertFalse(ProjectDeposit.objects.filter(project=self.project).exclude(deposition_id="").exists())

    def test_metadata_sync_without_a_license_sends_nothing(self):
        project = self._published()
        deposit = ProjectDeposit.objects.get(project=project)
        Project.objects.filter(pk=project.pk).update(license="")
        calls_before = len(self.fz.paths())
        job = zenodo_jobs.enqueue_metadata_sync(project)
        self._run()
        self._assert_refused(job, calls_before)
        self.assertEqual(self.fz.deposition(deposit.deposition_id).metadata["license"], "mit-license")

    def test_new_version_without_a_license_sends_nothing(self):
        project = self._published()
        Project.objects.filter(pk=project.pk).update(license="WTFPL")
        calls_before = len(self.fz.paths())
        job = zenodo_jobs.enqueue_new_version(project, self.owner, changelog="Second spin.")
        self._run()
        self._assert_refused(job, calls_before)


@override_settings(ZENODO_TIMEOUT_SECONDS=0.3)
class LostResponseTests(ZenodoJobBase):
    # Zenodo did the work but OSPREY timed out before the answer came.
    # The fake's `hang` rule answers after the client has given up, so
    # the request still lands. A retry must finish the same record, never
    # make a second one.
    def _lose(self, match):
        self.fz.add_rule(match=match, mode="hang", delay_s=0.9)

    def _settle(self, job):
        """First attempt times out; wait for the fake to finish, then retry."""
        self._run()
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_QUEUED, job.last_error)
        self.assertIn("timed out", job.last_error)
        self.fz.wait_idle()  # the hung request lands after the client gave up
        self._due_now(job)
        self._run()
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_DONE, job.last_error)

    def _published_depositions(self):
        return [d for d in self.fz.depositions.values() if d.doi]

    def _first_publish(self):
        zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._run()
        self.project.refresh_from_db()
        payload = _zip()
        ProjectAttachment.objects.create(project=self.project, file=ContentFile(payload, name="v2.zip"), filename="v2.zip", size_bytes=len(payload))

    def test_lost_first_publish_makes_one_record(self):
        self._lose("actions/publish")
        job = zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._settle(job)
        self.assertEqual(len(self.fz.depositions), 1)
        self.assertEqual(len(self._published_depositions()), 1)
        deposit = ProjectDeposit.objects.get(project=self.project)
        self.assertEqual(deposit.state, ProjectDeposit.STATE_PUBLISHED)
        self.assertEqual(deposit.doi, self._published_depositions()[0].doi)
        self.assertEqual(ProjectDepositVersion.objects.filter(deposit=deposit).count(), 1)
        self.project.refresh_from_db()
        self.assertEqual(self.project.visibility, Project.VISIBILITY_PUBLIC)
        self.assertEqual(Notification.objects.filter(user=self.owner, kind="deposit_published").count(), 1)

    def test_failed_first_upload_reuses_the_draft(self):
        self.fz.add_rule(match="/api/files/", mode="status", status=503, times=1)
        job = zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._run()
        self._due_now(job)
        self._run()
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_DONE, job.last_error)
        self.assertEqual(len(self.fz.depositions), 1)

    def test_lost_new_version_draft_is_picked_up(self):
        self._first_publish()
        self._lose("actions/newversion")
        job = zenodo_jobs.enqueue_new_version(self.project, self.owner, changelog="Second spin.")
        self._settle(job)
        deposit = ProjectDeposit.objects.get(project=self.project)
        self.assertEqual(len(self.fz.depositions), 2)
        self.assertEqual(len(self._published_depositions()), 2)
        self.assertEqual([v.version_index for v in ProjectDepositVersion.objects.filter(deposit=deposit).order_by("version_index")], [1, 2])

    def test_lost_new_version_publish_records_the_version(self):
        self._first_publish()
        self._lose("actions/publish")
        job = zenodo_jobs.enqueue_new_version(self.project, self.owner, changelog="Second spin.")
        self._settle(job)
        deposit = ProjectDeposit.objects.get(project=self.project)
        self.assertEqual(deposit.state, ProjectDeposit.STATE_PUBLISHED)
        self.assertEqual(len(self._published_depositions()), 2)
        versions = list(ProjectDepositVersion.objects.filter(deposit=deposit).order_by("version_index"))
        self.assertEqual([v.version_index for v in versions], [1, 2])
        self.assertEqual(versions[1].changelog, "Second spin.")
        self.assertEqual(versions[1].doi, deposit.doi)
        self.assertEqual(Notification.objects.filter(user=self.owner, kind="deposit_published").count(), 2)


class MissingArchiveTests(ZenodoJobBase):
    def test_missing_archive_fails_at_once_with_a_clear_error(self):
        import os

        os.remove(self.project.attachments.first().file.path)
        job = zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._run()
        job.refresh_from_db()
        self.assertEqual(job.status, ZenodoJob.STATUS_FAILED)
        self.assertEqual(job.attempts, 1)
        self.assertIn("archive", job.last_error.lower())


class FailedNewVersionRecoveryTests(ZenodoJobBase):
    # A new version that failed for good must leave the owner a way out:
    # publish again with a new archive, or discard it and go back to the
    # last published version.
    def setUp(self):
        super().setUp()
        zenodo_jobs.enqueue_publish(self.project, self.owner)
        self._run()
        self.project.refresh_from_db()
        payload = _zip()
        ProjectAttachment.objects.create(project=self.project, file=ContentFile(payload, name="v2.zip"), filename="v2.zip", size_bytes=len(payload))
        self.fz.add_rule(match="/api/files/", mode="status", status=400, times=1)
        self.job = zenodo_jobs.enqueue_new_version(self.project, self.owner, changelog="Second spin.")
        self._run()
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ZenodoJob.STATUS_FAILED)
        self.deposit = ProjectDeposit.objects.get(project=self.project)
        self.v1 = self.deposit.latest_version
        self.client.force_login(self.owner)
        self.url = reverse("projects:zenodo_new_version", args=[self.project.slug])

    def test_new_version_page_opens_after_a_failure(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "v2.zip")
        self.assertContains(response, "Discard")

    def test_publish_again_with_a_new_archive(self):
        response = self.client.post(self.url, {
            "action": "publish", "changelog": "Second spin, fixed.", "license_choice": "MIT",
            "archive": SimpleUploadedFile("v2b.zip", _zip(), content_type="application/zip"),
        })
        self.assertEqual(response.status_code, 302)
        self._run()
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.state, ProjectDeposit.STATE_PUBLISHED)
        versions = list(ProjectDepositVersion.objects.filter(deposit=self.deposit).order_by("version_index"))
        self.assertEqual([v.version_index for v in versions], [1, 2])
        self.assertEqual(versions[1].changelog, "Second spin, fixed.")
        self.assertEqual(len(self.fz.depositions), 2)  # the open Zenodo draft was reused
        files = [f["filename"] for f in self.fz.deposition(self.deposit.deposition_id).files]
        self.assertIn("v2b.zip", files)
        self.assertNotIn("v2.zip", files)

    def test_discard_goes_back_to_the_last_published_version(self):
        response = self.client.post(reverse("projects:zenodo_new_version_discard", args=[self.project.slug]))
        self.assertEqual(response.status_code, 302)
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.state, ProjectDeposit.STATE_PUBLISHED)
        self.assertEqual(self.deposit.deposition_id, self.v1.deposition_id)
        self.assertEqual(self.deposit.doi, self.v1.doi)
        self.assertEqual(self.deposit.pending_changelog, "")
        self.assertFalse(self.project.attachments.filter(published_to_zenodo=False).exists())
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ZenodoJob.STATUS_CANCELLED)
        page = self.client.get(self.project.get_absolute_url())
        self.assertNotContains(page, "could not be published")
        # A fresh new version still works, reusing the draft Zenodo kept open.
        self.client.post(self.url, {
            "action": "publish", "changelog": "Third try.", "license_choice": "MIT",
            "archive": SimpleUploadedFile("v2c.zip", _zip(), content_type="application/zip"),
        })
        self._run()
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.state, ProjectDeposit.STATE_PUBLISHED)
        self.assertEqual(ProjectDepositVersion.objects.filter(deposit=self.deposit).count(), 2)
        self.assertEqual(len(self.fz.depositions), 2)

    def test_discard_waits_for_a_running_job(self):
        zenodo_jobs.retry(self.job)
        self.client.post(reverse("projects:zenodo_new_version_discard", args=[self.project.slug]))
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, ZenodoJob.STATUS_QUEUED)
        self.deposit.refresh_from_db()
        self.assertNotEqual(self.deposit.state, ProjectDeposit.STATE_PUBLISHED)

    def test_only_the_owner_can_discard(self):
        other = get_user_model().objects.create_user(username="someone-else")
        self.client.force_login(other)
        response = self.client.post(reverse("projects:zenodo_new_version_discard", args=[self.project.slug]))
        self.assertEqual(response.status_code, 404)
