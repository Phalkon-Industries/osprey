"""Zenodo deposit integration for OSPREY projects."""
from __future__ import annotations

import html
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib import error, parse, request

from django.conf import settings
from django.utils import timezone

from .models import ArtifactLink, Project, ProjectDeposit


class ZenodoError(RuntimeError):
    """Raised when the Zenodo API rejects a request or config is missing."""


@dataclass(frozen=True)
class ProjectArchive:
    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


class ZenodoClient:
    """Small stdlib HTTP client for Zenodo's deposit API."""

    def __init__(self, base_url: str, access_token: str, timeout: int = 30):
        if not access_token:
            raise ZenodoError("ZENODO_ACCESS_TOKEN is not configured.")
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout = timeout

    @classmethod
    def from_settings(cls) -> "ZenodoClient":
        return cls(settings.ZENODO_API_BASE_URL, settings.ZENODO_ACCESS_TOKEN)

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        payload: dict[str, Any] | bytes | None = None,
        content_type: str = "application/json",
        expected: tuple[int, ...] = (200,),
    ) -> dict[str, Any]:
        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        data: bytes | None
        if payload is None:
            data = None
        elif isinstance(payload, bytes):
            data = payload
        else:
            data = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "User-Agent": "OSPREY Zenodo sandbox integration (https://osprey.phalkon.io/)",
        }
        if data is not None:
            headers["Content-Type"] = content_type
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                if response.status not in expected:
                    raise ZenodoError(f"Zenodo returned HTTP {response.status}: {body[:500]}")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ZenodoError(f"Zenodo returned HTTP {exc.code}: {body[:800]}") from exc
        except error.URLError as exc:
            raise ZenodoError(f"Could not reach Zenodo: {exc.reason}") from exc
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ZenodoError(f"Zenodo returned non-JSON response: {body[:500]}") from exc

    def create_deposition(self) -> dict[str, Any]:
        return self._request("POST", "/api/deposit/depositions", payload={}, expected=(201,))

    def update_deposition_metadata(self, deposition_id: str, metadata: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/api/deposit/depositions/{deposition_id}",
            payload={"metadata": metadata},
            expected=(200,),
        )

    def upload_to_bucket(self, bucket_url: str, archive: ProjectArchive) -> dict[str, Any]:
        quoted_filename = parse.quote(archive.filename)
        upload_url = f"{bucket_url.rstrip('/')}/{quoted_filename}"
        return self._request(
            "PUT",
            upload_url,
            payload=archive.content,
            content_type=archive.content_type,
            expected=(200, 201),
        )

    def publish_deposition(self, deposition_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/deposit/depositions/{deposition_id}/actions/publish",
            payload={},
            expected=(200, 202),
        )


def zenodo_configured() -> bool:
    return bool(settings.ZENODO_ACCESS_TOKEN)


def zenodo_mode_label() -> str:
    return "Zenodo sandbox" if settings.ZENODO_USE_SANDBOX else "Zenodo"


def _project_upload_type(project: Project) -> str:
    artifact_type = (project.artifact_type or "").lower()
    if any(word in artifact_type for word in ["software", "firmware", "analysis", "pipeline"]):
        return "software"
    if "data" in artifact_type:
        return "dataset"
    return "other"


def _zenodo_license(project: Project) -> str:
    mapping = {
        "MIT": "mit-license",
        "Apache-2.0": "apache-2.0",
        "BSD-2-Clause": "bsd-2-clause",
        "BSD-3-Clause": "bsd-3-clause",
        "GPL-3.0-only": "gpl-3.0",
        "GPL-3.0-or-later": "gpl-3.0",
        "CC-BY-4.0": "cc-by-4.0",
        "CC0-1.0": "cc0-1.0",
    }
    return mapping.get(project.license, "")


def _creator_rows(project: Project) -> list[dict[str, str]]:
    creators: list[dict[str, str]] = []
    for contribution in project.contributions.select_related("user").all():
        name = (contribution.display_name or "").strip()
        if not name:
            continue
        row = {"name": name}
        orcid = contribution.verified_orcid_id
        if orcid:
            row["orcid"] = orcid
        creators.append(row)
    if creators:
        return creators
    user = project.created_by
    if user is not None:
        try:
            display_name = user.profile.display_name
        except Exception:
            display_name = ""
        name = display_name or user.get_full_name() or user.get_username()
        return [{"name": name}]
    return [{"name": "OSPREY contributors"}]


def _description(project: Project) -> str:
    text = project.summary or project.description or project.readme or project.title
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    html_paragraphs = "".join(f"<p>{html.escape(p)}</p>" for p in paragraphs[:3])
    source = project.canonical_url or project.get_absolute_url()
    return html_paragraphs + f"<p>OSPREY project record: {html.escape(source)}</p>"


def metadata_for_project(project: Project) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "title": project.title,
        "upload_type": _project_upload_type(project),
        "description": _description(project),
        "creators": _creator_rows(project),
        "access_right": "open",
        "publication_date": date.today().isoformat(),
        "keywords": sorted(
            {
                value
                for value in [project.field, project.artifact_type, *project.tags.values_list("name", flat=True)]
                if value
            }
        ),
    }
    license_id = _zenodo_license(project)
    if license_id:
        metadata["license"] = license_id
    if settings.ZENODO_DEFAULT_COMMUNITY:
        metadata["communities"] = [{"identifier": settings.ZENODO_DEFAULT_COMMUNITY}]
    return metadata


