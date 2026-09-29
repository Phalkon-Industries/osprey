"""Zenodo records as an index source. Reuses the registered-project reader."""
from __future__ import annotations

from datetime import date

from projects import zenodo_register as zr

from .records import Author, Gate, SourceError, SourceRecord, normalize_license


def fetch(doi: str) -> SourceRecord:
    try:
        record = zr.resolve_record(doi)
    except zr.RegistrationError as exc:
        raise SourceError(str(exc)) from exc
    metadata = record.get("metadata") or {}
    access = metadata.get("access_right") or ((record.get("access") or {}).get("record")) or "open"
    if access not in ("open", "public"):
        raise SourceError("Only open-access Zenodo records can be indexed.")
    raw_license = zr.license_of(record)
    license_key = normalize_license(raw_license)
    files = record.get("files") or []
    record_url = (record.get("links") or {}).get("self_html") or f"https://zenodo.org/records/{record.get('id')}"
    description = metadata.get("description") or ""
    text = zr._strip_html(description)
    published = None
    try:
        published = date.fromisoformat((metadata.get("publication_date") or "")[:10])
    except ValueError:
        pass
    rec = SourceRecord(
        source="zenodo",
        external_id=str(record.get("conceptrecid") or record.get("id") or ""),
        concept_id=str(record.get("conceptrecid") or ""),
        title=(metadata.get("title") or "Untitled Zenodo record")[:300],
        canonical_url=f"https://doi.org/{record.get('conceptdoi') or record.get('doi')}" if (record.get("conceptdoi") or record.get("doi")) else record_url,
        summary=text[:280],
        readme=text,
        doi=record.get("conceptdoi") or record.get("doi") or "",
        license_raw=raw_license,
        license=license_key,
        files_url=record_url if files else "",
        files_url_source="record" if files else "",
        authors=[Author(zr.display_name(c["name"])[:200], c["orcid"], (c["affiliation"] or "")[:200]) for c in zr.creators_of(record)],
        published_on=published,
        keywords=list(metadata.get("keywords") or [])[:6],
        text_license=license_key or raw_license,
        artifact_type={"software": "Software", "dataset": "Dataset"}.get((metadata.get("resource_type") or {}).get("type") or metadata.get("upload_type") or "", "Hardware"),
        raw=record,
    )
    rec.gate_license = Gate(bool(license_key), raw_license or "no license on the record", "Zenodo record metadata")
    rec.gate_files = Gate(bool(files), f"{len(files)} file(s) on the record" if files else "no files on the record", "Zenodo record")
    return rec
