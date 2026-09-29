"""Registered projects: OSPREY projects backed by the authors' own Zenodo record.

Everything here reads Zenodo's public records API without a token. The
platform token is used for one optional thing the record owner can do on
Zenodo: submit the record to the OSPREY community, which OSPREY accepts.
OSPREY never writes to a registered record. (Letting OSPREY manage a registered
record was designed and set aside; see the feature doc.)

See planning/features/registered-projects.md.
"""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import date, datetime, time
from urllib import error, parse, request

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import claiming
from .models import Contribution, Project, ProjectDeposit, ProjectDepositVersion
from .zenodo import ZenodoClient, ZenodoError, zenodo_configured

logger = logging.getLogger(__name__)


class RegistrationError(Exception):
    """A reason the link cannot be made, phrased for the user."""


# --- reading Zenodo ------------------------------------------------------------


class ZenodoRecordReader:
    """Public, unauthenticated reads of Zenodo's records API."""

    def __init__(self, base_url: str | None = None, timeout: int | None = None):
        self.base_url = (base_url or settings.ZENODO_API_BASE_URL).rstrip("/")
        self.timeout = timeout or getattr(settings, "ZENODO_TIMEOUT_SECONDS", 20)

    def _get(self, path_or_url: str) -> dict:
        from projects.indexing.http import _guard_test_run

        _guard_test_run(path_or_url if path_or_url.startswith("http") else self.base_url)
        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        req = request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "OSPREY registered records (https://osprey.phalkon.io/)"},
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            if exc.code == 404:
                raise RegistrationError("Zenodo has no record with that DOI.") from exc
            raise RegistrationError(f"Zenodo returned an error (HTTP {exc.code}). Try again in a bit.") from exc
        except (error.URLError, OSError) as exc:
            raise RegistrationError("Couldn't reach Zenodo. Try again in a bit.") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RegistrationError("That DOI isn't a Zenodo record.") from exc

    def record(self, recid: str | int) -> dict:
        return self._get(f"/api/records/{recid}")

    def versions(self, recid: str | int) -> list[dict]:
        """Every version of the concept, oldest first.

        Zenodo lists versions newest first and paginates (ten per page by
        default, 25 at most; a larger size is a 400), so this follows
        `links.next` and sorts by the version index Zenodo records under
        `metadata.relations`."""
        url = f"/api/records/{recid}/versions?size=25"
        hits: list[dict] = []
        for _ in range(50):  # a hard stop, not a real limit
            data = self._get(url)
            hits.extend((data.get("hits") or {}).get("hits") or [])
            url = (data.get("links") or {}).get("next") or ""
            if not url:
                break
        return sorted(hits, key=version_sort_key)

    def by_doi(self, doi: str) -> dict | None:
        for field in ("doi", "conceptdoi"):
            data = self._get("/api/records?q=" + parse.quote(f'{field}:"{doi}"'))
            hits = (data.get("hits") or {}).get("hits") or []
            if hits:
                return hits[0]
        return None


def version_index(record: dict) -> int | None:
    """Zenodo's own version counter, 0-based, under metadata.relations."""
    for rel in ((record.get("metadata") or {}).get("relations") or {}).get("version") or []:
        if isinstance(rel, dict) and rel.get("index") is not None:
            return int(rel["index"])
    return None


def version_sort_key(record: dict):
    index = version_index(record)
    return (index if index is not None else 10**9, (record.get("metadata") or {}).get("publication_date") or "", int(record.get("id") or 0))


