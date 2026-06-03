"""Fetch ORCID public-record data for the verified-domain sign-in gate.

OSPREY is not an ORCID member, so we only get access to fields with
"Everyone" visibility via the free public API. A user whose verified
institutional email domain is set to "Only me" or "Trusted parties" will
look the same to us as a user with no verified domain at all.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
import json

logger = logging.getLogger(__name__)


PUBLIC_API_HOST = "pub.orcid.org"
SANDBOX_API_HOST = "pub.sandbox.orcid.org"
REQUEST_TIMEOUT_SECONDS = 6


class OrcidLookupError(Exception):
    """Raised when the ORCID public API can't be reached or returned junk."""


def _api_host(use_sandbox: bool) -> str:
    return SANDBOX_API_HOST if use_sandbox else PUBLIC_API_HOST


def fetch_email_record(orcid_id: str, use_sandbox: bool = False) -> dict[str, Any]:
    """Return the parsed JSON `/email` payload for the given ORCID iD.

    Raises OrcidLookupError on any network or decode failure so the caller
    can fail closed.
    """
    url = f"https://{_api_host(use_sandbox)}/v3.0/{orcid_id}/email"
    req = urlrequest.Request(url, headers={"Accept": "application/json"})
    try:
        with urlrequest.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except HTTPError as exc:
        raise OrcidLookupError(f"ORCID API HTTP {exc.code} for {orcid_id}") from exc
    except (URLError, TimeoutError) as exc:
        raise OrcidLookupError(f"ORCID API unreachable for {orcid_id}: {exc}") from exc
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise OrcidLookupError(f"ORCID API returned non-JSON for {orcid_id}") from exc


def verified_domains(payload: dict[str, Any]) -> list[str]:
    """Extract the list of verified institutional email domains from a payload.

    Looks at the public `email` collection. Each email entry can carry
    `source-client-id`/`source` metadata indicating the verifier. We accept
    any email entry whose `verified` flag is true and whose `source` is not
    the user themselves (i.e. it was asserted by a third party / institution).
    Domains are returned lowercased, deduped, in insertion order.
    """
    emails = (payload or {}).get("email") or []
    out: list[str] = []
    seen: set[str] = set()
    for entry in emails:
        if not isinstance(entry, dict):
            continue
        if not entry.get("verified"):
            continue
        addr = (entry.get("email") or "").strip().lower()
        if "@" not in addr:
            continue
        source = entry.get("source") or {}
        # The "source-orcid" key (when present) means the user asserted it
        # themselves; we want third-party-verified entries. Some payloads
        # only expose source-client-id; in that case we trust the
        # `verified` flag.
        source_orcid = (source.get("source-orcid") or {}).get("path") or ""
        target_orcid = (payload.get("path") or "").lstrip("/")
        if source_orcid and source_orcid == target_orcid:
            # Self-asserted; not an institutional verification.
            continue
        domain = addr.rsplit("@", 1)[-1]
        if domain and domain not in seen:
            seen.add(domain)
            out.append(domain)
    return out


def has_verified_institutional_domain(
    orcid_id: str, use_sandbox: bool = False
) -> tuple[bool, list[str]]:
    """Return (has_one, domains). Raises OrcidLookupError on transport failure."""
    payload = fetch_email_record(orcid_id, use_sandbox=use_sandbox)
    domains = verified_domains(payload)
    return bool(domains), domains
