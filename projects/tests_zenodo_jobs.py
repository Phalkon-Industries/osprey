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

    @override_settings(ZENODO_TIMEOUT_SECONDS=1)
    def test_hang_counts_as_an_outage(self):
        self.fz.add_rule(match="actions/publish", mode="hang", delay_s=2.5)
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
