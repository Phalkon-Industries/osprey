"""Browser journeys: real Chromium against the live test server, with the
fake Zenodo alongside. Everything starts and stops inside the test run.

These exist for the class of bug that server-side tests cannot see:
JavaScript, DOM parsing, file inputs, client-side state across pages. Keep
the set small and end-to-end; edge cases belong in the request-level
tests. See planning/features/browser-ui-tests.md for the rule.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
import zipfile

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import Client, override_settings, tag
from django.urls import reverse

from projects.models import (
    Watch,
    Contribution,
    LineageEdge,
    Project,
    ProjectAttachment,
    ProjectDeposit,
    ProjectDepositVersion,
    ZenodoJob,
)
from projects.testing.fake_zenodo import FakeZenodoServer
from projects.tests import add_orcid_account
from projects.zenodo import publish_project_now

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - installed in the dev image
    PlaywrightError = Exception
    sync_playwright = None


def zip_bytes(name: str = "firmware/main.c", text: str = "int main(){}") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


@tag("browser")
@override_settings(ALLOWED_HOSTS=["localhost", "127.0.0.1", "testserver"])
class JourneyTestCase(StaticLiveServerTestCase):
    """One Chromium per class, one context per test, fake Zenodo reset per test."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if sync_playwright is None:
            raise unittest.SkipTest("Playwright is not installed")
        cls.fz = FakeZenodoServer().start()
        cls.addClassCleanup(cls.fz.stop)
        zenodo_override = override_settings(
            ZENODO_API_BASE_URL=cls.fz.url,
            ZENODO_ACCESS_TOKEN=cls.fz.token,
            ZENODO_USE_SANDBOX=True,
        )
        zenodo_override.enable()
        cls.addClassCleanup(zenodo_override.disable)
        cls.media_dir = tempfile.mkdtemp()
        media_override = override_settings(MEDIA_ROOT=cls.media_dir)
        media_override.enable()
        cls.addClassCleanup(media_override.disable)
        cls.addClassCleanup(shutil.rmtree, cls.media_dir, ignore_errors=True)
        # Playwright's sync API drives an event loop in this thread, which
        # trips Django's async-safety guard on every ORM call afterwards.
        # Allowing it here is the documented Playwright-with-Django setup;
        # the flag is scoped to this class and restored on cleanup.
        previous = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"

        def restore():
            if previous is None:
                os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
            else:
                os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = previous

        cls.addClassCleanup(restore)
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        try:
            cls.browser = cls.playwright.chromium.launch()
        except PlaywrightError as exc:  # pragma: no cover - environment
            raise unittest.SkipTest(f"Playwright Chromium is unavailable: {exc}")
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        super().setUp()
        self.fz.reset()
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page_errors: list[str] = []
        self.console_errors: list[str] = []
        self.page.on("pageerror", lambda err: self.page_errors.append(str(err)))
        self.page.on(
            "console",
            lambda msg: self.console_errors.append(msg.text)
            if msg.type == "error" and "favicon" not in msg.text
            else None,
        )
        # Confirm dialogs (publish asks) are accepted by default.
        self.page.on("dialog", lambda dialog: dialog.accept())

    # -- helpers ----------------------------------------------------------

    def sign_in(self, user, context=None) -> None:
        """Copy a force_login session cookie into a browser context."""
        client = Client()
        client.force_login(user)
        cookie = client.cookies[settings.SESSION_COOKIE_NAME]
        (context or self.context).add_cookies(
            [
                {
                    "name": settings.SESSION_COOKIE_NAME,
                    "value": cookie.value,
                    "url": self.live_server_url,
                }
            ]
        )

    def url(self, name: str, *args) -> str:
        return self.live_server_url + reverse(name, args=args)

    def second_actor(self, user):
        """A separate browser context signed in as another user."""
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(context.close)
        self.sign_in(user, context=context)
        page = context.new_page()
        page.on("pageerror", lambda err: self.page_errors.append(str(err)))
        page.on("dialog", lambda dialog: dialog.accept())
        return page

    def tab_state(self, name: str) -> str:
        return self.page.locator(f"[data-form-tab={name}] [data-tab-state]").inner_text().strip()

    def save_and_continue(self, panel: str) -> None:
        self.page.click(f"[data-form-panel={panel}] [data-mark-done]")

    def assertNoBrowserErrors(self):
        self.assertEqual(self.page_errors, [], f"uncaught JS errors: {self.page_errors}")
        self.assertEqual(self.console_errors, [], f"console errors: {self.console_errors}")

    def make_owner(self, username: str = "journey-owner"):
        user = get_user_model().objects.create_user(username=username)
        add_orcid_account(user)
        return user

    def make_published_project(self, owner, slug: str = "journey-pump") -> Project:
        project = Project.objects.create(
            slug=slug,
            title="Journey Pump",
            summary="A pump that travels through a real browser.",
            readme="# Journey Pump",
            artifact_type="Hardware",
            field="Oceanography",
            license="MIT",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=owner,
        )
        Contribution.objects.create(project=project, display_name="Journey Owner", role="Project lead")
        payload = zip_bytes()
        ProjectAttachment.objects.create(
            project=project,
            file=ContentFile(payload, name="pump-v1.zip"),
            filename="pump-v1.zip",
            size_bytes=len(payload),
        )
        publish_project_now(project, owner)
        return project


