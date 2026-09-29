"""A fake GitHub REST API for tests: repos and READMEs, in memory.

Same rules as fake Zenodo: exists only inside a test run, never as a
standing service. Usage::

    with fake_github() as gh:
        gh.seed_repo("acme/pump", description="...", license="MIT")
"""
from __future__ import annotations

import contextlib
import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.test.utils import override_settings


@dataclass
class Repo:
    full_name: str
    description: str = ""
    license: str = ""  # SPDX id, "" for none, "NOASSERTION" for unrecognized
    topics: list[str] = field(default_factory=list)
    homepage: str = ""
    readme: str = ""
    private: bool = False

    def as_json(self) -> dict:
        owner, name = self.full_name.split("/", 1)
        return {
            "full_name": self.full_name,
            "name": name,
            "owner": {"login": owner},
            "description": self.description,
            "html_url": f"https://github.com/{self.full_name}",
            "homepage": self.homepage,
            "topics": self.topics,
            "private": self.private,
            "license": {"spdx_id": self.license, "key": self.license.lower()} if self.license else None,
            "pushed_at": "2026-06-21T03:45:23Z",
            "stargazers_count": 12,
            "archived": False,
            "default_branch": "main",
        }


def _tiny_png() -> bytes:
    """A small solid PNG for cover-image tests, built with Pillow so it
    always decodes."""
    import io
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 36), (30, 90, 160)).save(buf, format="PNG")
    return buf.getvalue()


_PNG = _tiny_png()


class FakeGitHubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.repos: dict[str, Repo] = {}
        self.fail_status: int | None = None
        self.requests: list[str] = []
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeGitHubServer":
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def seed_repo(self, full_name: str, **kwargs) -> Repo:
        repo = Repo(full_name=full_name, **kwargs)
        self.repos[full_name.lower()] = repo
        return repo


class _Handler(BaseHTTPRequestHandler):
    server: FakeGitHubServer  # type: ignore[assignment]

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
        if self.server.fail_status:
            self._send(self.server.fail_status, json.dumps({"message": "injected"}).encode())
            return
        if self.path == "/image.png":
            self._send(200, _PNG, content_type="image/png")
            return
        if self.path == "/not-an-image":
            self._send(200, b"<html>nope</html>", content_type="text/html")
            return
        m = re.match(r"^/repos/([^/]+)/([^/]+)(/readme)?$", self.path)
        if not m:
            self._send(404, json.dumps({"message": "Not Found"}).encode())
            return
        repo = self.server.repos.get(f"{m.group(1)}/{m.group(2)}".lower())
        if repo is None:
            self._send(404, json.dumps({"message": "Not Found"}).encode())
            return
        if m.group(3):
            if not repo.readme:
                self._send(404, json.dumps({"message": "Not Found"}).encode())
                return
            self._send(200, repo.readme.encode("utf-8"), content_type="application/vnd.github.raw+json")
            return
        self._send(200, json.dumps(repo.as_json()).encode())


@contextlib.contextmanager
def fake_github():
    server = FakeGitHubServer().start()
    override = override_settings(GITHUB_API_BASE_URL=server.url, GITHUB_API_TOKEN="")
    override.enable()
    try:
        yield server
    finally:
        override.disable()
        server.stop()


class FakeGitHubMixin:
    def setUp(self):
        super().setUp()
        stack = contextlib.ExitStack()
        self.gh = stack.enter_context(fake_github())
        self.addCleanup(stack.close)
