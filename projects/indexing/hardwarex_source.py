"""HardwareX articles as an index source.

Crossref gives the listing and the author list; Europe PMC's full-text
JATS gives the abstract and the journal's Specifications table, which is
where the hardware license, the source-file repository and the OSHWA
certification UID live. See the source survey in
planning/features/indexed-entries.md.
"""
from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import date

from django.conf import settings

from . import http
from .records import INDEX_ACCEPTED_LICENSES, Author, Gate, SourceError, SourceRecord, normalize_license

ISSN = "2468-0672"
DOI_PREFIX = "10.1016/j.ohx."


def _crossref(path: str, params: dict | None = None) -> dict:
    base = getattr(settings, "CROSSREF_API_BASE_URL", "https://api.crossref.org").rstrip("/")
    raw = http.get(f"{base}{path}", params=params, what="Crossref")
    try:
        return json.loads(raw.decode("utf-8"))["message"]
    except (ValueError, KeyError) as exc:
        raise SourceError("Unexpected response from Crossref.") from exc


def _europepmc(path: str, params: dict | None = None, accept: str = "application/json") -> bytes:
    base = getattr(settings, "EUROPEPMC_API_BASE_URL", "https://www.ebi.ac.uk/europepmc/webservices/rest").rstrip("/")
    return http.get(f"{base}{path}", params=params, accept=accept, what="Europe PMC")


def _text(el) -> str:
    # Join with spaces so adjacent elements ("<title>Abstract</title><p>This")
    # don't run together, then collapse whitespace.
    return re.sub(r"\s+", " ", html.unescape(" ".join(el.itertext()))).strip() if el is not None else ""


def _tables(root) -> list[list[list[str]]]:
    """Every table as rows of cell texts, in document order."""
    tables = []
    for table in root.iter():
        if table.tag.split("}")[-1] != "table":
            continue
        rows = []
        for tr in table.iter():
            if tr.tag.split("}")[-1] != "tr":
                continue
            cells = [_text(td) for td in tr if td.tag.split("}")[-1] in ("td", "th")]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


HEADER_WORDS = ("location of the file", "file type", "design file name", "open source license")


def spec_table(root) -> dict:
    """Pull what HardwareX requires of every article.

    The Specifications table is two columns of key/value rows ("Open
    source license", "Source file repository", "OSHWA certification UID",
    "Cost of hardware"). The design-files table has a column headed
    "Open source license" with one license per file; it is the fallback
    when the spec table has no license row, and its header must never be
    mistaken for a value.
    """
    found: dict = {}
    per_file: list[str] = []
    for rows in _tables(root):
        header = [c.lower().rstrip(":") for c in rows[0]]
        if "open source license" in header and any(w in " ".join(header) for w in ("location of the file", "file type", "design file")):
            col = header.index("open source license")
            for row in rows[1:]:
                if len(row) > col and row[col].strip():
                    per_file.append(row[col].strip())
            continue
        for row in rows:
            if len(row) < 2:
                continue
            key = row[0].lower().rstrip(":")
            value = row[1].strip()
            if value.lower() in HEADER_WORDS:
                continue
            if key.startswith("open source license") and "license" not in found:
                found["license"] = value
            elif key.startswith("source file repository") and "repository" not in found:
                found["repository"] = value
            elif key.startswith("oshwa certification") and "oshwa" not in found:
                found["oshwa"] = value
            elif key.startswith("cost of hardware") and "cost" not in found:
                found["cost"] = value
    if per_file:
        found["per_file_licenses"] = per_file
        if "license" not in found:
            keys = {normalize_license(v) for v in per_file}
            keys.discard("")
            if len(keys) == 1:
                found["license"] = per_file[0]
                found["license_from"] = "design files table"
            elif len(keys) > 1:
                found["license"] = "; ".join(sorted(set(per_file)))
                found["license_from"] = "design files table (mixed)"
    return found


def hardware_license(text: str) -> str:
    """The hardware license out of a spec-table cell like
    'Hardware: CERN-OHL-S v2.0 Software: MIT Documentation: CC BY 4.0'."""
    if not text:
        return ""
    m = re.search(r"hardware\s*[:\-]?\s*(.+?)(?=\s*(software|documentation|firmware|docs)\s*[:\-]|$)", text, re.I)
    candidate = m.group(1) if m else text
    return normalize_license(candidate)


def first_url(text: str) -> str:
    m = re.search(r"https?://[^\s,;]+", text or "")
    if m:
        return m.group(0).rstrip(".)")
    m = re.search(r"\b(10\.\d{4,9}/[^\s,;]+)", text or "")
    return f"https://doi.org/{m.group(1).rstrip('.)')}" if m else ""


def _abstract(root) -> str:
    for ab in root.iter():
        if ab.tag.split("}")[-1] == "abstract" and ab.get("abstract-type") in (None, "author"):
            text = _text(ab)
            return re.sub(r"^abstract\s+", "", text, flags=re.I)
    return ""


def _keywords(root) -> list[str]:
    """<kwd> elements when present; HardwareX often puts them as a
    'Keywords:' line inside the graphical abstract instead."""
    kwds = [_text(k) for k in root.iter() if k.tag.split("}")[-1] == "kwd"]
    if not kwds:
        for ab in root.iter():
            if ab.tag.split("}")[-1] == "abstract":
                m = re.search(r"keywords?\s*:\s*(.+)", _text(ab), re.I)
                if m:
                    kwds = [k.strip(" .") for k in m.group(1).split(",") if k.strip(" .")]
                    break
    return [k[:80] for k in kwds if k][:6]