class NewVersionJourneyTests(JourneyTestCase):
    def test_publish_new_version_with_a_zip_from_the_browser(self):
        # Regression for the `[object RadioNodeList]` redirect: the upload
        # script built its POST URL from form.action, which the two submit
        # buttons named "action" shadowed. Only a real browser sees that.
        owner = self.make_owner()
        project = self.make_published_project(owner)
        self.sign_in(owner)

        self.page.goto(self.url("projects:zenodo_new_version", project.slug))
        self.page.fill("textarea[name=changelog]", "Second spin of the board.")
        self.page.set_input_files(
            "input[name=archive]",
            {"name": "pump-v2.zip", "mimeType": "application/zip", "buffer": zip_bytes("firmware/v2.c")},
        )
        self.page.click("button[name=action][value=publish]")
        self.page.wait_for_url(f"**{reverse('projects:detail', args=[project.slug])}", timeout=20000)

        # Landed on the project page, not a 404, with the queued notice.
        self.assertIn("Journey Pump", self.page.content())
        self.assertIn("Publishing the new version", self.page.content())
        call_command("run_zenodo_jobs")
        self.assertEqual(ZenodoJob.objects.filter(status=ZenodoJob.STATUS_DONE).count(), 1)
        self.page.reload()
        self.assertIn("v2", self.page.content())
        deposit = ProjectDeposit.objects.get(project=project)
        self.assertEqual(deposit.state, ProjectDeposit.STATE_PUBLISHED)
        self.assertEqual(
            list(ProjectDepositVersion.objects.filter(deposit=deposit).order_by("version_index").values_list("version_index", flat=True)),
            [1, 2],
        )
        fake_v2 = self.fz.deposition(deposit.deposition_id)
        self.assertEqual(
            sorted(f["filename"] for f in fake_v2.files),
            ["CITATION.cff", "LICENSE.txt", "osprey-project.json", "pump-v2.zip"],
        )
        self.assertNoBrowserErrors()


class ConsoleErrorSweepTests(JourneyTestCase):
    """Every page type, as a signed-in owner, must load without JS errors.

    The cheapest broad net for the "widget vanished" class of report: an
    uncaught exception anywhere on a page fails this test with the
    message attached.
    """

    def test_signed_in_pages_load_without_js_errors(self):
        owner = self.make_owner()
        project = self.make_published_project(owner)
        self.sign_in(owner)
        pages = [
            self.url("home"),
            self.url("projects:list"),
            self.url("projects:detail", project.slug),
            self.url("projects:edit", project.slug),
            self.url("projects:new"),
            self.url("projects:zenodo_new_version", project.slug),
            self.url("notifications:inbox"),
            self.url("notifications:settings"),
            self.url("people:me"),
            self.url("how_it_works"),
            self.url("lineage_guide"),
        ]
        for target in pages:
            with self.subTest(url=target):
                response = self.page.goto(target)
                self.assertIsNotNone(response, target)
                self.assertEqual(response.status, 200, target)
                # Let deferred scripts (widget, autosave) run.
                self.page.wait_for_load_state("networkidle")
                self.assertNoBrowserErrors()


