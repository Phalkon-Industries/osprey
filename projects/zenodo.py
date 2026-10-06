"""Zenodo deposit integration for OSPREY projects."""
from __future__ import annotations

import logging
import html
import io
import http.client
import json
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib import error, parse, request

from django.conf import settings
from django.utils import timezone

from .models import ArtifactLink, Project, ProjectDeposit, ProjectDepositVersion


logger = logging.getLogger(__name__)


class ZenodoError(RuntimeError):
    """Raised when the Zenodo API rejects a request or config is missing."""


class LicenseNotSendable(ZenodoError):
    """The project's license has no Zenodo id. Zenodo applies CC BY 4.0 to
    an open record sent without one, so OSPREY sends nothing at all."""


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
        return cls(
            settings.ZENODO_API_BASE_URL,
            settings.ZENODO_ACCESS_TOKEN,
            timeout=getattr(settings, "ZENODO_TIMEOUT_SECONDS", 30),
        )

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
        except (error.URLError, OSError, http.client.HTTPException) as exc:
            # urllib wraps connect/send failures in URLError but lets read
            # failures (a hang past the timeout, a dropped connection) escape
            # as raw TimeoutError / RemoteDisconnected. Fold them all into
            # ZenodoError so callers have one thing to catch.
            reason = getattr(exc, "reason", None) or exc
            raise ZenodoError(f"Could not reach Zenodo: {reason}") from exc
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

    def stream_to_bucket(
        self,
        bucket_url: str,
        filename: str,
        file_obj,
        size: int,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        """PUT a file to a Zenodo bucket without holding the whole body in RAM.

        ``file_obj`` is read in 1 MiB chunks. ``size`` must equal the
        total bytes available; Zenodo's bucket endpoint requires a
        Content-Length header. Returns the parsed JSON response.
        """
        quoted_filename = parse.quote(filename)
        upload_url = f"{bucket_url.rstrip('/')}/{quoted_filename}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "Content-Type": content_type,
            "Content-Length": str(size),
            "User-Agent": "OSPREY Zenodo sandbox integration (https://osprey.phalkon.io/)",
        }
        # urllib.request will read file-like objects when Content-Length is
        # provided, sending in fixed-size chunks rather than buffering.
        req = request.Request(upload_url, data=file_obj, headers=headers, method="PUT")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
                if response.status not in (200, 201):
                    raise ZenodoError(f"Zenodo returned HTTP {response.status}: {body[:500]}")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ZenodoError(f"Zenodo returned HTTP {exc.code}: {body[:800]}") from exc
        except (error.URLError, OSError, http.client.HTTPException) as exc:
            # urllib wraps connect/send failures in URLError but lets read
            # failures (a hang past the timeout, a dropped connection) escape
            # as raw TimeoutError / RemoteDisconnected. Fold them all into
            # ZenodoError so callers have one thing to catch.
            reason = getattr(exc, "reason", None) or exc
            raise ZenodoError(f"Could not reach Zenodo: {reason}") from exc
        if not body:
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ZenodoError(f"Zenodo returned non-JSON response: {body[:500]}") from exc

    def publish_deposition(self, deposition_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/deposit/depositions/{deposition_id}/actions/publish",
            payload={},
            expected=(200, 202),
        )

    def get_deposition(self, deposition_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/deposit/depositions/{deposition_id}", expected=(200,))

    def create_new_version(self, deposition_id: str) -> dict[str, Any]:
        """Open a new-version draft from a published deposition.

        Zenodo's `actions/newversion` returns the *previous* (published)
        deposition with a `links.latest_draft` pointer. We follow that
        link with a GET so the caller gets the brand-new draft directly.
        """
        response = self._request(
            "POST",
            f"/api/deposit/depositions/{deposition_id}/actions/newversion",
            payload={},
            expected=(201, 202),
        )
        latest_draft = (response.get("links") or {}).get("latest_draft")
        if not latest_draft:
            raise ZenodoError("Zenodo did not return a latest_draft link for the new version.")
        return self._request("GET", latest_draft, expected=(200,))

    def list_deposition_files(self, deposition_id: str) -> list[dict[str, Any]]:
        response = self._request(
            "GET",
            f"/api/deposit/depositions/{deposition_id}/files",
            expected=(200,),
        )
        # The endpoint returns a list, but `_request` wraps to a dict only
        # if JSON object. urlopen returned bytes parsed via json.loads, so
        # response is whatever JSON returned. Handle both shapes.
        if isinstance(response, list):
            return response
        if isinstance(response, dict):
            return list(response.get("files") or [])
        return []

    def delete_deposition_file(self, deposition_id: str, file_id: str) -> None:
        self._request(
            "DELETE",
            f"/api/deposit/depositions/{deposition_id}/files/{file_id}",
            expected=(204, 200),
        )

    def delete_deposition(self, deposition_id: str) -> None:
        """Delete an unpublished deposition. Zenodo refuses (403) once published."""
        self._request(
            "DELETE",
            f"/api/deposit/depositions/{deposition_id}",
            expected=(204, 200),
        )

    def edit_published_deposition(self, deposition_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/deposit/depositions/{deposition_id}/actions/edit",
            payload={},
            expected=(200, 201),
        )

    def discard_deposition_changes(self, deposition_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/deposit/depositions/{deposition_id}/actions/discard",
            payload={},
            expected=(200, 201),
        )


def zenodo_configured() -> bool:
    return bool(settings.ZENODO_ACCESS_TOKEN)


def zenodo_mode_label() -> str:
    return "Zenodo sandbox" if settings.ZENODO_USE_SANDBOX else "Zenodo"


def discard_draft_depositions(project: Project) -> int:
    """Best effort: delete the project's never-published Zenodo depositions.

    A draft whose publish failed can leave an unsubmitted deposition on the
    OSPREY Zenodo account. Called when the owner deletes the draft. Zenodo
    being down or the deposition already gone must not stop the deletion,
    so every ZenodoError is logged and swallowed. Returns how many were
    removed.
    """
    if not zenodo_configured():
        return 0
    candidates = project.deposits.exclude(deposition_id="").exclude(
        state=ProjectDeposit.STATE_PUBLISHED
    ).filter(doi="")
    if not candidates.exists():
        return 0
    client = ZenodoClient.from_settings()
    removed = 0
    for deposit in candidates:
        try:
            client.delete_deposition(deposit.deposition_id)
        except ZenodoError as exc:
            logger.warning(
                "Could not delete draft deposition %s for project %s: %s",
                deposit.deposition_id, project.slug, exc,
            )
            continue
        removed += 1
    return removed


def _project_upload_type(project: Project) -> str:
    """Map an OSPREY artifact type to Zenodo's `upload_type` vocabulary.

    Zenodo accepts: publication, poster, presentation, dataset, image,
    video, software, lesson, physicalobject, other. Hardware design
    files are deposited as `other`: `physicalobject` describes the
    built object, not its design files, and Zenodo's citation formatter
    renders it as "[Graphic]". OSPREY's own citation carries the honest
    descriptor ("Hardware design") instead.
    """
    artifact_type = (project.artifact_type or "").lower()
    if not artifact_type:
        return "other"
    if "hardware" in artifact_type or "pcb" in artifact_type or "mechanical" in artifact_type:
        return "other"
    if any(word in artifact_type for word in ["software", "firmware", "library", "analysis", "pipeline"]):
        return "software"
    if "data" in artifact_type:
        return "dataset"
    if "protocol" in artifact_type or "method" in artifact_type or "documentation" in artifact_type:
        return "publication"
    return "other"


def _project_publication_type(project: Project) -> str:
    """Sub-type for `upload_type=publication`. Zenodo expects this field."""
    artifact_type = (project.artifact_type or "").lower()
    if "protocol" in artifact_type or "method" in artifact_type:
        return "workingpaper"
    if "documentation" in artifact_type:
        return "report"
    return "other"


def _zenodo_license(project: Project) -> str:
    return zenodo_license_id(project.license)


def zenodo_license_id(license: str) -> str:
    """Zenodo's id for an OSPREY license, or "" when there is none."""
    mapping = {
        "MIT": "mit-license",
        "Apache-2.0": "apache-2.0",
        "BSD-2-Clause": "bsd-2-clause",
        "BSD-3-Clause": "bsd-3-clause",
        "AGPL-3.0": "agpl-3.0",
        "GPL-3.0": "gpl-3.0",
        "GPL-3.0-only": "gpl-3.0",
        "GPL-3.0-or-later": "gpl-3.0",
        "LGPL-3.0": "lgpl-3.0",
        "MPL-2.0": "mpl-2.0",
        "CC-BY-4.0": "cc-by-4.0",
        "CC-BY-SA-4.0": "cc-by-sa-4.0",
        "CC0-1.0": "cc0-1.0",
        # Hardware licenses. Zenodo's vocabulary has them under these ids;
        # without them a CERN-OHL project was deposited with no license and
        # Zenodo applied its default (CC-BY) to the record.
        "CERN-OHL-P-2.0": "cern-ohl-p-2.0",
        "CERN-OHL-S-2.0": "cern-ohl-s-2.0",
        "CERN-OHL-W-2.0": "cern-ohl-w-2.0",
    }
    return mapping.get(license or "", "")


def _require_license(project: Project) -> str:
    license_id = _zenodo_license(project)
    if not license_id:
        raise LicenseNotSendable("The project has no license Zenodo accepts, so nothing was sent.")
    return license_id


def _creator_rows(project: Project) -> list[dict[str, str]]:
    creators: list[dict[str, str]] = []
    for contribution in project.credited_contributions.select_related("user").all():
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


def _osprey_permalink(project: Project) -> str:
    base = getattr(settings, "OSPREY_PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}{project.get_permalink()}" if base else project.get_permalink()


def _related_identifiers(project: Project, deposit: ProjectDeposit | None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(identifier: str, relation: str) -> None:
        identifier = (identifier or "").strip()
        if not identifier or identifier in seen:
            return
        seen.add(identifier)
        rows.append({"identifier": identifier, "relation": relation, "scheme": "url"})

    _add(_osprey_permalink(project), "isAlternateIdentifier")
    _add(project.canonical_url, "isSupplementTo")
    if deposit is not None:
        _add(deposit.repo_link, "isSupplementTo")
        latest = deposit.latest_version
        if latest is not None:
            _add(latest.repo_link, "isSupplementTo")
    return rows


def metadata_for_project(project: Project, deposit: ProjectDeposit | None = None) -> dict[str, Any]:
    upload_type = _project_upload_type(project)
    metadata: dict[str, Any] = {
        "title": project.title,
        "upload_type": upload_type,
        "description": _description(project),
        "creators": _creator_rows(project),
        "access_right": "open",
        "publication_date": date.today().isoformat(),
        # OSPREY's role in the record, in DataCite's own vocabulary:
        # the platform that hosts and distributes it. Zenodo stays the
        # publisher of record; this is how "OSPREY; Zenodo" in the
        # citation is backed by the metadata.
        "contributors": [
            {"name": "OSPREY", "type": "HostingInstitution"},
            {"name": "OSPREY", "type": "Distributor"},
        ],
        "keywords": sorted(
            {
                value
                for value in [project.field, project.artifact_type, *project.tags.values_list("name", flat=True)]
                if value
            }
        ),
    }
    if upload_type == "publication":
        metadata["publication_type"] = _project_publication_type(project)
    metadata["license"] = _require_license(project)
    if settings.ZENODO_DEFAULT_COMMUNITY:
        metadata["communities"] = [{"identifier": settings.ZENODO_DEFAULT_COMMUNITY}]
    related = _related_identifiers(project, deposit)
    if related:
        metadata["related_identifiers"] = related
    if deposit is not None and deposit.pending_changelog:
        metadata["version"] = f"v{deposit.next_version_index}"
        metadata["notes"] = deposit.pending_changelog
    if project.provided_as_is:
        note = "Provided as-is: no support or updates are planned."
        metadata["notes"] = (metadata.get("notes") + "\n\n" + note) if metadata.get("notes") else note
    return metadata


def _osprey_metadata_payload(project: Project) -> dict[str, Any]:
    """The osprey-project.json sidecar dropped into uploaded archives."""
    contributors = [
        {
            "display_name": c.display_name,
            "role": c.role,
            "credit_statement": c.credit_statement,
            "orcid_id": c.verified_orcid_id,
        }
        for c in project.credited_contributions.select_related("user").all()
    ]
    return {
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
        "provided_as_is": project.provided_as_is,
        "contributors": contributors,
        "tags": list(project.tags.values_list("name", flat=True)),
        "osprey_url": _osprey_permalink(project),
        "exported_at": timezone.now().isoformat(),
    }


def _osprey_citation_cff(project: Project) -> str:
    """A small CITATION.cff describing the OSPREY project record."""
    lines = [
        "cff-version: 1.2.0",
        f"title: {json.dumps(project.title)}",
        "message: Cite this OSPREY project record.",
    ]
    if project.doi:
        lines.append(f"doi: {json.dumps(project.doi)}")
    osprey_url = _osprey_permalink(project)
    if osprey_url:
        lines.append(f"url: {json.dumps(osprey_url)}")
    if project.canonical_url:
        lines.append(f"repository-code: {json.dumps(project.canonical_url)}")
    return "\n".join(lines) + "\n"


def _clear_existing_zenodo_files(client: ZenodoClient, deposition_id: str) -> None:
    """Remove every file attached to a draft deposition.

    Used when starting a new version: Zenodo carries the previous
    version's files into the new draft, but OSPREY's UX is that a new
    version requires a fresh archive upload, so we clear the slate.
    Failures are swallowed to keep the publish path resilient; if a
    leftover file remains it will simply appear in the new record.
    """
    if not deposition_id:
        return
    try:
        files = client.list_deposition_files(deposition_id)
    except ZenodoError:
        return
    for entry in files:
        file_id = entry.get("id") or entry.get("file_id")
        if not file_id:
            continue
        try:
            client.delete_deposition_file(deposition_id, str(file_id))
        except ZenodoError:
            continue


def _upload_project_attachments(client, deposit, project) -> None:
    """Push every not-yet-uploaded ProjectAttachment to the Zenodo bucket.

    The user's archive is streamed to Zenodo without being fully read
    into RAM, then small OSPREY sidecar files (`osprey-project.json`,
    `CITATION.cff`) are uploaded as siblings so the Zenodo record carries
    OSPREY context alongside the project archive. On success the
    attachment row is flipped to `published_to_zenodo=True` and the
    locally stored file is removed; failures bubble up as ZenodoError
    and the local copy is retained so the user can retry.
    """
    if not deposit.bucket_url:
        return
    attachments = list(
        project.attachments.filter(published_to_zenodo=False).exclude(file="")
    )
    for att in attachments:
        filename = att.filename or att.file.name.rsplit("/", 1)[-1]
        size = att.size_bytes or att.file.size
        att.file.open("rb")
        try:
            client.stream_to_bucket(
                deposit.bucket_url,
                filename,
                att.file,
                size,
                content_type="application/octet-stream",
            )
        finally:
            try:
                att.file.close()
            except Exception:  # pragma: no cover - defensive
                pass
        # Free the local copy: Zenodo is now the host.
        att.file.delete(save=False)
        att.published_to_zenodo = True
        att.save(update_fields=["file", "published_to_zenodo"])
    # Always (re)upload the OSPREY metadata sidecars so the Zenodo record
    # carries OSPREY context regardless of which archive(s) were attached.
    _upload_osprey_sidecars(client, deposit, project)


def _upload_osprey_sidecars(client, deposit, project) -> None:
    """Upload `osprey-project.json` and `CITATION.cff` as Zenodo siblings."""
    if not deposit.bucket_url:
        return
    metadata_bytes = (
        json.dumps(_osprey_metadata_payload(project), indent=2, default=str) + "\n"
    ).encode("utf-8")
    citation_bytes = _osprey_citation_cff(project).encode("utf-8")
    client.upload_to_bucket(
        deposit.bucket_url,
        ProjectArchive(
            filename="osprey-project.json",
            content=metadata_bytes,
            content_type="application/octet-stream",
        ),
    )
    client.upload_to_bucket(
        deposit.bucket_url,
        ProjectArchive(
            filename="CITATION.cff",
            content=citation_bytes,
            content_type="application/octet-stream",
        ),
    )
    if (project.license or "").strip():
        from .licensing import license_sidecar_text

        client.upload_to_bucket(
            deposit.bucket_url,
            ProjectArchive(
                filename="LICENSE.txt",
                content=license_sidecar_text(project).encode("utf-8"),
                content_type="application/octet-stream",
            ),
        )


def build_project_archive(project: Project) -> ProjectArchive:
    contributors = [
        {
            "display_name": c.display_name,
            "role": c.role,
            "credit_statement": c.credit_statement,
            "orcid_id": c.verified_orcid_id,
        }
        for c in project.credited_contributions.select_related("user").all()
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
    concept_id = str(
        response.get("conceptrecid")
        or metadata.get("conceptrecid")
        or response.get("concept_id")
        or ""
    )
    concept_doi = response.get("conceptdoi") or metadata.get("conceptdoi") or ""
    if not concept_doi and concept_id:
        # Sandbox often omits `conceptdoi` from the deposit response even
        # though it returns a `conceptrecid`. Construct the canonical DOI.
        prefix = "10.5072/zenodo." if settings.ZENODO_USE_SANDBOX else "10.5281/zenodo."
        concept_doi = f"{prefix}{concept_id}"
    return {
        "deposition_id": str(response.get("id") or ""),
        "bucket_url": links.get("bucket") or "",
        "record_id": str(response.get("record_id") or ""),
        "concept_id": concept_id,
        "doi": response.get("doi") or metadata.get("doi") or reserved.get("doi") or "",
        "concept_doi": concept_doi,
    }


def sync_project_to_zenodo(project: Project, user) -> ProjectDeposit:
    """Create/update a Zenodo draft and upload an OSPREY snapshot ZIP."""
    _require_license(project)
    client = ZenodoClient.from_settings()
    deposit, created = ProjectDeposit.objects.get_or_create(
        project=project,
        provider=ProjectDeposit.PROVIDER_ZENODO,
        sandbox=settings.ZENODO_USE_SANDBOX,
        defaults={"created_by": user if getattr(user, "is_authenticated", False) else None},
    )
    if not created and deposit.state == ProjectDeposit.STATE_PUBLISHED:
        return deposit
    try:
        current = None
        if not created and deposit.deposition_id:
            # A retry. Ask Zenodo what the last attempt actually did.
            current = _current_deposition(client, deposit)
            if current is not None and current.get("submitted"):
                _record_published(deposit, current)
                return deposit
        if current is None:
            created_response = client.create_deposition()
            ids = _extract_zenodo_ids(created_response)
            deposit.deposition_id = ids["deposition_id"]
            deposit.bucket_url = ids["bucket_url"]
            deposit.record_id = ids["record_id"]
            deposit.concept_id = ids["concept_id"]
            deposit.doi = ids["doi"]
            deposit.concept_doi = ids["concept_doi"]
            deposit.state = ProjectDeposit.STATE_DRAFT
            # Saved now, so a retry after a later failure finds this draft.
            deposit.save()

        metadata_response = client.update_deposition_metadata(deposit.deposition_id, metadata_for_project(project, deposit))
        if not deposit.bucket_url:
            ids = _extract_zenodo_ids(metadata_response)
            deposit.bucket_url = ids["bucket_url"]
        if not deposit.bucket_url:
            raise ZenodoError("Zenodo did not return a file bucket URL for this deposition.")
        # The user's archive is the canonical Zenodo file; OSPREY metadata
        # rides along as `osprey-project.json` and `CITATION.cff` siblings
        # uploaded by `_upload_project_attachments`.
        _upload_project_attachments(client, deposit, project)

        ids = _extract_zenodo_ids(metadata_response)
        deposit.record_id = ids["record_id"] or deposit.record_id
        deposit.concept_id = ids["concept_id"] or deposit.concept_id
        deposit.doi = ids["doi"] or deposit.doi
        deposit.concept_doi = ids["concept_doi"] or deposit.concept_doi
        deposit.last_response = metadata_response
        deposit.last_error = ""
        deposit.state = ProjectDeposit.STATE_DRAFT
        deposit.save()
        if deposit.concept_doi and project.doi != deposit.concept_doi:
            project.doi = deposit.concept_doi
            project.save(update_fields=["doi"])
        elif deposit.doi and (not project.doi or project.doi == deposit.doi):
            project.doi = deposit.doi
            project.save(update_fields=["doi"])
        return deposit
    except ZenodoError as exc:
        deposit.state = ProjectDeposit.STATE_ERROR
        deposit.last_error = str(exc)
        deposit.save(update_fields=["state", "last_error", "updated_at"])
        raise


def _record_published(deposit: ProjectDeposit, response: dict[str, Any]) -> ProjectDeposit:
    """Book a published Zenodo deposition: ids, version row, project DOI.
    Used on publish, and on a retry that finds the publish went through."""
    pending_changelog = deposit.pending_changelog
    pending_repo_link = deposit.repo_link
    next_index = deposit.next_version_index
    ids = _extract_zenodo_ids(response)
    deposit.record_id = ids["record_id"] or deposit.record_id
    deposit.concept_id = ids["concept_id"] or deposit.concept_id
    deposit.doi = ids["doi"] or deposit.doi
    deposit.concept_doi = ids["concept_doi"] or deposit.concept_doi
    deposit.state = ProjectDeposit.STATE_PUBLISHED
    deposit.published_at = timezone.now()
    deposit.last_response = response
    deposit.last_error = ""
    # Always record a version row so every published snapshot is in history.
    ProjectDepositVersion.objects.create(
        deposit=deposit,
        version_index=next_index,
        deposition_id=deposit.deposition_id,
        record_id=deposit.record_id,
        doi=deposit.doi,
        changelog=pending_changelog,
        repo_link=pending_repo_link,
        published_at=deposit.published_at,
        last_response=response,
    )
    deposit.pending_changelog = ""
    deposit.repo_link = ""
    deposit.save()

    project = deposit.project
    # Prefer the concept (project) DOI on the project record so the
    # canonical DOI does not change when a new version is published.
    canonical_doi = deposit.concept_doi or deposit.doi
    if canonical_doi:
        project.doi = canonical_doi
        project.save(update_fields=["doi"])
    if deposit.external_url:
        ArtifactLink.objects.update_or_create(
            project=project,
            kind="zenodo",
            url=deposit.external_url,
            defaults={"label": zenodo_mode_label()},
        )
    return deposit


def _current_deposition(client: "ZenodoClient", deposit: ProjectDeposit) -> dict[str, Any] | None:
    """Zenodo's view of the deposition OSPREY last worked on, or None if
    Zenodo has no such deposition."""
    try:
        return client.get_deposition(deposit.deposition_id)
    except ZenodoError as exc:
        if "HTTP 404" in str(exc) or "HTTP 410" in str(exc):
            return None
        raise


def publish_project_deposit(deposit: ProjectDeposit) -> ProjectDeposit:
    if deposit.provider != ProjectDeposit.PROVIDER_ZENODO:
        raise ZenodoError("Only Zenodo deposits can be published here.")
    if not deposit.deposition_id:
        raise ZenodoError("Create a Zenodo draft before publishing.")
    _require_license(deposit.project)
    client = ZenodoClient.from_settings()
    try:
        response = client.publish_deposition(deposit.deposition_id)
        return _record_published(deposit, response)
    except ZenodoError as exc:
        deposit.state = ProjectDeposit.STATE_ERROR
        deposit.last_error = str(exc)
        deposit.save(update_fields=["state", "last_error", "updated_at"])
        raise


def start_new_version_for_deposit(
    deposit: ProjectDeposit,
    *,
    changelog: str,
    repo_link: str = "",
    user=None,
) -> ProjectDeposit:
    """Open a new Zenodo version draft from a published deposit.

    Stores the supplied changelog and optional per-version repo link on
    the deposit, then re-runs metadata + snapshot upload against the new
    draft. Caller publishes via ``publish_project_deposit``.
    """
    if deposit.provider != ProjectDeposit.PROVIDER_ZENODO:
        raise ZenodoError("Only Zenodo deposits support new versions here.")
    # A retry finds the deposit mid-way. Either it still points at the
    # last published version (the draft request failed or its answer was
    # lost: asking again returns any draft Zenodo opened), or it points at
    # the new-version draft (carry on with it).
    latest = deposit.latest_version
    retrying = deposit.state in (ProjectDeposit.STATE_DRAFT, ProjectDeposit.STATE_ERROR) and bool(latest and latest.deposition_id)
    on_published = retrying and deposit.deposition_id == latest.deposition_id
    resuming = retrying and bool(deposit.deposition_id) and not on_published
    if deposit.state != ProjectDeposit.STATE_PUBLISHED and not retrying:
        raise ZenodoError("Publish the current Zenodo draft before starting a new version.")
    if not deposit.deposition_id:
        raise ZenodoError("This deposit has no Zenodo deposition to fork.")
    changelog = (changelog or "").strip()
    if not changelog:
        raise ZenodoError("A changelog is required when starting a new version.")
    # Persist the user's typed changelog and repo link BEFORE the first
    # network call. If Zenodo is down or the request dies mid-flight, the
    # text survives on the deposit row and prefills the form next time.
    deposit.pending_changelog = changelog
    deposit.repo_link = (repo_link or "").strip()
    deposit.save(update_fields=["pending_changelog", "repo_link", "updated_at"])
    project = deposit.project
    _require_license(project)
    client = ZenodoClient.from_settings()
    try:
        if resuming:
            current = _current_deposition(client, deposit)
            if current is not None and current.get("submitted"):
                # The last attempt's publish went through.
                return _record_published(deposit, current)
            if current is None:
                # The draft is gone on Zenodo: open a fresh one.
                deposit.deposition_id = latest.deposition_id
                resuming = False
        if not resuming:
            draft = client.create_new_version(deposit.deposition_id)
            ids = _extract_zenodo_ids(draft)
            deposit.deposition_id = ids["deposition_id"] or deposit.deposition_id
            deposit.bucket_url = ids["bucket_url"]
            deposit.record_id = ids["record_id"] or deposit.record_id
            deposit.concept_id = ids["concept_id"] or deposit.concept_id
            # Hold on to the previous version DOI until publish replaces it.
            deposit.doi = ids["doi"] or deposit.doi
            deposit.concept_doi = ids["concept_doi"] or deposit.concept_doi
        deposit.state = ProjectDeposit.STATE_DRAFT
        deposit.save()

        metadata_response = client.update_deposition_metadata(
            deposit.deposition_id, metadata_for_project(project, deposit)
        )
        if not deposit.bucket_url:
            ids = _extract_zenodo_ids(metadata_response)
            deposit.bucket_url = ids["bucket_url"]
        if not deposit.bucket_url:
            raise ZenodoError("Zenodo did not return a file bucket URL for the new-version draft.")
        # Drop any files Zenodo carried over from the previous published
        # version: a new version requires a fresh archive upload.
        _clear_existing_zenodo_files(client, deposit.deposition_id)
        _upload_project_attachments(client, deposit, project)
        deposit.last_response = metadata_response
        deposit.last_error = ""
        deposit.save(update_fields=["last_response", "last_error", "updated_at"])
        return deposit
    except ZenodoError as exc:
        deposit.state = ProjectDeposit.STATE_ERROR
        deposit.last_error = str(exc)
        deposit.save(update_fields=["state", "last_error", "updated_at"])
        raise

def publish_project_now(project: Project, user) -> ProjectDeposit:
    """One-shot publish: ensure draft, sync metadata, upload, publish.

    Used by the project form's Publish action so the Zenodo machinery is
    invisible to the user. Existing drafts are reused and re-synced before
    publishing, which lets retries recover from a partial publish.
    """
    deposit = sync_project_to_zenodo(project, user)
    if deposit.state == ProjectDeposit.STATE_PUBLISHED:
        return deposit
    return publish_project_deposit(deposit)


def update_published_metadata(project: Project) -> ProjectDeposit | None:
    """Push edited metadata to a project's already-published Zenodo record.

    Zenodo lets us re-open a published deposition for metadata edits via
    the ``actions/edit`` endpoint, change the metadata, then republish.
    File contents are not changed by this flow (uploading new files
    requires a new version). If the project does not have a published
    Zenodo deposit, this is a no-op and returns None.
    """
    deposit = project.deposits.filter(
        provider=ProjectDeposit.PROVIDER_ZENODO,
        state=ProjectDeposit.STATE_PUBLISHED,
    ).first()
    if deposit is None or not deposit.deposition_id:
        return None
    _require_license(project)
    client = ZenodoClient.from_settings()
    current: dict[str, Any] = {}
    try:
        current = client.edit_published_deposition(deposit.deposition_id)
    except ZenodoError as exc:
        message = str(exc).lower()
        # If the deposition is already in edit mode we can proceed; any
        # other failure should bubble up so the caller can flag it.
        if "already" not in message and "edit" not in message:
            raise
    metadata = metadata_for_project(project, deposit)
    # An edit keeps the record's publication date; only new versions are
    # dated the day they publish.
    published = ((current or {}).get("metadata") or {}).get("publication_date") or (
        (deposit.last_response or {}).get("metadata") or {}
    ).get("publication_date")
    if published:
        metadata["publication_date"] = published
    try:
        client.update_deposition_metadata(deposit.deposition_id, metadata)
        response = client.publish_deposition(deposit.deposition_id)
    except ZenodoError:
        try:
            client.discard_deposition_changes(deposit.deposition_id)
        except ZenodoError:
            pass
        raise
    deposit.last_response = response
    deposit.save(update_fields=["last_response", "updated_at"])
    return deposit


def publish_new_version_now(
    deposit: ProjectDeposit,
    *,
    changelog: str,
    repo_link: str = "",
    user=None,
) -> ProjectDeposit:
    """One-shot new-version flow: open new draft, sync metadata, publish."""
    start_new_version_for_deposit(
        deposit,
        changelog=changelog,
        repo_link=repo_link,
        user=user,
    )
    if deposit.state == ProjectDeposit.STATE_PUBLISHED:
        return deposit  # a retry found the last publish went through
    return publish_project_deposit(deposit)