def build_project_archive(project: Project) -> ProjectArchive:
    contributors = [
        {
            "display_name": c.display_name,
            "role": c.role,
            "credit_statement": c.credit_statement,
            "orcid_id": c.verified_orcid_id,
        }
        for c in project.contributions.select_related("user").all()
    ]
    project_json = {
        "title": project.title,
        "slug": project.slug,
        "summary": project.summary,
        "description": project.description,
        "field": project.field,
        "artifact_type": project.artifact_type,
        "license": project.license,
        "doi": project.doi,
        "canonical_url": project.canonical_url,
        "institution": project.institution,
        "contributors": contributors,
        "tags": list(project.tags.values_list("name", flat=True)),
        "exported_at": timezone.now().isoformat(),
    }
    citation = [
        "cff-version: 1.2.0",
        f"title: {json.dumps(project.title)}",
        "message: Cite this OSPREY project record.",
    ]
    if project.doi:
        citation.append(f"doi: {json.dumps(project.doi)}")
    if project.canonical_url:
        citation.append(f"url: {json.dumps(project.canonical_url)}")
    readme = project.readme or f"# {project.title}\n\n{project.summary}\n"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.md", readme)
        archive.writestr("osprey-project.json", json.dumps(project_json, indent=2, default=str) + "\n")
        archive.writestr("CITATION.cff", "\n".join(citation) + "\n")
    return ProjectArchive(filename=f"osprey-{project.slug}-snapshot.zip", content=buffer.getvalue())


def _extract_zenodo_ids(response: dict[str, Any]) -> dict[str, str]:
    metadata = response.get("metadata") or {}
    reserved = metadata.get("prereserve_doi") or {}
    links = response.get("links") or {}
    return {
        "deposition_id": str(response.get("id") or ""),
        "bucket_url": links.get("bucket") or "",
        "record_id": str(response.get("record_id") or ""),
        "concept_id": str(response.get("conceptrecid") or response.get("concept_id") or ""),
        "doi": metadata.get("doi") or reserved.get("doi") or "",
        "concept_doi": metadata.get("conceptdoi") or "",
    }


def sync_project_to_zenodo(project: Project, user) -> ProjectDeposit:
    """Create/update a Zenodo draft and upload an OSPREY snapshot ZIP."""
    client = ZenodoClient.from_settings()
    deposit, created = ProjectDeposit.objects.get_or_create(
        project=project,
        provider=ProjectDeposit.PROVIDER_ZENODO,
        sandbox=settings.ZENODO_USE_SANDBOX,
        defaults={"created_by": user if getattr(user, "is_authenticated", False) else None},
    )
    try:
        if created or not deposit.deposition_id or deposit.state != ProjectDeposit.STATE_DRAFT:
            created_response = client.create_deposition()
            ids = _extract_zenodo_ids(created_response)
            deposit.deposition_id = ids["deposition_id"]
            deposit.bucket_url = ids["bucket_url"]
            deposit.record_id = ids["record_id"]
            deposit.concept_id = ids["concept_id"]
            deposit.doi = ids["doi"]
            deposit.concept_doi = ids["concept_doi"]
            deposit.state = ProjectDeposit.STATE_DRAFT

        metadata_response = client.update_deposition_metadata(deposit.deposition_id, metadata_for_project(project))
        archive = build_project_archive(project)
        if not deposit.bucket_url:
            ids = _extract_zenodo_ids(metadata_response)
            deposit.bucket_url = ids["bucket_url"]
        if not deposit.bucket_url:
            raise ZenodoError("Zenodo did not return a file bucket URL for this deposition.")
        client.upload_to_bucket(deposit.bucket_url, archive)

        ids = _extract_zenodo_ids(metadata_response)
        deposit.record_id = ids["record_id"] or deposit.record_id
        deposit.concept_id = ids["concept_id"] or deposit.concept_id
        deposit.doi = ids["doi"] or deposit.doi
        deposit.concept_doi = ids["concept_doi"] or deposit.concept_doi
        deposit.last_response = metadata_response
        deposit.last_error = ""
        deposit.state = ProjectDeposit.STATE_DRAFT
        deposit.save()
        if deposit.doi and (not project.doi or project.doi == deposit.doi):
            project.doi = deposit.doi
            project.save(update_fields=["doi"])
        return deposit
    except ZenodoError as exc:
        deposit.state = ProjectDeposit.STATE_ERROR
        deposit.last_error = str(exc)
        deposit.save(update_fields=["state", "last_error", "updated_at"])
        raise


def publish_project_deposit(deposit: ProjectDeposit) -> ProjectDeposit:
    if deposit.provider != ProjectDeposit.PROVIDER_ZENODO:
        raise ZenodoError("Only Zenodo deposits can be published here.")
    if not deposit.deposition_id:
        raise ZenodoError("Create a Zenodo draft before publishing.")
    client = ZenodoClient.from_settings()
    try:
        response = client.publish_deposition(deposit.deposition_id)
        ids = _extract_zenodo_ids(response)
        deposit.record_id = ids["record_id"] or deposit.record_id
        deposit.concept_id = ids["concept_id"] or deposit.concept_id
        deposit.doi = ids["doi"] or deposit.doi
        deposit.concept_doi = ids["concept_doi"] or deposit.concept_doi
        deposit.state = ProjectDeposit.STATE_PUBLISHED
        deposit.published_at = timezone.now()
        deposit.last_response = response
        deposit.last_error = ""
        deposit.save()

        project = deposit.project
        if deposit.doi:
            project.doi = deposit.doi
            project.save(update_fields=["doi"])
        if deposit.external_url:
            ArtifactLink.objects.update_or_create(
                project=project,
                kind="zenodo",
                url=deposit.external_url,
                defaults={"label": zenodo_mode_label()},
            )
        return deposit
    except ZenodoError as exc:
        deposit.state = ProjectDeposit.STATE_ERROR
        deposit.last_error = str(exc)
        deposit.save(update_fields=["state", "last_error", "updated_at"])
        raise