class SubmissionJourneyTests(JourneyTestCase):
    def test_submit_through_all_six_tabs_and_publish(self):
        owner = self.make_owner()
        self.sign_in(owner)
        self.page.goto(self.url("projects:new"))

        # Basics. The first Save and continue on a brand-new project is a
        # real submit that creates the draft and returns to the form.
        self.page.fill("input[name=title]", "Tabbed Tide Logger")
        self.page.fill("[name=summary]", "Logs tides, one tab at a time.")
        self.page.select_option("select[name=license_choice]", "MIT")
        self.page.fill("input[name=artifact_type]", "Hardware")
        self.page.select_option("select[name=self_rating]", "4")
        self.save_and_continue("basics")
        self.page.wait_for_url("**/edit/?tab=description*", timeout=20000)
        project = Project.objects.get(title="Tabbed Tide Logger")
        self.assertEqual(project.visibility, Project.VISIBILITY_PRIVATE)
        self.assertEqual(self.tab_state("basics"), "✓")

        # Description, then the remaining sections; these autosave in the
        # background and advance without leaving the page.
        self.page.fill("textarea[name=readme]", "# Tabbed Tide Logger\n\nBuilt in a browser test.")
        self.save_and_continue("description")
        self.page.wait_for_selector("[data-form-panel=contributors]:not([hidden])")
        self.assertEqual(self.page.input_value("input[name=contributions-0-role]"), "Project lead")
        self.save_and_continue("contributors")
        self.page.wait_for_selector("[data-form-panel=files]:not([hidden])")
        # The archive input is wrapped in a drop target; picking a file
        # shows its name there.
        self.assertEqual(self.page.locator("[data-form-panel=files] .dropzone input[name=attachment_files]").count(), 1)
        self.page.set_input_files(
            "input[name=attachment_files]",
            {"name": "tide-logger.zip", "mimeType": "application/zip", "buffer": zip_bytes()},
        )
        self.page.wait_for_function("() => document.querySelector('.dropzone-name').textContent.includes('tide-logger.zip')")
        self.save_and_continue("files")
        self.page.wait_for_selector("[data-form-panel=details]:not([hidden])")
        self.save_and_continue("details")
        self.page.wait_for_selector("[data-form-panel=related]:not([hidden])")
        self.save_and_continue("related")
        for name in ("basics", "description", "contributors", "files", "details", "related"):
            self.assertEqual(self.tab_state(name), "✓", name)
        # The background autosave persisted the README.
        self.page.wait_for_timeout(500)
        project.refresh_from_db()
        self.assertIn("Built in a browser test", project.readme)

        self.page.click("[data-publish-button]")  # confirm dialog auto-accepted
        self.page.wait_for_url(f"**{reverse('projects:detail', args=[project.slug])}", timeout=30000)
        self.assertIn("OSPREY will mint the DOI", self.page.content())
        project.refresh_from_db()
        self.assertEqual(project.visibility, Project.VISIBILITY_PRIVATE)  # until the job runs
        call_command("run_zenodo_jobs")
        self.page.reload()
        project.refresh_from_db()
        self.assertEqual(project.visibility, Project.VISIBILITY_PUBLIC)
        self.assertTrue(project.doi)
        deposit = ProjectDeposit.objects.get(project=project)
        fake_dep = self.fz.deposition(deposit.deposition_id)
        self.assertEqual(fake_dep.state, "done")
        self.assertIn("tide-logger.zip", [f["filename"] for f in fake_dep.files])
        self.assertTrue(project.contributions.filter(orcid_id=owner.socialaccount_set.get().uid).exists())
        self.assertIn(project.doi, self.page.content())
        self.assertNoBrowserErrors()

    def test_publish_click_on_an_incomplete_form_lists_what_is_missing(self):
        owner = self.make_owner()
        self.sign_in(owner)
        self.page.goto(self.url("projects:new"))
        self.page.click("[data-publish-button]")
        note = self.page.locator("[data-publish-blocked-note]")
        note.wait_for()
        text = note.inner_text()
        self.assertIn("Not ready to publish", text)
        self.assertIn("Basics", text)
        self.assertIn("Files", text)
        self.assertEqual(Project.objects.count(), 0)
        self.assertNoBrowserErrors()


