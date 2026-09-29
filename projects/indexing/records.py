from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from django.conf import settings

from projects.forms import COMMON_LICENSES

# Licenses OSPREY lists on native and registered projects (the dropdown).
ACCEPTED_LICENSES = {key for key, _label in COMMON_LICENSES}
# Open licenses an indexed entry may carry as a badge without being
# selectable for OSPREY's own projects. TAPR OHL meets the OSHWA open
# source hardware definition; decided 2026-09-29 for indexed entries only.
INDEXED_ONLY_LICENSES = {"TAPR-OHL-1.0"}
INDEX_ACCEPTED_LICENSES = ACCEPTED_LICENSES | INDEXED_ONLY_LICENSES

# Free-text and SPDX spellings seen in the wild, mapped onto OSPREY's keys.
_LICENSE_PATTERNS = [
    (r"\btapr\b", "TAPR-OHL-1.0"),
    (r"cern[\s-]*ohl[\s-]*s", "CERN-OHL-S-2.0"),
    (r"cern[\s-]*ohl[\s-]*w", "CERN-OHL-W-2.0"),
    (r"cern[\s-]*ohl[\s-]*p", "CERN-OHL-P-2.0"),
    (r"\bagpl", "AGPL-3.0"),
    (r"\blgpl", "LGPL-3.0"),
    (r"\bgpl[^a-z0-9]{0,6}v?3|general public license[^0-9]{0,12}v?3", "GPL-3.0"),
    (r"\bapache", "Apache-2.0"),
    (r"\bmit\b", "MIT"),
    (r"bsd[\s-]*3", "BSD-3-Clause"),
    (r"bsd[\s-]*2", "BSD-2-Clause"),
    (r"mpl[\s-]*2|mozilla public", "MPL-2.0"),
    (r"cc0", "CC0-1.0"),
    (r"cc[\s-]*by[\s-]*sa", "CC-BY-SA-4.0"),
    (r"cc[\s-]*by\b|creative commons attribution(?! ?-? ?(non|no))", "CC-BY-4.0"),
]


def normalize_license(text: str) -> str:
    """Best-effort map of a license string to an OSPREY key, or "" when the
    text names nothing we recognize."""
    raw = (text or "").strip()
    if not raw:
        return ""
    if raw in INDEX_ACCEPTED_LICENSES:
        return raw
    low = raw.lower()
    # Creative Commons URLs: creativecommons.org/licenses/by-sa/4.0, /by/4.0, /publicdomain/zero/1.0
    m = re.search(r"creativecommons\.org/(?:licenses/([a-z-]+)|publicdomain/(zero))", low)
    if m:
        code = m.group(1) or m.group(2)
        if "nc" in code.split("-") or "nd" in code.split("-"):
            return ""
        return {"by": "CC-BY-4.0", "by-sa": "CC-BY-SA-4.0", "zero": "CC0-1.0"}.get(code, "")
    # Non-commercial and no-derivatives clauses are not open licenses.
    if re.search(r"\b(nc|nd)\b|non[\s-]*commercial|no[\s-]*deriv", low):
        return ""
    for pattern, key in _LICENSE_PATTERNS:
        if re.search(pattern, low):
            return key
    return ""


@dataclass
class Gate:
    ok: bool = False
    found: str = ""
    where: str = ""
    # True when the value was found but a person must confirm it, e.g. a
    # repository link scanned out of an article body.
    confirm: bool = False

    def as_dict(self) -> dict:
        return {"ok": self.ok, "found": self.found, "where": self.where, "confirm": self.confirm}


@dataclass
class Author:
    name: str
    orcid: str = ""
    affiliation: str = ""


