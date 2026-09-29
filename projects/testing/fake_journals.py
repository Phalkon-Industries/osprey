"""A fake Crossref, Europe PMC and JOH OAI-PMH feed for tests, served from
the fixtures captured in the 2026-09-28 source survey. Exists only inside
a test run.

    with fake_journals() as fj:
        fj.seed_hardwarex_fixture()   # the resistance-welding article
        fj.seed_joh_fixture()         # the SACE machining article
"""
from __future__ import annotations

import contextlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from django.test.utils import override_settings

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "journals"
HARDWAREX_DOI = "10.1016/j.ohx.2026.e00839"
HARDWAREX_PMCID = "PMC13594966"
JOH_DOI = "10.5206/joh.v10i1.24875"


class FakeJournalsServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.crossref_works: dict[str, dict] = {}       # doi -> message
        self.crossref_listing: list[dict] = []          # items for /works?filter=
        self.epmc_by_doi: dict[str, str] = {}           # doi -> pmcid
        self.epmc_fulltext: dict[str, str] = {}         # pmcid -> xml
        self.oai_xml: str = "<OAI-PMH><ListRecords></ListRecords></OAI-PMH>"
        self.fail: dict[str, list[int]] = {}            # path prefix -> statuses to return first
        self.requests: list[str] = []
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def start(self):
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- seeding -----------------------------------------------------------

    def seed_hardwarex_fixture(self):
        work = json.loads((FIXTURES / "crossref_work_hardwarex.json").read_text())["message"]
        self.crossref_works[HARDWAREX_DOI.lower()] = work
        self.crossref_listing = [{"DOI": HARDWAREX_DOI}]
        self.epmc_by_doi[HARDWAREX_DOI.lower()] = HARDWAREX_PMCID
        self.epmc_fulltext[HARDWAREX_PMCID] = (FIXTURES / "europepmc_fulltext_hardwarex.xml").read_text()
        return work

    def seed_crossref_work(self, doi: str, *, title: str, authors: list[dict], year: int = 2025, pmcid: str = "", fulltext_xml: str = ""):
        self.crossref_works[doi.lower()] = {
            "DOI": doi, "title": [title], "author": authors, "container-title": ["HardwareX"],
            "published": {"date-parts": [[year, 1, 1]]}, "license": [{"URL": "http://creativecommons.org/licenses/by/4.0/", "content-version": "vor"}],
        }
        self.crossref_listing.append({"DOI": doi})
        if pmcid:
            self.epmc_by_doi[doi.lower()] = pmcid
            self.epmc_fulltext[pmcid] = fulltext_xml

    def seed_joh_fixture(self):
        self.oai_xml = (FIXTURES / "joh_oai_listrecords.xml").read_text()

    def fail_next(self, path_prefix: str, statuses: list[int]):
        self.fail[path_prefix] = list(statuses)


class _Handler(BaseHTTPRequestHandler):
    server: FakeJournalsServer  # type: ignore[assignment]

    def log_message(self, format, *args):  # noqa: A002
        return

    def _send(self, status: int, body: bytes = b"", content_type: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.requests.append(self.path)
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)
        for prefix, statuses in self.server.fail.items():
            if path.startswith(prefix) and statuses:
                self._send(statuses.pop(0), b'{"message":"injected"}')
                return
        if path == "/crossref/works":
            items = self.server.crossref_listing
            flt = (qs.get("filter") or [""])[0]
            m = re.search(r"from-pub-date:(\d{4}-\d{2}-\d{2})", flt)
            if m:
                since = m.group(1)
                items = [i for i in items if (self.server.crossref_works.get(i["DOI"].lower(), {}).get("published", {}).get("date-parts") or [[9999]])[0][0] >= int(since[:4])]
            self._send(200, json.dumps({"message": {"items": items, "next-cursor": ""}}).encode())
            return
        m = re.match(r"^/crossref/works/(.+)$", path)
        if m:
            work = self.server.crossref_works.get(m.group(1).lower())
            if work is None:
                self._send(404, b"Resource not found.", content_type="text/plain")
            else:
                self._send(200, json.dumps({"status": "ok", "message": work}).encode())
            return
        if path == "/epmc/search":
            q = (qs.get("query") or [""])[0]
            m = re.match(r"DOI:(.+)$", q, re.I)
            pmcid = self.server.epmc_by_doi.get((m.group(1) if m else "").lower())
            result = [{"doi": m.group(1), "pmcid": pmcid}] if pmcid else []
            self._send(200, json.dumps({"hitCount": len(result), "resultList": {"result": result}}).encode())
            return
        m = re.match(r"^/epmc/(PMC\d+)/fullTextXML$", path)
        if m:
            xml = self.server.epmc_fulltext.get(m.group(1))
            if xml is None:
                self._send(404, b"", content_type="text/plain")
            else:
                self._send(200, xml.encode("utf-8"), content_type="application/xml")
            return
        if path == "/oai":
            xml = self.server.oai_xml
            since = (qs.get("from") or [""])[0]
            if since:
                # Keep records whose datestamp is on or after `from`.
                head, _, rest = xml.partition("<record>")
                kept = [r for r in rest.split("<record>") if (re.search(r"<datestamp>(\d{4}-\d{2}-\d{2})", r) or [None, "0000"])[1] >= since]
                xml = head + "".join("<record>" + r for r in kept) if kept else head + "</ListRecords></OAI-PMH>"
            self._send(200, xml.encode("utf-8"), content_type="application/xml")
            return
        self._send(404, b'{"message":"Not Found"}')


@contextlib.contextmanager
def fake_journals():
    server = FakeJournalsServer().start()
    override = override_settings(
        CROSSREF_API_BASE_URL=f"{server.url}/crossref",
        EUROPEPMC_API_BASE_URL=f"{server.url}/epmc",
        JOH_OAI_URL=f"{server.url}/oai",
        INDEXING_RETRY_SLEEP=0,
    )
    override.enable()
    from projects.indexing import joh_source

    joh_source._cache.update(at=0.0, records=None)
    try:
        yield server
    finally:
        joh_source._cache.update(at=0.0, records=None)
        override.disable()
        server.stop()


class FakeJournalsMixin:
    def setUp(self):
        super().setUp()
        stack = contextlib.ExitStack()
        self.fj = stack.enter_context(fake_journals())
        self.addCleanup(stack.close)
