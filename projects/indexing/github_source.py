"""GitHub repositories as an index source."""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings

from .records import Author, Gate, SourceError, SourceRecord, normalize_license, summary_from_readme, user_agent


def _base() -> str:
    return getattr(settings, "GITHUB_API_BASE_URL", "https://api.github.com").rstrip("/")


def _get(path: str, accept: str = "application/vnd.github+json") -> bytes:
    headers = {"Accept": accept, "User-Agent": user_agent()}
    token = getattr(settings, "GITHUB_API_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(f"{_base()}{path}", headers=headers)
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.read()
    except HTTPError as exc:
        if exc.code == 404:
            raise SourceError("That repository doesn't exist or is private.") from exc
        raise SourceError(f"GitHub returned HTTP {exc.code}.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SourceError(f"Could not reach GitHub: {exc}") from exc


def fetch(full_name: str) -> SourceRecord:
    try:
        repo = json.loads(_get(f"/repos/{full_name}").decode("utf-8"))
    except ValueError as exc:
        raise SourceError("Unexpected response from GitHub.") from exc
    if repo.get("private"):
        raise SourceError("That repository is private.")
    spdx = ((repo.get("license") or {}).get("spdx_id") or "").strip()
    raw_license = "" if spdx in ("", "NOASSERTION") else spdx
    license_key = normalize_license(raw_license)
    readme = ""
    try:
        readme = _get(f"/repos/{full_name}/readme", accept="application/vnd.github.raw+json").decode("utf-8", "replace")
    except SourceError:
        readme = ""
    description = (repo.get("description") or "").strip()
    html_url = repo.get("html_url") or f"https://github.com/{full_name}"
    owner = (repo.get("owner") or {}).get("login") or full_name.split("/")[0]
    rec = SourceRecord(
        source="github",
        external_id=repo.get("full_name") or full_name,
        title=repo.get("name") or full_name.split("/")[-1],
        canonical_url=html_url,
        # GitHub's About text when the repo has one, else the README's first
        # paragraph of prose (headings, badges and HTML skipped).
        summary=(description or summary_from_readme(readme))[:280],
        readme=readme[:20000] if readme else description,
        license_raw=spdx,
        license=license_key,
        files_url=html_url,
        files_url_source="repo",
        authors=[Author(owner)],
        keywords=list(repo.get("topics") or [])[:6],
        text_license=license_key,
        artifact_type="Software",
        raw={k: repo.get(k) for k in ("full_name", "html_url", "homepage", "pushed_at", "stargazers_count", "archived", "default_branch")},
    )
    rec.gate_license = Gate(bool(license_key), spdx or "no license file", "GitHub repository license (SPDX)")
    rec.gate_files = Gate(True, "repository", "GitHub")
    return rec
