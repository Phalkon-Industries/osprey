"""Read-only contract checks against the real Crossref, Europe PMC and JOH
OAI-PMH services. Skipped unless RUN_LIVE_INDEXING=1:

    RUN_LIVE_INDEXING=1 manage.py test --tag=live-indexing

Run in dev before a deploy. Never writes anywhere."""
from __future__ import annotations

import os
from datetime import date

from django.test import TestCase, tag

from projects.indexing import github_source, hardwarex_source, joh_source
from projects.testing.fake_journals import HARDWAREX_DOI, JOH_DOI


@tag("live-indexing")
class LiveIndexingReadTests(TestCase):
    def setUp(self):
        if os.environ.get("RUN_LIVE_INDEXING") != "1":
            self.skipTest("set RUN_LIVE_INDEXING=1 for live reads")

    def test_hardwarex_article_still_yields_spec_table(self):
        rec = hardwarex_source.fetch(HARDWAREX_DOI)
        self.assertEqual(rec.license, "CERN-OHL-S-2.0")
        self.assertTrue(rec.files_url.startswith(("http://doi.org/10.17632", "https://doi.org/10.17632")))
        self.assertEqual(rec.oshwa_uid, "BR000022")
        self.assertEqual(len(rec.authors), 6)

    def test_hardwarex_listing_pages(self):
        dois = hardwarex_source.list_new(date(2026, 1, 1), limit=150)
        self.assertGreater(len(dois), 100)
        self.assertTrue(all(d.lower().startswith("10.1016/j.ohx.") for d in dois))

    def test_joh_feed_parses(self):
        records = joh_source.harvest()
        self.assertGreaterEqual(len(records), 60)
        rec = joh_source.fetch(JOH_DOI)
        self.assertEqual([a.name for a in rec.authors], ["Zhaohan Zheng", "Rolf Wüthrich"])
        self.assertTrue(rec.files_url)

    def test_github_public_repo(self):
        rec = github_source.fetch("OceanographyforEveryone/OpenCTD")
        self.assertEqual(rec.license, "MIT")
        self.assertTrue(rec.summary)