class EditPublishedJourneyTests(JourneyTestCase):
    def test_saving_a_published_project_syncs_metadata_to_zenodo(self):
        owner = self.make_owner()
        project = self.make_published_project(owner)
        self.sign_in(owner)
        self.page.goto(self.url("projects:edit", project.slug))
        # Published projects don't offer the archive upload; new versions do.
        self.assertEqual(self.page.locator("input[name=attachment_files]").count(), 0)
        self.page.fill("input[name=title]", "Journey Pump, renamed")
        self.page.click("button[name=action][value=save]")
        self.page.wait_for_url(f"**{reverse('projects:detail', args=[project.slug])}", timeout=20000)
        self.assertIn("Journey Pump, renamed", self.page.content())
        self.assertIn("Zenodo metadata sync pending", self.page.content())
        call_command("run_zenodo_jobs")
        fake_dep = self.fz.deposition(ProjectDeposit.objects.get(project=project).deposition_id)
        self.assertEqual(fake_dep.metadata["title"], "Journey Pump, renamed")
        self.assertEqual(fake_dep.state, "done")
        self.assertNoBrowserErrors()


class ContributorConfirmationJourneyTests(JourneyTestCase):
    def test_owner_sends_confirmation_and_colleague_accepts(self):
        from notifications.models import Notification

        owner = self.make_owner()
        colleague = get_user_model().objects.create_user(username="journey-colleague")
        add_orcid_account(colleague, "0000-0002-1111-2222")
        project = Project.objects.create(
            slug="shared-sensor",
            title="Shared Sensor",
            summary="Two people built this.",
            artifact_type="Hardware",
            field="Oceanography",
            license="MIT",
            visibility=Project.VISIBILITY_PRIVATE,
            created_by=owner,
        )
        Contribution.objects.create(project=project, display_name="Journey Owner", role="Project lead", orcid_id=owner.socialaccount_set.get().uid)
        row = Contribution.objects.create(project=project, display_name="Colleague Person", role="Firmware", orcid_id="0000-0002-1111-2222")

        # Owner: Contributors tab, Send confirmation request on the colleague's row.
        self.sign_in(owner)
        self.page.goto(self.url("projects:edit", project.slug) + "?tab=contributors")
        self.page.click("button[name=contrib_confirm][value='1']")
        self.page.wait_for_url("**/edit/?tab=contributors*", timeout=20000)
        self.assertIn("Confirmation request sent", self.page.content())
        row.refresh_from_db()
        self.assertEqual(row.claim_status, Contribution.CLAIM_INVITED)
        self.assertTrue(Notification.objects.filter(user=colleague, kind="contributor_listed").exists())

        # Colleague: Contributor Credits, Accept.
        other = self.second_actor(colleague)
        other.goto(self.url("contributor_claims"))
        self.assertIn("Shared Sensor", other.content())
        other.click(f"form:has(input[name=contribution_id][value='{row.pk}']) button[value=accept]")
        other.wait_for_load_state("networkidle")
        row.refresh_from_db()
        self.assertEqual(row.claim_status, Contribution.CLAIM_VERIFIED)
        self.assertEqual(row.user_id, colleague.pk)
        self.assertIn("accepted", other.content().lower())
        self.assertNoBrowserErrors()


