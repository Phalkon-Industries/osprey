"""ORCID public expanded-search helper.

Used by the contributor formset's inline ORCID search. The search itself
is gated through a server endpoint so we can cache, send a proper
User-Agent, and avoid leaking client IPs to ORCID.
"""

from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_ORCID_SEARCH_URL = "https://pub.orcid.org/v3.0/expanded-search/"
_USER_AGENT = "OSPREY/0.1 (https://osprey.phalkon.io)"


def _published_name(given: str, family: str, credit: str, orcid_id: str) -> str:
    """Best display name for a contributor pulled from an ORCID record.

    Order of preference:
      1. credit-name (what the researcher chose to publish under)
      2. given-names + family-names
      3. literal "ORCID <id>" fallback (submitter is expected to fix)
    """
    credit = (credit or "").strip()
    if credit:
        return credit
    full = " ".join(
        part for part in [(given or "").strip(), (family or "").strip()] if part
    )
    if full:
        return full
    if orcid_id:
        return f"ORCID {orcid_id}"
    return ""


def _first_institution(value) -> str:
    """Pick the primary affiliation from ORCID's institution-name list.

    ORCID returns either a string, a list of strings, or nothing. The
    inline form intentionally takes only the first; surfacing the rest
    is a polish item.
    """
    if not value:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, str):
            return first.strip()
    return ""


def normalize_row(row: dict) -> dict:
    """Turn one ORCID expanded-search hit into the JSON shape the JS expects."""
    orcid_id = (row.get("orcid-id") or "").strip()
    given = (row.get("given-names") or "").strip()
    family = (row.get("family-names") or "").strip()
    credit = (row.get("credit-name") or "").strip()
    institutions = row.get("institution-name") or []
    if not isinstance(institutions, list):
        institutions = [institutions]
    institutions = [str(name).strip() for name in institutions if name]
    return {
        "orcid_id": orcid_id,
        "given_names": given,
        "family_names": family,
        "credit_name": credit,
        "display_name": _published_name(given, family, credit, orcid_id),
        "affiliation": _first_institution(institutions),
        "institutions": institutions,
    }


def expanded_search(query: str, rows: int = 10) -> dict:
    """Call ORCID's public expanded-search endpoint.

    Returns ``{"results": [...], "error": ""}``. On error, returns an
    empty result list and a human-readable error message rather than
    raising; the inline UI degrades silently when ORCID is unreachable.
    """
    query = (query or "").strip()
    if not query:
        return {"results": [], "error": ""}
    params = {"q": query, "rows": str(min(max(rows, 1), 25))}
    url = f"{_ORCID_SEARCH_URL}?{urlencode(params)}"
    req = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
    )
    try:
        with urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return {"results": [], "error": f"ORCID API returned HTTP {exc.code}."}
    except URLError as exc:
        return {"results": [], "error": f"Could not reach ORCID: {exc.reason}."}
    except (ValueError, TimeoutError) as exc:
        return {"results": [], "error": f"Unexpected response from ORCID: {exc}."}
    rows_in = payload.get("expanded-result") or []
    return {
        "results": [normalize_row(r) for r in rows_in],
        "error": "",
    }
