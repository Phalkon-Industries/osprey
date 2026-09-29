"""Screenshot regression: key pages rendered in a real browser, compared
pixel-wise against committed baselines.

This is the net for the "tab renders empty" class of bug: nothing
asserted on markup, just "the page still looks like it did." A page
whose rendering drifts past the threshold fails with the actual and a
diff image written to `screenshot-diffs/` (gitignored) for a human look.

Baselines live in `projects/testing/screenshots/` and are committed.

- First run with no baseline for a page: the baseline is written and
  the test passes with a note. Commit the new files.
- `UPDATE_SCREENSHOTS=1 manage.py test projects.tests_screenshots`
  rewrites every baseline after an intentional visual change.

Determinism: animations and transitions are disabled, the caret is
hidden, fonts are awaited, the fake Zenodo gives stable DOIs, and the
data is created fresh per test in a fixed order. Small text differences
(a date, a permalink id) stay well under the threshold; layout changes
do not.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

from django.conf import settings
from django.urls import reverse
from PIL import Image, ImageChops

from projects.models import Contribution, Project
from projects.tests_journeys import JourneyTestCase

BASELINE_DIR = Path(__file__).resolve().parent / "testing" / "screenshots"
DIFF_DIR = Path(settings.BASE_DIR) / "screenshot-diffs"
# Fraction of pixels allowed to differ before a page counts as changed.
THRESHOLD = 0.005
# Per-channel difference below this is treated as identical (antialiasing).
PIXEL_TOLERANCE = 24

FREEZE_CSS = """
*, *::before, *::after {
  transition: none !important;
  animation: none !important;
  caret-color: transparent !important;
}
"""


def _diff_ratio(baseline: Image.Image, actual: Image.Image) -> tuple[float, Image.Image]:
    """Share of pixels that differ, plus a diff image for humans."""
    width = max(baseline.width, actual.width)
    height = max(baseline.height, actual.height)
    a = Image.new("RGB", (width, height), "magenta")
    b = Image.new("RGB", (width, height), "magenta")
    a.paste(baseline.convert("RGB"), (0, 0))
    b.paste(actual.convert("RGB"), (0, 0))
    diff = ImageChops.difference(a, b).convert("L")
    mask = diff.point(lambda v: 255 if v > PIXEL_TOLERANCE else 0)
    changed = mask.histogram()[255]  # count of flagged pixels, no per-pixel Python loop
    total = width * height
    # Highlight changed pixels in red over a faded actual.
    overlay = b.point(lambda v: v // 3 + 170)
    red = Image.new("RGB", (width, height), (220, 30, 30))
    overlay.paste(red, (0, 0), mask)
    return (changed / total if total else 0.0), overlay


class ScreenshotBaselineTests(JourneyTestCase):
    """One test per theme so a dark-only regression reads as such."""

    def _seed(self):
        owner = self.make_owner("shots-owner")
        published = self.make_published_project(owner, slug="shots-published-pump")
        draft = Project.objects.create(
            slug="shots-draft-logger",
            title="Shots Draft Logger",
            summary="A draft used for the form screenshots.",
            readme="# Shots Draft Logger\n\nSome README text so the description tab has content.",
            artifact_type="Hardware",
            field="Oceanography",
            license="MIT",
            visibility=Project.VISIBILITY_PRIVATE,
            created_by=owner,
        )
        Contribution.objects.create(project=draft, display_name="Shots Owner", role="Project lead")
        # A linked project, for the link page and its edit form.
        from projects import zenodo_link
        from projects.tests_linked import RECORD
        record = self.fz.seed_published({**RECORD, "creators": [{"name": "Owner, Shots", "orcid": owner.socialaccount_set.get().uid}]})
        self.linked = zenodo_link.link_project(record.doi, owner)
        # Linked slugs carry a random suffix and the placeholder cover's hue
        # comes from the slug; pin it so the baseline is stable.
        Project.objects.filter(pk=self.linked.pk).update(slug="shots-linked-pump")
        self.linked.refresh_from_db()
        self.sign_in(owner)
        return owner, published, draft

    def _pages(self, published, draft):
        return [
            ("home", self.url("home")),
            ("projects-list", self.url("projects:list")),
            ("project-detail", self.url("projects:detail", published.slug)),
            ("project-lineage", self.url("projects:lineage", published.slug)),
            ("project-versions", self.url("projects:versions", published.slug)),
            ("project-new", self.url("projects:new")),
            ("project-edit-basics", self.url("projects:edit", draft.slug) + "?tab=basics"),
            ("project-edit-contributors", self.url("projects:edit", draft.slug) + "?tab=contributors"),
            ("project-edit-lineage", self.url("projects:edit", draft.slug) + "?tab=related"),
            ("new-version", self.url("projects:zenodo_new_version", published.slug)),
            ("link-page", self.url("projects:link")),
            ("project-edit-linked", self.url("projects:edit", self.linked.slug)),
            ("contributor-credits", self.url("contributor_claims")),
            ("notification-settings", self.url("notifications:settings")),
            ("inbox", self.url("notifications:inbox")),
            ("profile", self.url("people:me")),
            ("how-it-works", self.url("how_it_works")),
        ]

    def _capture(self, url: str) -> Image.Image:
        self.page.goto(url)
        self.page.wait_for_load_state("networkidle")
        self.page.add_style_tag(content=FREEZE_CSS)
        self.page.evaluate("document.fonts && document.fonts.ready")
        self.page.wait_for_timeout(150)
        return Image.open(io.BytesIO(self.page.screenshot(full_page=True)))

    def _run_theme(self, theme: str):
        self.page.emulate_media(reduced_motion="reduce")
        self.context.add_cookies([{"name": "osprey-theme", "value": theme, "url": self.live_server_url}])
        owner, published, draft = self._seed()
        BASELINE_DIR.mkdir(parents=True, exist_ok=True)
        update = os.environ.get("UPDATE_SCREENSHOTS") == "1"
        created, changed = [], []
        for name, url in self._pages(published, draft):
            with self.subTest(page=name, theme=theme):
                actual = self._capture(url)
                path = BASELINE_DIR / f"{name}-{theme}.png"
                if not path.exists():
                    actual.save(path, optimize=True)
                    created.append(path.name)
                    continue
                baseline = Image.open(path)
                ratio, diff = _diff_ratio(baseline, actual)
                if update:
                    # Rewrite only pages that would have failed, so an
                    # update doesn't churn every file in git over
                    # antialiasing noise.
                    if ratio > THRESHOLD:
                        actual.save(path, optimize=True)
                        created.append(path.name)
                    continue
                if ratio > THRESHOLD:
                    DIFF_DIR.mkdir(parents=True, exist_ok=True)
                    actual.save(DIFF_DIR / f"{name}-{theme}.actual.png")
                    diff.save(DIFF_DIR / f"{name}-{theme}.diff.png")
                    changed.append(f"{name} ({theme}): {ratio:.2%} of pixels differ")
        if created:
            print(f"\n[screenshots] wrote {len(created)} baseline(s) for {theme}: {', '.join(created)}")
        self.assertNoBrowserErrors()
        self.assertEqual(
            changed,
            [],
            "Pages changed visually (see screenshot-diffs/ for actual and diff images; "
            "run with UPDATE_SCREENSHOTS=1 to accept intentional changes):\n  "
            + "\n  ".join(changed),
        )

    def test_light_theme_matches_baselines(self):
        self._run_theme("light")

    def test_dark_theme_matches_baselines(self):
        self._run_theme("dark")