class LineageJourneyTests(JourneyTestCase):
    def test_declare_a_derived_from_link_on_a_draft(self):
        owner = self.make_owner()
        parent = self.make_published_project(owner, slug="parent-pump")
        child = Project.objects.create(
            slug="child-pump",
            title="Child Pump",
            summary="Derived from the parent.",
            artifact_type="Hardware",
            field="Oceanography",
            license="MIT",
            visibility=Project.VISIBILITY_PRIVATE,
            created_by=owner,
        )
        Contribution.objects.create(project=child, display_name="Journey Owner", role="Project lead")
        self.sign_in(owner)
        self.page.goto(self.url("projects:edit", child.slug) + "?tab=related")
        self.page.click("[data-lineage-add]")
        row = self.page.locator("[data-lineage-row]:not([hidden])").first
        row.locator("input[name=lineage_target]").fill(self.url("projects:detail", parent.slug))
        row.locator("[data-lineage-lookup]").click()
        preview = row.locator("[data-lineage-preview]")
        preview.wait_for()
        self.page.wait_for_function(
            "el => el.textContent.includes('Journey Pump')", arg=preview.element_handle(), timeout=10000
        )
        row.locator("select[name=lineage_relation]").select_option("derived_from")
        self.page.click("button[name=action][value=draft]")
        self.page.wait_for_url(f"**{reverse('projects:detail', args=[child.slug])}", timeout=20000)
        edge = LineageEdge.objects.get(child=child, parent=parent)
        self.assertEqual(edge.relation, "derived_from")
        self.assertIsNone(edge.claimed_at)  # quiet until the child publishes
        self.assertNoBrowserErrors()


class UseReportJourneyTests(JourneyTestCase):
    def test_file_a_use_report_from_another_account(self):
        from use_reports.models import UseReport

        owner = self.make_owner()
        project = self.make_published_project(owner)
        reader = get_user_model().objects.create_user(username="journey-reader")
        self.sign_in(reader)
        self.page.goto(self.url("use_reports:new", project.slug))
        self.page.fill("textarea[name=narrative]", "Ran it on the dock for a week; the seals held.")
        self.page.fill("input[name=used_at]", "Woods Hole dock")
        self.page.click("form:has(textarea[name=narrative]) button[type=submit]")
        self.page.wait_for_url(f"**{reverse('use_reports:index', args=[project.slug])}", timeout=20000)
        self.assertIn("the seals held", self.page.content())
        self.assertEqual(UseReport.objects.filter(project=project, author=reader).count(), 1)
        self.assertNoBrowserErrors()


class SuggestionBoxJourneyTests(JourneyTestCase):
    def test_send_a_suggestion_with_a_screenshot_from_a_project_page(self):
        from feedback.models import Feedback

        owner = self.make_owner()
        project = self.make_published_project(owner)
        self.sign_in(owner)
        self.page.goto(self.url("projects:detail", project.slug))
        self.page.click(".fb-fab")
        shot = self.page.locator(".fb-include-shot")
        if not shot.is_checked():
            shot.check()
        self.page.wait_for_function(
            "() => document.querySelector('.fb-shot-status').textContent.includes('annotate')",
            timeout=20000,
        )
        self.page.fill(".fb-msg", "The cover image looks cropped on this page.")
        self.page.click(".fb-send")
        self.page.wait_for_function(
            "() => document.querySelector('.fb-status').textContent.includes('Thanks')",
            timeout=20000,
        )
        # The panel closes and a thank-you toast takes its place.
        self.page.wait_for_selector(".fb-toast.is-in", timeout=5000)
        self.assertIn("Thank you", self.page.locator(".fb-toast").inner_text())
        fb = Feedback.objects.get()
        self.assertEqual(fb.user, owner)
        self.assertIn("cropped", fb.message)
        self.assertTrue(fb.screenshot, "screenshot was not stored")
        self.assertTrue(fb.browser.startswith("Chrome "), fb.browser)
        self.assertIn(reverse("projects:detail", args=[project.slug]), fb.page_url)
        self.assertNoBrowserErrors()


