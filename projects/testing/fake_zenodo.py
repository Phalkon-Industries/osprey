"""A fake of Zenodo's deposit API for tests.

OSPREY's real `ZenodoClient` talks to this over real HTTP, so the publish,
new-version, and metadata-sync code runs unmodified against something
fast, offline, deterministic, and breakable on command.

It exists only inside a test run: `fake_zenodo()` starts it on a random
loopback port, points the Zenodo settings at it for the duration, and
tears it down on exit. There is deliberately no `__main__`, no compose
service, and no runtime switch; dev, sandbox, and prod always talk to
real Zenodo, and the system check in `projects/checks.py` refuses to
boot a deployed server pointed anywhere else.

Behavior copied from Zenodo where the code has depended on it:

- DOIs are pre-reserved on creation (`metadata.prereserve_doi`) and
  assigned on publish. `conceptrecid` is returned from the start;
  `conceptdoi` only after the first publish, mirroring the sandbox quirk
  `_extract_zenodo_ids` works around.
- Publish requires at least one file and the required metadata fields,
  otherwise 400 with Zenodo's error body shape.
- A new version copies the previous version's files into a fresh draft
  and answers with the *original* deposition carrying a
  `links.latest_draft` pointer, which the client then follows.
- Edit mode (`actions/edit`) snapshots metadata so `actions/discard` can
  restore it; publishing an edit keeps the same DOI.
- Every `/api/` call needs the Bearer token; otherwise 401.

Failure injection: `server.add_rule(match="actions/publish", mode="status",
status=500, times=1)`. Modes: `status` (respond with an error),
`hang` (sleep `delay_s`, longer than the client timeout), `drop` (close
the connection without a response), `garbage` (an HTML body with
`status`, default 500, where JSON was expected). Rules are consumed `times` times, then ignored.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlparse

from django.test import override_settings

# Same prefix the sandbox uses so the DOI-handling code sees realistic
# values. Record ids start high so nothing here resembles a real record.
DOI_PREFIX = "10.5072/zenodo."
FIRST_RECORD_ID = 900000
REQUIRED_METADATA = ("title", "upload_type", "description", "creators")

STATE_UNSUBMITTED = "unsubmitted"  # draft, never published
STATE_DONE = "done"  # published
STATE_INPROGRESS = "inprogress"  # published, reopened for a metadata edit


@dataclass
class Rule:
    match: str
    mode: str = "status"
    status: int = 500
    delay_s: float = 0.0
    times: int = 1
    message: str = "Injected failure"


@dataclass
class Deposition:
    id: int
    conceptrecid: int
    bucket: str
    state: str = STATE_UNSUBMITTED
    metadata: dict[str, Any] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    doi: str = ""
    conceptdoi: str = ""
    version_index: int = 1
    published_metadata: dict[str, Any] | None = None
    # "osprey" is the platform token's own account; anything else is a
    # record somebody else owns (a linked project's source). Edit access
    # for the platform token then depends on an explicit grant.
    owner: str = "osprey"
    edit_granted: bool = False
    communities: list[str] = field(default_factory=list)

    @property
    def editable(self) -> bool:
        return self.state in (STATE_UNSUBMITTED, STATE_INPROGRESS)

    @property
    def platform_can_edit(self) -> bool:
        return self.owner == "osprey" or self.edit_granted


@dataclass
class CommunityRequest:
    id: str
    record_id: int
    community: str
    status: str = "submitted"  # submitted | accepted | declined


class FakeZenodoServer(ThreadingHTTPServer):
    """Loopback HTTP server holding one in-memory Zenodo."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, token: str = "fake-token"):
        super().__init__(("127.0.0.1", 0), FakeZenodoHandler)
        self.token = token
        self.lock = threading.RLock()
        self.depositions: dict[int, Deposition] = {}
        self.buckets: dict[str, int] = {}
        self.rules: list[Rule] = []
        self.requests: list[tuple[str, str]] = []
        self.community_requests: dict[str, CommunityRequest] = {}
        self.community = "osprey"
        # Largest page the versions listing will serve; tests lower it to
        # force the reader through `links.next`.
        self.max_page_size = 100
        self._next_id = FIRST_RECORD_ID
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeZenodoServer":
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def reset(self) -> None:
        with self.lock:
            self.depositions.clear()
            self.buckets.clear()
            self.rules.clear()
            self.requests.clear()
            self.community_requests.clear()
            self._next_id = FIRST_RECORD_ID

    # -- test-facing helpers ----------------------------------------------

    def add_rule(self, **kwargs) -> Rule:
        rule = Rule(**kwargs)
        with self.lock:
            self.rules.append(rule)
        return rule

    def deposition(self, deposition_id) -> Deposition:
        return self.depositions[int(deposition_id)]

    def paths(self, method: str | None = None) -> list[str]:
        return [p for m, p in self.requests if method is None or m == method]

    # -- seeding records that belong to other people ------------------------

    def seed_published(
        self,
        metadata: dict[str, Any],
        *,
        files: list[tuple[str, int]] | None = None,
        owner: str = "someone-else",
        concept: int | None = None,
    ) -> Deposition:
        """A published record OSPREY did not create: the source of a
        linked project. `files` is a list of (filename, size)."""
        with self.lock:
            dep = self.new_deposition(concept=concept, copy_from=None)
            dep.owner = owner
            dep.metadata = json.loads(json.dumps(metadata))
            dep.files = [
                {"id": uuid.uuid4().hex, "filename": name, "filesize": size, "checksum": "md5:seeded"}
                for name, size in (files or [("archive.zip", 1234)])
            ]
            siblings = [d for d in self.depositions.values() if d.conceptrecid == dep.conceptrecid and d is not dep]
            if siblings:
                dep.version_index = max(d.version_index for d in siblings) + 1
                dep.conceptdoi = siblings[0].conceptdoi
            dep.doi = f"{DOI_PREFIX}{dep.id}"
            dep.conceptdoi = dep.conceptdoi or f"{DOI_PREFIX}{dep.conceptrecid}"
            dep.state = STATE_DONE
            return dep

    def grant_edit(self, deposition_id, granted: bool = True) -> None:
        self.deposition(deposition_id).edit_granted = granted

    def submit_to_community(self, deposition_id, community: str | None = None) -> CommunityRequest:
        """What a record owner does on Zenodo: an inclusion request that
        the community's curators (OSPREY) then accept."""
        with self.lock:
            req = CommunityRequest(
                id=uuid.uuid4().hex,
                record_id=int(deposition_id),
                community=community or self.community,
            )
            self.community_requests[req.id] = req
            return req

    def latest_in_concept(self, conceptrecid: int) -> Deposition | None:
        published = [d for d in self.depositions.values() if d.conceptrecid == conceptrecid and d.state != STATE_UNSUBMITTED]
        return max(published, key=lambda d: d.version_index) if published else None

    def record_representation(self, dep: Deposition) -> dict[str, Any]:
        """The public /api/records shape: what a linked project reads."""
        rep = self.representation(dep)
        rep["files"] = [
            {
                "id": f["id"],
                "key": f["filename"],
                "size": f["filesize"],
                "checksum": f["checksum"],
                "links": {"self": f"{self.url}/api/records/{dep.id}/files/{f['filename']}/content"},
            }
            for f in dep.files
        ]
        rep["links"]["self_html"] = f"{self.url}/records/{dep.id}"
        rep["links"]["versions"] = f"{self.url}/api/records/{dep.id}/versions"
        latest = self.latest_in_concept(dep.conceptrecid)
        rep["links"]["latest"] = f"{self.url}/api/records/{latest.id}" if latest else ""
        rep["metadata"]["communities"] = [{"id": c} for c in dep.communities]
        # Zenodo's version counter lives here, 0-based, newest is_last.
        rep["metadata"]["relations"] = {
            "version": [{
                "index": dep.version_index - 1,
                "is_last": latest is dep,
                "parent": {"pid_type": "recid", "pid_value": str(dep.conceptrecid)},
            }]
        }
        rep["owners"] = [{"id": 1 if dep.owner == "osprey" else 2}]
        rep["created"] = rep["updated"] = "2026-01-01T00:00:00+00:00"
        return rep

    # -- state changes (called by the handler under the lock) -------------

    def new_deposition(self, *, concept: int | None = None, copy_from: Deposition | None = None) -> Deposition:
        # Like Zenodo, a new concept gets its own id just below its first
        # version's id, so "the concept" and "version 1" are different
        # records and a concept id resolves to the latest version.
        if concept is None:
            concept = self._next_id
            self._next_id += 1
        dep_id = self._next_id
        self._next_id += 1
        dep = Deposition(
            id=dep_id,
            conceptrecid=concept,
            bucket=uuid.uuid4().hex,
        )
        if copy_from is not None:
            dep.metadata = json.loads(json.dumps(copy_from.metadata))
            dep.files = [
                {**f, "id": uuid.uuid4().hex} for f in copy_from.files
            ]
            dep.conceptdoi = copy_from.conceptdoi
            dep.version_index = copy_from.version_index + 1
        self.depositions[dep_id] = dep
        self.buckets[dep.bucket] = dep_id
        return dep

    def representation(self, dep: Deposition) -> dict[str, Any]:
        links = {
            "self": f"{self.url}/api/deposit/depositions/{dep.id}",
            "bucket": f"{self.url}/api/files/{dep.bucket}",
            "html": f"{self.url}/deposit/{dep.id}",
        }
        metadata = dict(dep.metadata)
        metadata["prereserve_doi"] = {"doi": f"{DOI_PREFIX}{dep.id}", "recid": dep.id}
        rep: dict[str, Any] = {
            "id": dep.id,
            "conceptrecid": str(dep.conceptrecid),
            "state": dep.state,
            "submitted": dep.state != STATE_UNSUBMITTED,
            "title": metadata.get("title", ""),
            "metadata": metadata,
            "files": [dict(f) for f in dep.files],
            "links": links,
        }
        if dep.doi:
            rep["doi"] = dep.doi
            rep["record_id"] = dep.id
            metadata["doi"] = dep.doi
            links["record_html"] = f"{self.url}/records/{dep.id}"
        if dep.conceptdoi:
            rep["conceptdoi"] = dep.conceptdoi
        return rep

    def take_rule(self, path: str) -> Rule | None:
        with self.lock:
            for rule in self.rules:
                if rule.times > 0 and rule.match in path:
                    rule.times -= 1
                    return rule
        return None


