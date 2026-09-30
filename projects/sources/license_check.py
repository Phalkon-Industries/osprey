"""License audit: is the license OSPREY shows the license the files carry?

For every native and registered project, compare OSPREY's license with
the Zenodo record's license and with the detected license of every GitHub
repository the project links (canonical URL and artifact links). Store
the result on the project, tell the owner when the findings change, and
list open findings for staff. A wrong license on OSPREY is the one error
the catalog can't afford (decided 2026-09-29).
"""
from __future__ import annotations

import hashlib
import logging
import time

from django.urls import reverse
from django.utils import timezone

from projects import zenodo_register as zr
from projects.models import ArtifactLink, Project, ProjectDeposit

from . import github_source
from .records import GITHUB_RE, SourceError, normalize_license

logger = logging.getLogger(__name__)


def _family(key: str) -> str:
    return (key or "").replace("-or-later", "").replace("-only", "").lower()


def repos_of(project: Project) -> list[str]:
    """GitHub owner/repo names the project points at, deduped, in order."""
    urls = [project.canonical_url] + list(
        ArtifactLink.objects.filter(project=project).values_list("url", flat=True)
    )
    out: list[str] = []
    for url in urls:
        m = GITHUB_RE.search(url or "")
        if not m:
            continue
        name = m.group(2)[:-4] if m.group(2).endswith(".git") else m.group(2)
        full = f"{m.group(1)}/{name}"
        if full.lower() not in {o.lower() for o in out}:
            out.append(full)
    return out


def zenodo_license(project: Project) -> tuple[str, str]:
    """(normalized key, raw) from the stored Zenodo response, or ("", "")
    when the project has no published record."""
    deposit = (
        project.deposits.filter(provider=ProjectDeposit.PROVIDER_ZENODO, state=ProjectDeposit.STATE_PUBLISHED)
        .order_by("-published_at").first()
    )
    if deposit is None:
        return "", ""
    raw = zr.license_of(deposit.last_response or {})
    return normalize_license(raw) or "", raw


def check_project(project: Project, *, fetch_github: bool = True) -> dict:
    osprey = (project.license or "").strip()
    findings: list[dict] = []
    z_key, z_raw = zenodo_license(project)
    if z_raw or z_key:
        if not z_key:
            findings.append({"kind": "zenodo_unknown", "text": f"The Zenodo record's license ({z_raw}) isn't one OSPREY recognizes."})
        elif osprey and _family(z_key) != _family(osprey):
            findings.append({"kind": "zenodo_mismatch", "text": f"OSPREY lists {osprey}; the Zenodo record says {z_key}."})
    github: dict[str, str] = {}
    if fetch_github:
        for full in repos_of(project):
            try:
                rec = github_source.fetch(full)
            except SourceError as exc:
                github[full] = f"error: {exc}"
                findings.append({"kind": "github_unreachable", "text": f"The repository {full} couldn't be read: {exc}"})
                continue
            github[full] = rec.license or ""
            if not rec.license:
                findings.append({"kind": "github_missing", "text": f"The repository {full} has no license file."})
            elif osprey and _family(rec.license) != _family(osprey):
                findings.append({"kind": "github_mismatch", "text": f"OSPREY lists {osprey}; the repository {full} says {rec.license}."})
    if not osprey and (z_key or any(v and not v.startswith("error") for v in github.values())):
        findings.append({"kind": "osprey_missing", "text": "The project has no license set on OSPREY."})
    return {
        "osprey": osprey,
        "zenodo": z_key or z_raw,
        "github": github,
        "findings": findings,
        "checked_at": timezone.now().isoformat(),
    }


def _fingerprint(findings: list[dict]) -> str:
    return hashlib.sha1("|".join(sorted(f["text"] for f in findings)).encode("utf-8")).hexdigest()[:12]


def _tell_owner(project: Project, findings: list[dict]) -> None:
    if project.created_by_id is None or not findings:
        return
    from notifications.events import _emit

    _emit(
        project.created_by,
        kind="license_check",
        title=f"License check: {project.title}"[:200],
        body=" ".join(f["text"] for f in findings)[:1000],
        url=reverse("projects:edit", args=[project.slug]),
        dedup_key=f"license_check:{project.pk}:{_fingerprint(findings)}",
        project_slug=project.slug,
    )


def audit(project: Project, *, notify: bool = True, fetch_github: bool = True) -> dict:
    """Check one project, store the result, notify the owner if the findings
    are new or changed. Returns the stored result."""
    previous = _fingerprint(project.license_findings)
    result = check_project(project, fetch_github=fetch_github)
    project.license_check = result
    project.license_checked_at = timezone.now()
    project.save(update_fields=["license_check", "license_checked_at"])
    if notify and result["findings"] and _fingerprint(result["findings"]) != previous:
        _tell_owner(project, result["findings"])
    return result


def auditable() -> "QuerySet[Project]":  # noqa: F821
    return Project.objects.filter(origin__in=[Project.ORIGIN_NATIVE, Project.ORIGIN_REGISTERED], visibility=Project.VISIBILITY_PUBLIC).order_by("pk")


def run_audit(projects, *, pause: float = 0.5, notify: bool = True) -> dict:
    counts = {"checked": 0, "with_findings": 0}
    rows = list(projects)
    for n, project in enumerate(rows, 1):
        result = audit(project, notify=notify)
        counts["checked"] += 1
        if result["findings"]:
            counts["with_findings"] += 1
        if pause and n < len(rows) and result["github"]:
            time.sleep(pause)
    return counts