def fetch(doi: str) -> SourceRecord:
    doi = doi.strip()
    work = _crossref(f"/works/{doi}")
    title = re.sub(r"\s+", " ", (work.get("title") or ["Untitled"])[0]).strip()
    authors = []
    for a in work.get("author") or []:
        name = " ".join(p for p in (a.get("given"), a.get("family")) if p).strip() or a.get("name", "")
        if not name:
            continue
        orcid = (a.get("ORCID") or "").rsplit("/", 1)[-1]
        aff = ((a.get("affiliation") or [{}])[0].get("name") or "")[:200]
        authors.append(Author(name[:200], orcid, aff))
    parts = ((work.get("published") or work.get("issued") or {}).get("date-parts") or [[None]])[0]
    published = None
    if parts and parts[0]:
        published = date(parts[0], parts[1] if len(parts) > 1 and parts[1] else 1, parts[2] if len(parts) > 2 and parts[2] else 1)
    article_license = ""
    for lic in work.get("license") or []:
        if lic.get("content-version") in ("vor", "unspecified", None):
            article_license = lic.get("URL", "")
    rec = SourceRecord(
        source="hardwarex",
        external_id=doi,
        title=title[:300],
        canonical_url=f"https://doi.org/{doi}",
        doi=doi,
        authors=authors,
        published_on=published,
        artifact_type="Hardware",
        text_license=normalize_license(article_license) or ("CC BY-NC-ND 4.0" if "nc-nd" in article_license else article_license),
        raw={"crossref": {k: work.get(k) for k in ("DOI", "container-title", "published", "license", "is-referenced-by-count", "URL")}},
    )
    # Europe PMC: the spec table and the abstract.
    try:
        hits = json.loads(_europepmc("/search", {"query": f"DOI:{doi}", "format": "json", "pageSize": "1"}).decode("utf-8"))
        result = ((hits.get("resultList") or {}).get("result") or [{}])[0]
        pmcid = result.get("pmcid") or ""
    except (SourceError, ValueError) as exc:
        # A transient failure is not the same as absence; say so, so a
        # Re-check later can turn the entry live.
        why = f"Europe PMC unavailable ({exc}); re-check later"
        rec.gate_license = Gate(False, why, "Europe PMC")
        rec.gate_files = Gate(False, why, "Europe PMC")
        return rec
    if not pmcid:
        rec.gate_license = Gate(False, "article not in Europe PMC; spec table unavailable", "Europe PMC")
        rec.gate_files = Gate(False, "article not in Europe PMC; spec table unavailable", "Europe PMC")
        return rec
    rec.raw["pmcid"] = pmcid
    try:
        root = ET.fromstring(_europepmc(f"/{pmcid}/fullTextXML", accept="application/xml"))
    except (SourceError, ET.ParseError) as exc:
        why = f"full text not available from Europe PMC ({exc}); re-check later"
        rec.gate_license = Gate(False, why, "Europe PMC")
        rec.gate_files = Gate(False, why, "Europe PMC")
        return rec
    table = spec_table(root)
    rec.raw["spec_table"] = table
    abstract = _abstract(root)
    rec.readme = abstract
    rec.summary = abstract[:280]
    rec.keywords = _keywords(root)
    rec.oshwa_uid = (table.get("oshwa") or "").strip()[:20]
    if rec.oshwa_uid.lower().startswith(("n/a", "none", "not")):
        rec.oshwa_uid = ""
    lic_text = table.get("license", "")
    rec.license_raw = lic_text
    where = "Specifications table (Europe PMC full text)"
    if table.get("license_from"):
        where = table["license_from"].capitalize() + " (Europe PMC full text)"
    if table.get("license_from", "").endswith("(mixed)"):
        rec.license = ""
        rec.gate_license = Gate(False, f"design files carry different licenses: {lic_text}; pick the hardware one", where)
    else:
        rec.license = hardware_license(lic_text)
        accepted = rec.license in INDEX_ACCEPTED_LICENSES
        rec.gate_license = Gate(accepted, lic_text or "no Open source license row in the spec table", where)
        if rec.license and not accepted:
            rec.gate_license.found = f"{lic_text}: open, but not on OSPREY's list"
    repo = first_url(table.get("repository", ""))
    rec.files_url = repo
    rec.files_url_source = "spec_table" if repo else ""
    rec.gate_files = Gate(bool(repo), table.get("repository") or "no Source file repository row in the spec table", "Specifications table, Source file repository")
    return rec


def list_new(since: date, limit: int = 5000) -> list[str]:
    """DOIs published in the journal on or after `since`, via Crossref."""
    dois: list[str] = []
    cursor = "*"
    while len(dois) < limit:
        # Crossref refuses `sort` together with `cursor`; order doesn't
        # matter here because the caller drops DOIs it already knows.
        msg = _crossref("/works", {
            "filter": f"issn:{ISSN},from-pub-date:{since.isoformat()}",
            "rows": "100", "cursor": cursor, "select": "DOI",
        })
        items = msg.get("items") or []
        dois.extend(i["DOI"] for i in items if i.get("DOI"))
        cursor = msg.get("next-cursor") or ""
        if not items or not cursor or len(items) < 100:
            break
    return dois[:limit]
