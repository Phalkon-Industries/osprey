"""Journal of Open Hardware as an index source, through the OJS OAI-PMH
feed with JATS full text. The whole journal is 60 articles, so the
harvest is one page; it is cached briefly so a paste of several DOIs
doesn't refetch it per line.

JOH has no structured hardware license or files table, so every entry
comes back held: the license gate reports the article's CC BY, and the
files link is the first repository URL found in the body, marked as a
body scan for staff to confirm.
"""
from __future__ import annotations

import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import date

from django.conf import settings

from . import http
from .records import Author, Gate, SourceError, SourceRecord, normalize_license

DOI_PREFIXES = ("10.5206/joh.", "10.5334/joh.")
_cache: dict = {"at": 0.0, "records": None}
CACHE_SECONDS = 600
REPO_RE = re.compile(r"https?://(?:www\.)?(?:github\.com|gitlab\.com|codeberg\.org|zenodo\.org|doi\.org/10\.5281|osf\.io|doi\.org/10\.17605)[^\s\"'<>)]*", re.I)


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _text(el) -> str:
    # Join with spaces so adjacent elements ("<title>Abstract</title><p>This")
    # don't run together, then collapse whitespace.
    return re.sub(r"\s+", " ", html.unescape(" ".join(el.itertext()))).strip() if el is not None else ""


def _find(el, name: str):
    for child in el.iter():
        if _local(child.tag) == name:
            return child
    return None


def _findall(el, name: str):
    return [child for child in el.iter() if _local(child.tag) == name]


def harvest(since: date | None = None) -> list[dict]:
    """Every JATS record in the feed, parsed to a small dict."""
    now = time.monotonic()
    if since is None and _cache["records"] is not None and now - _cache["at"] < CACHE_SECONDS:
        return _cache["records"]
    url = getattr(settings, "JOH_OAI_URL", "https://ojs.lib.uwo.ca/index.php/openhardware/oai")
    params = {"verb": "ListRecords", "metadataPrefix": "jats"}
    if since is not None:
        params["from"] = since.isoformat()
    records: list[dict] = []
    token = None
    while True:
        if token:
            params = {"verb": "ListRecords", "resumptionToken": token}
        raw = http.get(url, params=params, accept="application/xml", what="the JOH OAI-PMH feed")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise SourceError("Unexpected response from the JOH OAI-PMH feed.") from exc
        for rec in _findall(root, "record"):
            parsed = _parse(rec)
            if parsed:
                records.append(parsed)
        tok = _find(root, "resumptionToken")
        token = (tok.text or "").strip() if tok is not None else ""
        if not token:
            break
    if since is None:
        _cache.update(at=now, records=records)
    return records


def _parse(rec) -> dict | None:
    article = _find(rec, "article")
    if article is None:
        return None
    doi = ""
    for aid in _findall(article, "article-id"):
        if aid.get("pub-id-type") == "doi":
            doi = (aid.text or "").strip()
    if not doi:
        return None
    meta = _find(article, "article-meta")
    title = _text(_find(meta, "article-title")) if meta is not None else ""
    authors: list[Author] = []
    for group in _findall(article, "contrib-group"):
        if group.get("content-type") not in (None, "author"):
            continue
        for contrib in _findall(group, "contrib"):
            if contrib.get("contrib-type") not in (None, "author"):
                continue
            name = _find(contrib, "name")
            if name is None:
                continue
            given = _text(_find(name, "given-names"))
            surname = _text(_find(name, "surname"))
            full = f"{given} {surname}".strip()
            orcid = ""
            for cid in _findall(contrib, "contrib-id"):
                if cid.get("contrib-id-type") == "orcid":
                    orcid = (cid.text or "").rsplit("/", 1)[-1].strip()
            aff = _text(_find(contrib, "aff"))
            if full and full not in [a.name for a in authors]:
                authors.append(Author(full[:200], orcid, aff[:200]))
        if authors:
            break
    abstract = re.sub(r"^abstract\s+", "", _text(_find(article, "abstract")), flags=re.I)
    lic = _find(article, "license")
    lic_url = (lic.get("{http://www.w3.org/1999/xlink}href") or lic.get("href") or "") if lic is not None else ""
    lic_text = _text(lic) if lic is not None else ""
    published = None
    for pd in _findall(article, "pub-date"):
        if pd.get("date-type") in ("pub", None):
            y, m, d = (_text(_find(pd, n)) for n in ("year", "month", "day"))
            try:
                published = date(int(y), int(m or 1), int(d or 1))
            except ValueError:
                published = None
            break
    body = _find(article, "body")
    body_text = "".join(body.itertext()) if body is not None else ""
    links = []
    for m in REPO_RE.finditer(body_text):
        url = m.group(0).split("?", 1)[0].rstrip(".,;:")  # strip share tokens
        if url not in links:
            links.append(url)
    keywords = [_text(k) for k in _findall(article, "kwd")][:6]
    return {
        "doi": doi, "title": title, "authors": authors, "abstract": abstract,
        "license_url": lic_url, "license_text": lic_text, "published": published,
        "links": links, "keywords": keywords,
    }


def _record_for(doi: str) -> dict:
    for rec in harvest():
        if rec["doi"].lower() == doi.lower():
            return rec
    raise SourceError("That DOI isn't in the Journal of Open Hardware feed.")


def fetch(doi: str) -> SourceRecord:
    r = _record_for(doi.strip())
    article_license = normalize_license(r["license_url"] or r["license_text"])
    files = r["links"][0] if r["links"] else ""
    rec = SourceRecord(
        source="joh",
        external_id=r["doi"],
        title=(r["title"] or "Untitled")[:300],
        canonical_url=f"https://doi.org/{r['doi']}",
        doi=r["doi"],
        summary=r["abstract"][:280],
        readme=r["abstract"],
        authors=r["authors"],
        published_on=r["published"],
        keywords=r["keywords"],
        artifact_type="Hardware",
        license_raw=r["license_text"],
        license="",
        files_url=files,
        files_url_source="body_scan" if files else "",
        text_license=article_license or r["license_text"],
        raw={"candidate_links": r["links"], "article_license": article_license},
    )
    # JOH is always held: the article license is not the hardware license,
    # and a body scan can't tell the project's repository from a cited one.
    rec.gate_license = Gate(False, f"article: {article_license or r['license_text'] or 'unknown'}; no hardware license in structured data", "JOH OAI-PMH JATS")
    if files:
        rec.gate_files = Gate(True, files, "link found in article body; confirm it is the project's own", confirm=True)
    else:
        rec.gate_files = Gate(False, "no repository link in the body", "article body")
    return rec


def list_new(since: date, limit: int = 5000) -> list[str]:
    return [r["doi"] for r in harvest(since)][:limit]