@dataclass
class SourceRecord:
    source: str
    external_id: str
    title: str
    canonical_url: str
    summary: str = ""
    readme: str = ""
    doi: str = ""
    license_raw: str = ""
    license: str = ""
    files_url: str = ""
    files_url_source: str = ""
    authors: list[Author] = field(default_factory=list)
    published_on: date | None = None
    keywords: list[str] = field(default_factory=list)
    oshwa_uid: str = ""
    text_license: str = ""
    artifact_type: str = ""
    field_name: str = ""
    institution: str = ""
    raw: dict = field(default_factory=dict)
    gate_license: Gate = field(default_factory=Gate)
    gate_files: Gate = field(default_factory=Gate)
    # Zenodo only: what a registered project needs for conversion later.
    concept_id: str = ""

    @property
    def passes(self) -> bool:
        """Both gates pass and neither still needs a person to confirm."""
        return (
            self.gate_license.ok and self.gate_files.ok
            and not self.gate_license.confirm and not self.gate_files.confirm
        )


class SourceError(Exception):
    """The source could not be read or the identifier is not usable."""


DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.I)
GITHUB_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)", re.I)
ZENODO_RECORD_RE = re.compile(r"(?:https?://)?(?:sandbox\.)?zenodo\.org/(?:records?/|doi/10\.\d+/zenodo\.)(\d+)", re.I)
# HardwareX DOIs: 10.1016/j.ohx.YYYY.eNNNNN since 2018; the 2016-2017
# volumes used Elsevier's PII form, 10.1016/S2468-0672(17)30019-6.
HARDWAREX_PREFIXES = ("10.1016/j.ohx.", "10.1016/s2468-0672(")
JOH_PREFIXES = ("10.5206/joh.", "10.5334/joh.")


def detect(line: str) -> tuple[str, str]:
    """Classify one pasted line into (source, external_id).

    Sources: zenodo (DOI or record id), github (owner/repo), hardwarex,
    joh, doi (some other DOI), url (anything else), or "" when the line
    is empty.
    """
    text = (line or "").strip().rstrip(".,;")
    if not text:
        return "", ""
    m = GITHUB_RE.search(text)
    if m:
        repo = m.group(2)
        if repo.endswith(".git"):
            repo = repo[:-4]
        return "github", f"{m.group(1)}/{repo}"
    m = DOI_RE.search(text)
    if m:
        doi = m.group(0).rstrip("/")
        low = doi.lower()
        if "zenodo." in low:
            return "zenodo", doi
        if low.startswith(HARDWAREX_PREFIXES):
            return "hardwarex", doi
        if low.startswith(JOH_PREFIXES):
            return "joh", doi
        return "doi", doi
    m = ZENODO_RECORD_RE.search(text)
    if m:
        host = "10.5072" if "sandbox." in text.lower() else "10.5281"
        return "zenodo", f"{host}/zenodo.{m.group(1)}"
    if re.match(r"https?://", text, re.I):
        return "url", text
    return "url", text


_MD_INLINE = [
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),          # images
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),      # links -> text
    (re.compile(r"<[^>]+>"), ""),                        # html tags
    (re.compile(r"[`*_~]+"), ""),                        # emphasis marks
]


def summary_from_readme(readme: str, limit: int = 280) -> str:
    """First paragraph of prose in a README: skips headings, badge lines,
    HTML-only lines and blank lines, strips inline Markdown, cuts to `limit`
    at a word boundary."""
    paragraph: list[str] = []
    for raw in (readme or "").splitlines():
        line = raw.strip()
        if not line:
            if paragraph:
                break
            continue
        if line.startswith(("#", "|", ">", "```", "---", "===")):
            if paragraph:
                break
            continue
        cleaned = line
        for pattern, repl in _MD_INLINE:
            cleaned = pattern.sub(repl, cleaned)
        cleaned = cleaned.strip()
        if not cleaned or not re.search(r"[A-Za-z]{3,}", cleaned):
            if paragraph:
                break
            continue
        paragraph.append(cleaned)
    text = re.sub(r"\s+", " ", " ".join(paragraph)).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def user_agent() -> str:
    base = getattr(settings, "OSPREY_PUBLIC_BASE_URL", "https://osprey.phalkon.io")
    return f"OSPREY/0.2 (+{base}; indexing)"
