"""Project full-text search (Postgres).

Each project stores a weighted tsvector, rebuilt by signals whenever the
project, its tags, its contributors or its wiki pages change:

    A  title
    B  summary, tags, credited contributors, institutions
    C  README
    D  wiki pages

Queries use websearch syntax ("quoted phrases", -exclusions) with every
word also matched as a prefix, so "therm" finds "thermistor". A pasted
DOI skips text search and matches project, deposit and version DOIs.
"""

from __future__ import annotations

import re

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db import connection
from django.db.models import F, Q, TextField, Value

CONFIG = "english"

# Fields on Project itself that feed the index. A save that touches none
# of them skips the rebuild.
PROJECT_FIELDS = {"title", "summary", "readme", "institution"}

_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"(?:https?://|www\.)\S+")
_TAG = re.compile(r"<[^>]+>")
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")
_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)
_LEXEME = re.compile(r"('(?:[^']|'')*')")


def _plain(markdown: str) -> str:
    """Markdown with image and link URLs removed, keeping their text."""
    text = _IMAGE.sub(r"\1", markdown or "")
    text = _LINK.sub(r"\1", text)
    text = _URL.sub(" ", text)
    return _TAG.sub(" ", text)


def _vector(text: str, weight: str) -> SearchVector:
    return SearchVector(Value(text, output_field=TextField()), weight=weight, config=CONFIG)


def rebuild(project_id: int) -> None:
    from wiki.models import WikiPage

    from .models import Contribution, Project, TagAssignment

    row = (
        Project.objects.filter(pk=project_id)
        .values("title", "summary", "readme", "institution")
        .first()
    )
    if row is None:
        return
    tags = TagAssignment.objects.filter(project_id=project_id).values_list("tag__name", flat=True)
    people = (
        Contribution.objects.filter(project_id=project_id)
        .exclude(claim_status=Contribution.CLAIM_DECLINED)
        .values_list("display_name", "affiliation")
    )
    wiki = WikiPage.objects.filter(project_id=project_id).values_list("title", "body")
    b_text = " ".join(
        [row["summary"], row["institution"], *tags, *(f"{n} {a}" for n, a in people)]
    )
    d_text = " ".join(f"{t} {_plain(b)}" for t, b in wiki)
    vector = (
        _vector(row["title"], "A")
        + _vector(b_text, "B")
        + _vector(_plain(row["readme"]), "C")
        + _vector(d_text, "D")
    )
    Project.objects.filter(pk=project_id).update(search_vector=vector)


def rebuild_missing() -> None:
    """Index projects that have never been indexed (run after migrate)."""
    from .models import Project

    for pk in Project.objects.filter(search_vector=None).values_list("pk", flat=True):
        rebuild(pk)


def _doi(q: str) -> str:
    doi = _DOI_PREFIX.sub("", q.strip())
    return doi if _DOI.match(doi) else ""


def _prefix_query(q: str) -> str:
    """websearch_to_tsquery output with every lexeme made a prefix match."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT websearch_to_tsquery(%s, %s)::text", [CONFIG, q])
        text = cursor.fetchone()[0]
    return _LEXEME.sub(r"\1:*", text)


def search(qs, q: str):
    """Filter and rank a Project queryset by a search box query."""
    q = (q or "").strip()
    if not q:
        return qs
    doi = _doi(q)
    if doi:
        return qs.filter(
            Q(doi__iexact=doi)
            | Q(deposits__doi__iexact=doi)
            | Q(deposits__concept_doi__iexact=doi)
            | Q(deposits__versions__doi__iexact=doi)
        )
    text = _prefix_query(q)
    if not text:
        return qs.none()
    # Lexemes are already stemmed; "simple" only parses the operators.
    query = SearchQuery(text, search_type="raw", config="simple")
    return (
        qs.filter(search_vector=query)
        .annotate(search_rank=SearchRank(F("search_vector"), query))
        .order_by("-search_rank", "-updated_at")
    )