def normalize_orcid(value: str) -> str:
    value = (value or "").strip()
    for prefix in ("https://orcid.org/", "http://orcid.org/", "orcid.org/"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    return value.strip("/").upper()


PRODUCTION_HOST = "https://zenodo.org"
SANDBOX_HOST = "https://sandbox.zenodo.org"
SANDBOX_DOI_PREFIX = "10.5072/"
PRODUCTION_DOI_PREFIX = "10.5281/"


def is_sandbox_doi(doi: str) -> bool:
    return parse_doi(doi).lower().startswith(SANDBOX_DOI_PREFIX)


def host_for_doi(doi: str) -> str:
    """The Zenodo a DOI belongs to. The prefix says which; anything else
    falls back to the configured endpoint (which is what tests use)."""
    clean = parse_doi(doi).lower()
    if clean.startswith(SANDBOX_DOI_PREFIX):
        return SANDBOX_HOST if settings.ZENODO_API_BASE_URL.rstrip("/") in (PRODUCTION_HOST, SANDBOX_HOST) else settings.ZENODO_API_BASE_URL
    if clean.startswith(PRODUCTION_DOI_PREFIX):
        return PRODUCTION_HOST
    return settings.ZENODO_API_BASE_URL


def reader_for_doi(doi: str) -> "ZenodoRecordReader":
    return ZenodoRecordReader(base_url=host_for_doi(doi))


def parse_doi(text: str) -> str:
    doi = (text or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:", "DOI:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    return doi.strip().strip("/")


def resolve_record(doi: str, reader: ZenodoRecordReader | None = None) -> dict:
    """The latest published record for a Zenodo DOI (concept or version)."""
    doi = parse_doi(doi)
    if not doi:
        raise RegistrationError("Paste the DOI of your Zenodo record.")
    if is_sandbox_doi(doi) and not settings.ZENODO_USE_SANDBOX:
        raise RegistrationError(
            "That's a Zenodo sandbox DOI. Sandbox records are test data and "
            "can't be registered here; publish the record on zenodo.org first."
        )
    # Public reads go to whichever Zenodo the DOI belongs to, so a
    # production record can be registered from a sandbox or dev server.
    reader = reader or reader_for_doi(doi)
    match = re.search(r"zenodo\.(\d+)$", doi, re.I)
    if match:
        record = reader.record(match.group(1))
    else:
        record = reader.by_doi(doi)
        if record is None:
            raise RegistrationError("That DOI isn't a Zenodo record.")
    if not record.get("id"):
        raise RegistrationError("That DOI isn't a Zenodo record.")
    # A version DOI resolves to that version; the project follows the
    # concept, so hop to its latest version (Zenodo answers a concept id
    # with the newest record).
    concept = str(record.get("conceptrecid") or "")
    if concept and concept != str(record.get("id")) and version_index(record) is not None:
        rel = ((record.get("metadata") or {}).get("relations") or {}).get("version") or [{}]
        if not rel[0].get("is_last", True):
            record = reader.record(concept)
    return record


def creators_of(record: dict) -> list[dict]:
    """Normalize creators to name, orcid, affiliation across API shapes."""
    rows = []
    for entry in (record.get("metadata") or {}).get("creators") or []:
        orcid = entry.get("orcid") or ""
        person = entry.get("person_or_org") or {}
        if not orcid:
            for ident in person.get("identifiers") or []:
                if ident.get("scheme") == "orcid":
                    orcid = ident.get("identifier") or ""
        name = entry.get("name") or person.get("name") or ""
        affiliation = entry.get("affiliation") or ""
        if not affiliation:
            affs = entry.get("affiliations") or []
            if affs and isinstance(affs[0], dict):
                affiliation = affs[0].get("name") or ""
        if not (name or "").strip():
            continue
        orcid = normalize_orcid(orcid)
        if orcid and any(r["orcid"] == orcid for r in rows):
            continue  # the same person listed twice
        rows.append({"name": name, "orcid": orcid, "affiliation": affiliation or ""})
    return rows


def display_name(name: str) -> str:
    """Zenodo stores 'Family, Given'; OSPREY shows 'Given Family'."""
    if "," in name:
        family, given = name.split(",", 1)
        return f"{given.strip()} {family.strip()}".strip()
    return name.strip()


_LICENSE_IDS = {
    "mit": "MIT", "mit-license": "MIT",
    "apache-2.0": "Apache-2.0",
    "bsd-2-clause": "BSD-2-Clause", "bsd-3-clause": "BSD-3-Clause",
    "agpl-3.0": "AGPL-3.0", "gpl-3.0": "GPL-3.0", "gpl-3.0-only": "GPL-3.0",
    "gpl-3.0-or-later": "GPL-3.0-or-later", "lgpl-3.0": "LGPL-3.0", "mpl-2.0": "MPL-2.0",
    "cc-by-4.0": "CC-BY-4.0", "cc-by-sa-4.0": "CC-BY-SA-4.0", "cc0-1.0": "CC0-1.0",
    "cern-ohl-p-2.0": "CERN-OHL-P-2.0", "cern-ohl-s-2.0": "CERN-OHL-S-2.0", "cern-ohl-w-2.0": "CERN-OHL-W-2.0",
}


def license_of(record: dict) -> str:
    raw = (record.get("metadata") or {}).get("license") or ""
    if isinstance(raw, dict):
        raw = raw.get("id") or ""
    key = str(raw).strip().lower()
    return _LICENSE_IDS.get(key, str(raw).strip()[:80])


def _strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _published_at(record: dict):
    raw = (record.get("metadata") or {}).get("publication_date") or ""
    try:
        day = date.fromisoformat(raw[:10])
    except ValueError:
        return timezone.now()
    return timezone.make_aware(datetime.combine(day, time.min))


def _version_label(record: dict, index: int) -> str:
    return (record.get("metadata") or {}).get("version") or f"v{index}"


# --- link ---------------------------------------------------------------------


def find_existing(record: dict) -> Project | None:
    concept_id = str(record.get("conceptrecid") or "")
    deposit = (
        ProjectDeposit.objects.filter(provider=ProjectDeposit.PROVIDER_ZENODO, concept_id=concept_id)
        .select_related("project")
        .first()
        if concept_id
        else None
    )
    return deposit.project if deposit else None


def register_record(doi: str, user, reader: ZenodoRecordReader | None = None) -> Project:
    """Create a registered project from a Zenodo DOI the user is a creator of."""
    from .forms import _generate_slug
    from .zenodo_jobs import notify_project_published

    orcid = normalize_orcid(claiming.orcid_for(user))
    if not orcid:
        raise RegistrationError("Sign in with ORCID first. We check the record against your ORCID iD.")
    # The DOI decides which Zenodo to read (production or sandbox).
    reader = reader or reader_for_doi(doi)
    record = resolve_record(doi, reader)
    metadata = record.get("metadata") or {}
    access = metadata.get("access_right") or ((record.get("access") or {}).get("record")) or "open"
    if access not in ("open", "public"):
        raise RegistrationError("Only open-access Zenodo records can be registered.")
    creators = creators_of(record)
    if not any(c["orcid"] == orcid for c in creators):
        raise RegistrationError(
            "Your ORCID iD isn't listed on that record as a creator. If it's your "
            "record, add your ORCID iD on Zenodo and publish the change, then try "
            "again. If it isn't yours, you can index the project instead."
        )
    # Every project on OSPREY carries an open license OSPREY accepts; the
    # record is the source of truth for it, so the link stops here rather
    # than asking the linker to pick one.
    from .forms import COMMON_LICENSES

    license_name = license_of(record)
    accepted = {key for key, _label in COMMON_LICENSES}
    if license_name not in accepted:
        shown = license_name or "no license"
        raise RegistrationError(
            f"The record's license ({shown}) isn't one OSPREY accepts. OSPREY "
            "lists open-licensed work only. If you think this license should be "
            "supported, ask in the Suggestion Box and staff will review it."
        )
    existing = find_existing(record) or Project.objects.filter(
        origin=Project.ORIGIN_INDEXED, source=Project.SOURCE_ZENODO,
        external_id=str(record.get("conceptrecid") or ""),
    ).first()
    if existing is not None and not existing.is_indexed:
        raise RegistrationError(f"That record is already on OSPREY as “{existing.title}”.")

    versions = reader.versions(record["id"]) or [record]
    if existing is not None:
        return _convert_indexed_entry(existing, record, versions, creators, orcid, user)
    description = metadata.get("description") or ""
    title = (metadata.get("title") or "Untitled Zenodo record")[:300]
    with transaction.atomic():
        project = Project(
            slug=_generate_slug(title),
            title=title,
            summary=_strip_html(description)[:280],
            readme=description if "<" not in description else _strip_html(description),
            license=license_of(record),
            doi=record.get("conceptdoi") or record.get("doi") or "",
            origin=Project.ORIGIN_REGISTERED,
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=user,
        )
        project.save()
        owner_row = None
        for index, creator in enumerate(creators):
            row = Contribution.objects.create(
                project=project,
                display_name=display_name(creator["name"])[:200] or "Author",
                role="Author",
                orcid_id=creator["orcid"],
                affiliation=(creator["affiliation"] or "")[:200],
                order=index,
            )
            if creator["orcid"] == orcid and owner_row is None:
                owner_row = row
        if owner_row is not None:
            owner_row.user = user
            owner_row.claim_status = Contribution.CLAIM_VERIFIED
            owner_row.save(update_fields=["user", "claim_status"])
        deposit = ProjectDeposit.objects.create(
            project=project,
            sandbox=is_sandbox_doi(record.get("doi") or doi),
            managed=False,
            deposition_id=str(record["id"]),
            record_id=str(record["id"]),
            concept_id=str(record.get("conceptrecid") or ""),
            doi=record.get("doi") or "",
            concept_doi=record.get("conceptdoi") or "",
            state=ProjectDeposit.STATE_PUBLISHED,
            published_at=_published_at(record),
            created_by=user,
            last_response=record,
            zenodo_synced_at=timezone.now(),
        )
        _sync_versions(deposit, versions)
    # Coauthors with OSPREY accounts hear about their credit the normal way.
    for row in project.contributions.exclude(pk=getattr(owner_row, "pk", None)).exclude(orcid_id=""):
        try:
            claiming.request_confirmation(row, user)
        except Exception:  # noqa: BLE001 - never block the link on a notification
            logger.exception("confirmation request failed for %s", row.pk)
    notify_project_published(project)
    return project


def _convert_indexed_entry(project: Project, record: dict, versions: list[dict], creators: list[dict], orcid: str, user) -> Project:
    """An indexed Zenodo entry becomes a registered project in place: same
    row, same slug, lineage edges kept. The entry's author rows are
    rebuilt from the record; rows someone already claimed stay theirs."""
    from .zenodo_jobs import notify_project_published

    metadata = record.get("metadata") or {}
    description = metadata.get("description") or ""
    with transaction.atomic():
        project.origin = Project.ORIGIN_REGISTERED
        project.created_by = user
        project.visibility = Project.VISIBILITY_PUBLIC
        project.index_state = Project.INDEX_LIVE
        project.title = (metadata.get("title") or project.title)[:300]
        project.summary = _strip_html(description)[:280] or project.summary
        project.readme = description if "<" not in description else _strip_html(description)
        project.license = license_of(record)
        project.doi = record.get("conceptdoi") or record.get("doi") or project.doi
        project.save()
        by_orcid = {row.orcid_id: row for row in project.contributions.exclude(orcid_id="")}
        seen = set()
        owner_row = None
        for index, creator in enumerate(creators):
            row = by_orcid.get(creator["orcid"]) if creator["orcid"] else None
            if row is None:
                row = Contribution(project=project, orcid_id=creator["orcid"])
            row.display_name = display_name(creator["name"])[:200] or "Author"
            row.role = "Author"
            row.affiliation = (creator["affiliation"] or "")[:200]
            row.order = index
            row.save()
            seen.add(row.pk)
            if creator["orcid"] == orcid and owner_row is None:
                owner_row = row
        project.contributions.exclude(pk__in=seen).filter(user__isnull=True).delete()
        if owner_row is not None:
            owner_row.user = user
            owner_row.claim_status = Contribution.CLAIM_VERIFIED
            owner_row.save(update_fields=["user", "claim_status"])
        deposit = ProjectDeposit.objects.create(
            project=project,
            sandbox=is_sandbox_doi(record.get("doi") or ""),
            managed=False,
            deposition_id=str(record["id"]),
            record_id=str(record["id"]),
            concept_id=str(record.get("conceptrecid") or ""),
            doi=record.get("doi") or "",
            concept_doi=record.get("conceptdoi") or "",
            state=ProjectDeposit.STATE_PUBLISHED,
            published_at=_published_at(record),
            created_by=user,
            last_response=record,
            zenodo_synced_at=timezone.now(),
        )
        _sync_versions(deposit, versions)
    for row in project.contributions.exclude(pk=getattr(owner_row, "pk", None)).exclude(orcid_id="").filter(user__isnull=True):
        try:
            claiming.request_confirmation(row, user)
        except Exception:  # noqa: BLE001
            logger.exception("confirmation request failed for %s", row.pk)
    notify_project_published(project)
    return project


def _sync_versions(deposit: ProjectDeposit, versions: list[dict]) -> int:
    """Create version rows for Zenodo versions OSPREY hasn't seen. Returns
    how many were added."""
    known = set(deposit.versions.values_list("record_id", flat=True))
    added = 0
    next_index = deposit.next_version_index
    for record in versions:
        record_id = str(record.get("id") or "")
        if not record_id or record_id in known:
            continue
        ProjectDepositVersion.objects.create(
            deposit=deposit,
            version_index=next_index,
            deposition_id=record_id,
            record_id=record_id,
            doi=record.get("doi") or "",
            changelog=_version_label(record, next_index),
            published_at=_published_at(record),
            last_response=record,
        )
        next_index += 1
        added += 1
    return added


# --- refresh -----------------------------------------------------------------------


def refresh_registered(project: Project, reader: ZenodoRecordReader | None = None) -> dict:
    """Re-read the Zenodo record: title, license, DOIs, new versions, new
    creators. Never removes a credit row on its own."""
    if not project.is_registered:
        raise RegistrationError("Only registered projects are refreshed from Zenodo.")
    deposit = project.deposits.filter(provider=ProjectDeposit.PROVIDER_ZENODO).first()
    if deposit is None:
        raise RegistrationError("This project has no Zenodo record to refresh.")
    reader = reader or reader_for_doi(deposit.concept_doi or deposit.doi)
    record = reader.record(deposit.concept_id or deposit.record_id)
    metadata = record.get("metadata") or {}
    changes: dict = {"versions_added": 0, "creators_added": 0, "fields": []}

    title = (metadata.get("title") or project.title)[:300]
    if title != project.title:
        project.title = title
        changes["fields"].append("title")
    license_name = license_of(record)
    if license_name and license_name != project.license:
        project.license = license_name
        changes["fields"].append("license")
    concept_doi = record.get("conceptdoi") or deposit.concept_doi
    if concept_doi != project.doi:
        project.doi = concept_doi
        changes["fields"].append("doi")
    project.save(update_fields=["title", "license", "doi"])

    deposit.deposition_id = deposit.record_id = str(record.get("id") or deposit.record_id)
    deposit.doi = record.get("doi") or deposit.doi
    deposit.concept_doi = concept_doi
    deposit.last_response = record
    deposit.zenodo_synced_at = timezone.now()
    community = getattr(settings, "ZENODO_DEFAULT_COMMUNITY", "")
    if community:
        ids = {(c.get("id") or c.get("identifier") or "") for c in metadata.get("communities") or []}
        deposit.in_community = community in ids
    deposit.save()
    changes["versions_added"] = _sync_versions(deposit, reader.versions(record["id"]) or [record])

    known_orcids = set(project.contributions.exclude(orcid_id="").values_list("orcid_id", flat=True))
    known_names = set(project.contributions.values_list("display_name", flat=True))
    order = project.contributions.count()
    for creator in creators_of(record):
        name = display_name(creator["name"])[:200] or "Author"
        if (creator["orcid"] and creator["orcid"] in known_orcids) or (not creator["orcid"] and name in known_names):
            continue
        Contribution.objects.create(
            project=project, display_name=name, role="Author",
            orcid_id=creator["orcid"], affiliation=(creator["affiliation"] or "")[:200], order=order,
        )
        order += 1
        changes["creators_added"] += 1
    return changes


# --- optional connections ----------------------------------------------------


def accept_community_requests() -> int:
    """Accept pending inclusion requests to the OSPREY community whose
    record belongs to a project on OSPREY. Others are left for staff."""
    community = getattr(settings, "ZENODO_DEFAULT_COMMUNITY", "")
    if not community or not zenodo_configured():
        return 0
    client = ZenodoClient.from_settings()
    try:
        data = client._request("GET", "/api/requests?" + parse.urlencode({"q": f"receiver.community:{community}", "status": "submitted"}))
    except ZenodoError as exc:
        # An outage here is not an error worth a traceback every ten
        # minutes; the next poll will try again.
        logger.info("community request poll skipped: %s", exc)
        return 0
    accepted = 0
    for hit in (data.get("hits") or {}).get("hits") or []:
        record_id = str((hit.get("topic") or {}).get("record") or "")
        doi = hit.get("record_doi") or ""
        deposit = ProjectDeposit.objects.filter(provider=ProjectDeposit.PROVIDER_ZENODO).filter(
            models_q(record_id, doi)
        ).select_related("project").first()
        if deposit is None or deposit.project.origin == Project.ORIGIN_INDEXED:
            continue
        try:
            client._request("POST", f"/api/requests/{hit['id']}/actions/accept", payload={}, expected=(200, 201, 202))
        except ZenodoError:
            logger.exception("accepting community request %s failed", hit.get("id"))
            continue
        ProjectDeposit.objects.filter(pk=deposit.pk).update(in_community=True)
        accepted += 1
    return accepted


def models_q(record_id: str, doi: str):
    from django.db.models import Q

    q = Q(pk__in=[])
    if record_id:
        q |= Q(record_id=record_id) | Q(concept_id=record_id) | Q(versions__record_id=record_id)
    if doi:
        q |= Q(doi=doi) | Q(concept_doi=doi) | Q(versions__doi=doi)
    return q
