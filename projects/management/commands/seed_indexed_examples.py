"""Create a handful of real indexed entries by hand, for playing with the
entry page before the importer exists.

Reads projects/testing/fixtures/indexed_examples.json, which was built from
live API responses on 2026-09-28 (see planning/features/indexed-entries.md).
Idempotent: matches on (source, external_id) and updates in place. Sends no
notifications. Safe to run on dev repeatedly.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from projects.models import Contribution, Project, Tag, TagAssignment

FIXTURE = Path(__file__).resolve().parents[2] / "testing" / "fixtures" / "indexed_examples.json"


def seed_indexed_examples(only: set[str] | None = None) -> list[Project]:
    entries = json.loads(FIXTURE.read_text())
    made = []
    for e in entries:
        if only and e["source"] not in only:
            continue
        made.append(_upsert(e))
    return made


@transaction.atomic
def _upsert(e: dict) -> Project:
    held = e.get("index_state") == Project.INDEX_HELD
    fields = dict(
        origin=Project.ORIGIN_INDEXED,
        title=e["title"],
        summary=e.get("summary", ""),
        readme=e.get("readme", ""),
        canonical_url=e.get("canonical_url", ""),
        doi=e.get("doi", ""),
        license=e.get("license", ""),
        field=e.get("field", ""),
        artifact_type=e.get("artifact_type", ""),
        institution=e.get("institution", ""),
        files_url=e.get("files_url", ""),
        files_url_source=e.get("files_url_source", ""),
        published_on=date.fromisoformat(e["published_on"]) if e.get("published_on") else None,
        index_state=e.get("index_state", Project.INDEX_LIVE),
        visibility=Project.VISIBILITY_PRIVATE if held else Project.VISIBILITY_PUBLIC,
        gate_license=e.get("gate_license", {}),
        gate_files=e.get("gate_files", {}),
        source_metadata=e.get("source_metadata", {}),
        oshwa_uid=e.get("oshwa_uid", ""),
        index_checked_at=timezone.now(),
    )
    project = Project.objects.filter(source=e["source"], external_id=e["external_id"]).first()
    if project is None:
        base = slugify(e["title"])[:60].strip("-") or e["source"]
        slug, n = base, 2
        while Project.objects.filter(slug=slug).exists():
            slug, n = f"{base}-{n}", n + 1
        project = Project(source=e["source"], external_id=e["external_id"], slug=slug, indexed_at=timezone.now())
    for k, v in fields.items():
        setattr(project, k, v)
    project.save()
    project.contributions.filter(user__isnull=True).delete()
    for i, a in enumerate(e.get("authors", [])):
        Contribution.objects.create(
            project=project,
            display_name=a["name"],
            orcid_id=a.get("orcid", ""),
            affiliation=a.get("affiliation", ""),
            role="Author",
            order=i,
        )
    TagAssignment.objects.filter(project=project).delete()
    for name in e.get("tags", []):
        tag, _ = Tag.objects.get_or_create(name=name[:60])
        TagAssignment.objects.get_or_create(project=project, tag=tag)
    return project


class Command(BaseCommand):
    help = "Seed the example indexed entries from the fixture file (dev only)."

    def add_arguments(self, parser):
        parser.add_argument("--only", nargs="*", help="Source keys to seed (zenodo github hardwarex joh).")

    def handle(self, *args, **options):
        made = seed_indexed_examples(set(options["only"] or []) or None)
        for p in made:
            self.stdout.write(f"{p.index_state:4} {p.source:9} /projects/{p.slug}/  {p.title[:60]}")