class StaffTriageJourneyTests(JourneyTestCase):
    def test_staff_triages_a_suggestion(self):
        from feedback.models import Feedback

        reporter = self.make_owner()
        staff = get_user_model().objects.create_user(username="journey-staff", is_staff=True)
        fb = Feedback.objects.create(user=reporter, message="The lineage tab is empty for me.")
        self.sign_in(staff)
        self.page.goto(self.url("feedback:review"))
        form = self.page.locator(f"form.feedback-triage:has(input[name=feedback_id][value='{fb.pk}'])")
        form.locator("select[name=status]").select_option("triaged")
        form.locator("textarea[name=admin_notes]").fill("Reproduced; nested section.")
        form.locator("button[type=submit]").click()
        self.page.wait_for_load_state("networkidle")
        fb.refresh_from_db()
        self.assertEqual(fb.status, Feedback.STATUS_TRIAGED)
        self.assertEqual(fb.admin_notes, "Reproduced; nested section.")
        self.assertNoBrowserErrors()


class NotificationSettingsJourneyTests(JourneyTestCase):
    def test_turning_off_sitewide_new_project_notices(self):
        from notifications.models import NotificationPreference

        owner = self.make_owner()
        self.sign_in(owner)
        self.page.goto(self.url("notifications:settings"))
        self.page.select_option("select[name=new_projects]", "off")
        self.page.click("form:has(input[value=save_new_projects]) button[type=submit]")
        self.page.wait_for_load_state("networkidle")
        self.assertEqual(NotificationPreference.objects.get(user=owner).new_projects, "off")
        self.assertEqual(self.page.input_value("select[name=new_projects]"), "off")
        self.assertNoBrowserErrors()


class RegisterJourneyTests(JourneyTestCase):
    def test_register_a_zenodo_record_from_the_browser(self):
        from projects.tests_registered import RECORD, OWNER_ORCID

        owner = get_user_model().objects.create_user(username="journey-registrant")
        add_orcid_account(owner, OWNER_ORCID)
        record = self.fz.seed_published(RECORD, files=[("pump-v1.zip", 4096)])
        self.sign_in(owner)
        self.page.goto(self.url("projects:new"))
        self.page.click("text=Register your Zenodo record")
        self.page.wait_for_url("**/projects/register/")
        self.page.fill("input[name=doi]", f"https://doi.org/{record.doi}")
        self.page.click("button:has-text('Register this record')")
        self.page.wait_for_url("**/edit/?tab=basics", timeout=20000)
        self.assertIn("Your Zenodo record", self.page.content())
        self.assertIn("OSPREY community on Zenodo", self.page.content())
        self.assertEqual(self.page.locator("[data-form-tab=files]").count(), 0)
        self.assertEqual(self.page.input_value("input[name=title]"), "Deep Sea Peristaltic Pump")
        project = Project.objects.get(origin=Project.ORIGIN_REGISTERED)
        self.assertEqual(project.created_by, owner)
        # Refresh from the page: nothing changed yet.
        with self.page.expect_navigation():
            self.page.click("button:has-text('Refresh from Zenodo')")
        self.assertIn("Refreshed from Zenodo. Nothing changed.", self.page.content())
        # Publish a new version on "Zenodo", refresh again: it shows up.
        self.fz.seed_published(RECORD, concept=record.conceptrecid)
        with self.page.expect_navigation():
            self.page.click("button:has-text('Refresh from Zenodo')")
        self.assertIn("1 new version", self.page.content())
        self.page.goto(self.url("projects:versions", project.slug))
        self.assertIn("v2", self.page.content())
        self.assertNoBrowserErrors()


class DeleteDraftJourneyTests(JourneyTestCase):
    def test_owner_deletes_a_draft_from_the_edit_page(self):
        owner = self.make_owner()
        draft = Project.objects.create(
            slug="journey-scrap",
            title="Scrap Draft",
            summary="Never going to be published.",
            artifact_type="Hardware",
            field="Oceanography",
            visibility=Project.VISIBILITY_PRIVATE,
            created_by=owner,
        )
        self.sign_in(owner)
        self.page.goto(self.url("projects:edit", draft.slug))
        with self.page.expect_navigation():
            self.page.click("text=Delete draft")
        self.assertIn("Delete this draft?", self.page.content())
        with self.page.expect_navigation():
            self.page.click("button:has-text('Delete draft')")
        self.assertIn("Deleted the draft", self.page.content())
        self.assertFalse(Project.objects.filter(pk=draft.pk).exists())
        self.assertNoBrowserErrors()