class FakeZenodoHandler(BaseHTTPRequestHandler):
    server: FakeZenodoServer  # type: ignore[assignment]

    ROUTES = [
        ("POST", re.compile(r"^/api/deposit/depositions$"), "create"),
        ("GET", re.compile(r"^/api/deposit/depositions/(\d+)$"), "get"),
        ("PUT", re.compile(r"^/api/deposit/depositions/(\d+)$"), "update"),
        ("POST", re.compile(r"^/api/deposit/depositions/(\d+)/actions/(publish|newversion|edit|discard)$"), "action"),
        ("GET", re.compile(r"^/api/deposit/depositions/(\d+)/files$"), "list_files"),
        ("DELETE", re.compile(r"^/api/deposit/depositions/(\d+)/files/([^/]+)$"), "delete_file"),
        ("PUT", re.compile(r"^/api/files/([^/]+)/(.+)$"), "upload"),
        ("GET", re.compile(r"^/api/records/(\d+)$"), "record_json"),
        ("GET", re.compile(r"^/api/records/(\d+)/versions$"), "record_versions"),
        ("GET", re.compile(r"^/api/records/(\d+)/files/([^/]+)/content$"), "record_file"),
        ("GET", re.compile(r"^/api/records$"), "record_search"),
        ("GET", re.compile(r"^/api/requests$"), "requests_list"),
        ("POST", re.compile(r"^/api/requests/([^/]+)/actions/(accept|decline)$"), "request_action"),
        ("GET", re.compile(r"^/records/(\d+)$"), "record_html"),
    ]

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return

    # One entry point per verb; everything funnels into _dispatch.
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # -- plumbing ---------------------------------------------------------

    def _content_length(self) -> int:
        try:
            return int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return 0

    def _read_json(self) -> dict[str, Any]:
        raw = self.rfile.read(self._content_length())
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _drain(self) -> None:
        remaining = self._content_length()
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            remaining -= len(chunk)

    def _send(self, status: int, payload: Any = None, *, content_type: str = "application/json", raw: bytes | None = None) -> None:
        if raw is None:
            raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def _error(self, status: int, message: str, errors: list[dict[str, str]] | None = None) -> None:
        body: dict[str, Any] = {"status": status, "message": message}
        if errors:
            body["errors"] = errors
        self._send(status, body)

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {self.server.token}"

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path
        with self.server.lock:
            self.server.requests.append((method, path))
        rule = self.server.take_rule(path)
        if rule is not None:
            if rule.mode == "status":
                self._drain()
                self._error(rule.status, rule.message)
                return
            if rule.mode == "hang":
                time.sleep(rule.delay_s)
                # Fall through and answer normally; the client has timed out.
            elif rule.mode == "drop":
                self.close_connection = True
                return
            elif rule.mode == "garbage":
                # An HTML page where JSON was expected, e.g. from a proxy in
                # front of Zenodo. `status` picks the code (default 500).
                self._drain()
                self._send(rule.status, raw=b"<html><body>Service temporarily unavailable</body></html>", content_type="text/html")
                return
        public = path.startswith("/api/records")
        if path.startswith("/api/") and not public and not self._authorized():
            self._drain()
            self._error(401, "The server could not verify that you are authorized to access the URL requested.")
            return
        for verb, pattern, name in self.ROUTES:
            if verb != method:
                continue
            match = pattern.match(path)
            if match:
                with self.server.lock:
                    getattr(self, f"_route_{name}")(*match.groups())
                return
        self._drain()
        self._error(404, "The requested URL was not found on the server.")

    def _dep_or_404(self, dep_id: str) -> Deposition | None:
        dep = self.server.depositions.get(int(dep_id))
        if dep is None:
            self._drain()
            self._error(404, "PID does not exist.")
        return dep

    # -- routes -----------------------------------------------------------

    def _route_create(self) -> None:
        body = self._read_json()
        dep = self.server.new_deposition()
        if isinstance(body.get("metadata"), dict):
            dep.metadata = body["metadata"]
        self._send(201, self.server.representation(dep))

    def _route_get(self, dep_id: str) -> None:
        dep = self._dep_or_404(dep_id)
        if dep is None:
            return
        if not dep.platform_can_edit:
            # Zenodo answers the deposit endpoint with 403 for records the
            # token cannot edit; OSPREY uses exactly that to detect grants.
            self._error(403, "You don't have the permission to edit this record.")
            return
        self._send(200, self.server.representation(dep))

    def _route_update(self, dep_id: str) -> None:
        dep = self._dep_or_404(dep_id)
        if dep is None:
            return
        body = self._read_json()
        if not dep.platform_can_edit:
            self._error(403, "You don't have the permission to edit this record.")
            return
        if not dep.editable:
            self._error(403, "Deposit is not editable. Use actions/edit to reopen it.")
            return
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            self._error(400, "Validation error.", [{"field": "metadata", "message": "Missing metadata object."}])
            return
        dep.metadata = metadata
        self._send(200, self.server.representation(dep))

    def _route_action(self, dep_id: str, action: str) -> None:
        dep = self._dep_or_404(dep_id)
        if dep is None:
            return
        self._drain()
        if not dep.platform_can_edit:
            self._error(403, "You don't have the permission to edit this record.")
            return
        if action == "publish":
            self._publish(dep)
        elif action == "newversion":
            if dep.state != STATE_DONE:
                self._error(400, "Please publish the deposition before creating a new version.")
                return
            draft = self.server.new_deposition(concept=dep.conceptrecid, copy_from=dep)
            rep = self.server.representation(dep)
            rep["links"]["latest_draft"] = f"{self.server.url}/api/deposit/depositions/{draft.id}"
            self._send(201, rep)
        elif action == "edit":
            if dep.state != STATE_DONE:
                self._error(400, "Deposit must be published before it can be edited." if dep.state == STATE_UNSUBMITTED else "Deposit is already in edit mode.")
                return
            dep.published_metadata = json.loads(json.dumps(dep.metadata))
            dep.state = STATE_INPROGRESS
            self._send(201, self.server.representation(dep))
        elif action == "discard":
            if dep.state != STATE_INPROGRESS:
                self._error(400, "Deposit is not in edit mode.")
                return
            dep.metadata = dep.published_metadata or dep.metadata
            dep.published_metadata = None
            dep.state = STATE_DONE
            self._send(201, self.server.representation(dep))

    def _publish(self, dep: Deposition) -> None:
        if dep.state == STATE_DONE:
            self._error(400, "Deposit is already published.")
            return
        errors = [
            {"field": f"metadata.{name}", "message": "Missing data for required field."}
            for name in REQUIRED_METADATA
            if not dep.metadata.get(name)
        ]
        if dep.state == STATE_UNSUBMITTED and not dep.files:
            errors.insert(0, {"field": "files", "message": "Missing uploaded files. Please add one or more files."})
        if errors:
            self._error(400, "Validation error.", errors)
            return
        dep.doi = f"{DOI_PREFIX}{dep.id}"
        dep.conceptdoi = dep.conceptdoi or f"{DOI_PREFIX}{dep.conceptrecid}"
        # Zenodo does not invent a version label; it is only present when
        # the depositor set one (OSPREY does for its own new versions).
        dep.state = STATE_DONE
        dep.published_metadata = None
        self._send(202, self.server.representation(dep))

    def _route_list_files(self, dep_id: str) -> None:
        dep = self._dep_or_404(dep_id)
        if dep is not None:
            self._send(200, [dict(f) for f in dep.files])

    def _route_delete_file(self, dep_id: str, file_id: str) -> None:
        dep = self._dep_or_404(dep_id)
        if dep is None:
            return
        before = len(dep.files)
        dep.files = [f for f in dep.files if f["id"] != file_id]
        if len(dep.files) == before:
            self._error(404, "File does not exist.")
            return
        self._send(204)

    def _route_upload(self, bucket: str, filename: str) -> None:
        dep_id = self.server.buckets.get(bucket)
        dep = self.server.depositions.get(dep_id) if dep_id is not None else None
        if dep is None:
            self._drain()
            self._error(404, "Bucket does not exist.")
            return
        if not dep.editable:
            self._drain()
            self._error(403, "Bucket is locked; the deposit is published.")
            return
        filename = unquote(filename)
        digest = hashlib.md5()  # noqa: S324 - Zenodo reports md5 checksums
        size = 0
        remaining = self._content_length()
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            remaining -= len(chunk)
        entry = {
            "id": uuid.uuid4().hex,
            "filename": filename,
            "filesize": size,
            "checksum": f"md5:{digest.hexdigest()}",
        }
        dep.files = [f for f in dep.files if f["filename"] != filename] + [entry]
        self._send(201, {"key": filename, "size": size, "checksum": entry["checksum"], "version_id": entry["id"]})

    def _public_record(self, dep_id: str) -> Deposition | None:
        """A record by id, or the latest version when given a concept id
        (Zenodo resolves concept ids to the newest version)."""
        dep = self.server.depositions.get(int(dep_id))
        if dep is None or dep.state == STATE_UNSUBMITTED:
            dep = self.server.latest_in_concept(int(dep_id))
        return dep

    def _route_record_json(self, dep_id: str) -> None:
        dep = self._public_record(dep_id)
        if dep is None:
            self._error(404, "PID does not exist.")
            return
        self._send(200, self.server.record_representation(dep))

    def _route_record_versions(self, dep_id: str) -> None:
        """Like Zenodo: newest first, paginated (default 10), with a
        `links.next` while more remain."""
        from urllib.parse import parse_qs
        dep = self._public_record(dep_id)
        if dep is None:
            self._error(404, "PID does not exist.")
            return
        params = parse_qs(urlparse(self.path).query)
        size = max(1, min(int(params.get("size", ["10"])[0] or 10), self.server.max_page_size))
        page = max(1, int(params.get("page", ["1"])[0] or 1))
        hits = sorted(
            (d for d in self.server.depositions.values() if d.conceptrecid == dep.conceptrecid and d.state != STATE_UNSUBMITTED),
            key=lambda d: d.version_index,
            reverse=True,
        )
        chunk = hits[(page - 1) * size: page * size]
        links = {"self": f"{self.server.url}/api/records/{dep_id}/versions?size={size}&page={page}"}
        if page * size < len(hits):
            links["next"] = f"{self.server.url}/api/records/{dep_id}/versions?size={size}&page={page + 1}"
        self._send(200, {"hits": {"hits": [self.server.record_representation(d) for d in chunk], "total": len(hits)}, "links": links})

    def _route_record_file(self, dep_id: str, filename: str) -> None:
        dep = self._public_record(dep_id)
        filename = unquote(filename)
        entry = next((f for f in (dep.files if dep else []) if f["filename"] == filename), None)
        if entry is None:
            self._error(404, "File does not exist.")
            return
        # Bodies are not stored; serve zeros of the recorded size so a
        # mirror sees the right byte count.
        self._send(200, raw=b"\0" * int(entry["filesize"]), content_type="application/octet-stream")

    def _route_record_search(self) -> None:
        from urllib.parse import parse_qs
        query = parse_qs(urlparse(self.path).query).get("q", [""])[0]
        hits = []
        for dep in self.server.depositions.values():
            if dep.state == STATE_UNSUBMITTED:
                continue
            if query.startswith("doi:") and dep.doi == query[4:].strip('"'):
                hits.append(dep)
            elif query.startswith("conceptdoi:") and dep.conceptdoi == query[11:].strip('"'):
                if self.server.latest_in_concept(dep.conceptrecid) is dep:
                    hits.append(dep)
        self._send(200, {"hits": {"hits": [self.server.record_representation(d) for d in hits], "total": len(hits)}})

    def _route_requests_list(self) -> None:
        from urllib.parse import parse_qs
        params = parse_qs(urlparse(self.path).query)
        status = params.get("status", [None])[0]
        hits = []
        for req in self.server.community_requests.values():
            if status and req.status != status:
                continue
            if req.community != self.server.community:
                continue
            dep = self.server.depositions.get(req.record_id)
            hits.append({
                "id": req.id,
                "status": req.status,
                "type": "community-inclusion",
                "receiver": {"community": req.community},
                "topic": {"record": str(req.record_id)},
                "record_doi": dep.doi if dep else "",
            })
        self._send(200, {"hits": {"hits": hits, "total": len(hits)}})

    def _route_request_action(self, request_id: str, action: str) -> None:
        self._drain()
        req = self.server.community_requests.get(request_id)
        if req is None:
            self._error(404, "Request does not exist.")
            return
        if req.status != "submitted":
            self._error(400, "Request is not open.")
            return
        req.status = "accepted" if action == "accept" else "declined"
        if action == "accept":
            dep = self.server.depositions.get(req.record_id)
            if dep is not None and req.community not in dep.communities:
                dep.communities.append(req.community)
        self._send(200, {"id": req.id, "status": req.status})

    def _route_record_html(self, dep_id: str) -> None:
        dep = self.server.depositions.get(int(dep_id))
        if dep is None or dep.state == STATE_UNSUBMITTED:
            self._send(404, raw=b"<html><body><h1>Record not found</h1></body></html>", content_type="text/html")
            return
        title = dep.metadata.get("title", "Untitled")
        html = (
            f"<html><head><title>{title} (fake Zenodo)</title></head><body>"
            f"<h1>{title}</h1><p>DOI: {dep.doi}</p><p>Concept DOI: {dep.conceptdoi}</p>"
            f"<ul>{''.join(f'<li>{f['filename']} ({f['filesize']} bytes)</li>' for f in dep.files)}</ul>"
            "</body></html>"
        ).encode("utf-8")
        self._send(200, raw=html, content_type="text/html")


# -- test entry points -------------------------------------------------------


@contextlib.contextmanager
def fake_zenodo(token: str = "fake-token"):
    """Start a fake Zenodo and point the Zenodo settings at it.

    Usage::

        with fake_zenodo() as fz:
            deposit = publish_project_now(project, user)
            assert fz.deposition(deposit.deposition_id).state == "done"
    """
    server = FakeZenodoServer(token=token).start()
    override = override_settings(
        ZENODO_API_BASE_URL=server.url,
        ZENODO_ACCESS_TOKEN=token,
        ZENODO_USE_SANDBOX=True,
    )
    override.enable()
    try:
        yield server
    finally:
        override.disable()
        server.stop()


class FakeZenodoMixin:
    """TestCase mixin: `self.fz` is a fresh fake for every test."""

    fake_zenodo_token = "fake-token"

    def setUp(self):
        super().setUp()
        stack = contextlib.ExitStack()
        self.fz = stack.enter_context(fake_zenodo(token=self.fake_zenodo_token))
        self.addCleanup(stack.close)
