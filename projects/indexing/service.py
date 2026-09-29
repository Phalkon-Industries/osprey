"""Resolve pasted lines into previews; create, approve and decline entries."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

from projects import claiming
from projects.models import Contribution, Project, ProjectDeposit, Tag, TagAssignment
from projects.zenodo_register import normalize_orcid

from . import github_source, zenodo_source
from .records import SourceError, SourceRecord, detect

logger = logging.getLogger(__name__)

ADAPTERS = {
    "zenodo": zenodo_source.fetch,
    "github": github_source.fetch,
}

# Preview outcomes.
LIVE = "live"          # both gates pass; goes public on import
HELD = "held"          # created invisible, waits in the review queue
EXISTS = "exists"      # already a project or entry on OSPREY
YOURS = "yours"        # the submitter is a creator: register it instead
UNSUPPORTED = "unsupported"  # no adapter yet for this source
ERROR = "error"        # the source could not be read


@dataclass
class Preview:
    line: str
    source: str = ""
    external_id: str = ""
    outcome: str = ERROR
    message: str = ""
    record: SourceRecord | None = None
    existing: Project | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def can_import(self) -> bool:
        return self.outcome in (LIVE, HELD)


def _repo_matches(full_name: str):
    """Projects that already point at this GitHub repository: as their
    canonical URL, as an artifact link, or as an indexed entry."""
    from django.db.models import Q

    from projects.models import ArtifactLink

    owner, _, name = full_name.partition("/")
    if not owner or not name:
        return Project.objects.none()
    pattern = rf"^https?://(www\.)?github\.com/{owner}/{name}(\.git)?/?$"
    linked = ArtifactLink.objects.filter(url__iregex=pattern).values_list("project_id", flat=True)
    return Project.objects.filter(
        Q(canonical_url__iregex=pattern)
        | Q(pk__in=linked)
        | Q(source=Project.SOURCE_GITHUB, external_id__iexact=full_name)
    ).order_by("origin")


def find_existing(source: str, external_id: str, doi: str = "", concept_id: str = "") -> Project | None:
    qs = Project.objects.all()
    if source == "github" and external_id:
        # Prefer the owned project over an index entry when both exist.
        hit = _repo_matches(external_id).first()
        if hit:
            return hit
    if external_id:
        hit = qs.filter(source=source, external_id__iexact=external_id).first()
        if hit:
            return hit
    if concept_id:
        dep = ProjectDeposit.objects.filter(provider=ProjectDeposit.PROVIDER_ZENODO, concept_id=concept_id).select_related("project").first()
        if dep:
            return dep.project
    if doi:
        hit = qs.filter(doi__iexact=doi).first()
        if hit:
            return hit
    return None


def resolve(line: str, user=None) -> Preview:
    """One pasted line to one preview. Never raises."""
    preview = Preview(line=line)
    source, external_id = detect(line)
    preview.source, preview.external_id = source, external_id
    if not source:
        preview.outcome, preview.message = ERROR, "Empty line."
        return preview
    if source not in ADAPTERS:
        preview.outcome = UNSUPPORTED
        preview.message = {
            "hardwarex": "HardwareX import isn't wired up yet.",
            "joh": "Journal of Open Hardware import isn't wired up yet.",
            "doi": "Only Zenodo DOIs can be indexed automatically for now.",
            "url": "Plain URLs can't be indexed automatically yet.",
        }.get(source, "Unsupported source.")
        return preview
    existing = find_existing(source, external_id, doi=external_id if source == "zenodo" else "")
    if existing is not None:
        preview.outcome, preview.existing = EXISTS, existing
        preview.message = f"Already on OSPREY as “{existing.title}”."
        return preview
    try:
        record = ADAPTERS[source](external_id)
    except SourceError as exc:
        preview.outcome, preview.message = ERROR, str(exc)
        return preview
    preview.record = record
    existing = find_existing(record.source, record.external_id, doi=record.doi, concept_id=record.concept_id)
    if existing is not None:
        preview.outcome, preview.existing = EXISTS, existing
        preview.message = f"Already on OSPREY as “{existing.title}”."
        return preview
    if user is not None and record.source == "zenodo":
        orcid = normalize_orcid(claiming.orcid_for(user) or "")
        if orcid and any(a.orcid == orcid for a in record.authors):
            preview.outcome = YOURS
            preview.message = "You're a creator on this record. Register it instead."
            return preview
    if not record.gate_license.ok:
        preview.reasons.append(f"License: {record.gate_license.found}.")
    if not record.gate_files.ok:
        preview.reasons.append(f"Files: {record.gate_files.found}.")
    preview.outcome = LIVE if record.passes else HELD
    preview.message = "Both gates pass." if record.passes else "Held for review: " + " ".join(preview.reasons)
    return preview


def _unique_slug(title: str) -> str:
    base = slugify(title)[:60].strip("-") or "entry"
    slug, n = base, 2
    while Project.objects.filter(slug=slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


@transaction.atomic
def index_record(record: SourceRecord, *, listed_by=None, hold: bool | None = None) -> Project:
    """Create the entry, or refresh an existing one from the same source.

    `hold` overrides the gate verdict (staff can force a hold). Never
    notifies anyone; the caller sends the run summary.
    """
    held = (not record.passes) if hold is None else hold
    project = Project.objects.filter(source=record.source, external_id=record.external_id).first()
    creating = project is None
    if creating:
        project = Project(source=record.source, external_id=record.external_id, slug=_unique_slug(record.title), indexed_at=timezone.now(), listed_by=listed_by)
    project.origin = Project.ORIGIN_INDEXED
    project.title = record.title[:300]
    project.summary = record.summary[:280]
    project.readme = record.readme
    project.canonical_url = record.canonical_url[:200]
    project.doi = record.doi[:120]
    project.license = record.license[:80]
    project.artifact_type = project.artifact_type or record.artifact_type
    project.field = project.field or record.field_name
    project.institution = project.institution or record.institution
    project.files_url = record.files_url[:200]
    project.files_url_source = record.files_url_source
    project.published_on = record.published_on
    project.oshwa_uid = record.oshwa_uid
    project.gate_license = record.gate_license.as_dict()
    project.gate_files = record.gate_files.as_dict()
    project.source_metadata = {**record.raw, "text_license": record.text_license} if isinstance(record.raw, dict) else {"text_license": record.text_license}
    project.index_checked_at = timezone.now()
    if creating or project.index_state == Project.INDEX_HELD:
        project.index_state = Project.INDEX_HELD if held else Project.INDEX_LIVE
        project.visibility = Project.VISIBILITY_PRIVATE if held else Project.VISIBILITY_PUBLIC
    project.save()
    # Author rows: replace the unclaimed ones, keep any a person has claimed.
    project.contributions.filter(user__isnull=True).delete()
    claimed = {normalize_orcid(o) for o in project.contributions.exclude(orcid_id="").values_list("orcid_id", flat=True)}
    for i, author in enumerate(record.authors):
        if author.orcid and author.orcid in claimed:
            continue
        Contribution.objects.create(project=project, display_name=author.name[:200] or "Author", orcid_id=author.orcid, affiliation=author.affiliation[:200], role="Author", order=i)
    if creating:
        for name in record.keywords:
            tag, _ = Tag.objects.get_or_create(name=name[:80])
            TagAssignment.objects.get_or_create(project=project, tag=tag)
    return project


def split_names(text: str) -> list[str]:
    """'A. Person, B. Person' or one per line -> names, deduped, max 20."""
    out: list[str] = []
    for part in (text or "").replace(";", ",").replace("\n", ",").split(","):
        name = " ".join(part.split())[:200]
        if name and name not in out:
            out.append(name)
    return out[:20]


@dataclass
class Submitted:
    """What the submitter typed for one line on the second step."""
    title: str = ""
    summary: str = ""
    license: str = ""
    files_url: str = ""
    authors: str = ""

    def clean(self) -> "Submitted":
        return Submitted(
            title=self.title.strip()[:300],
            summary=self.summary.strip()[:280],
            license=self.license.strip()[:80],
            files_url=self.files_url.strip()[:200],
            authors=", ".join(split_names(self.authors)),
        )


def _replace_author_rows(project: Project, names: list[str]) -> None:
    """Unclaimed author rows become these names; rows a person claimed stay."""
    project.contributions.filter(user__isnull=True).delete()
    start = project.contributions.count()
    for i, name in enumerate(names):
        Contribution.objects.create(project=project, display_name=name, role="Author", order=start + i)


def request_row(preview: Preview, *, listed_by, submitted: Submitted | None = None) -> Project:
    """A held entry for a source OSPREY can't read yet (journal DOIs, other
    DOIs, plain URLs), filled in from what the submitter typed. Staff check
    it against the original and approve or decline."""
    source = preview.source if preview.source in ("hardwarex", "joh") else Project.SOURCE_OTHER
    external_id = preview.external_id[:200]
    existing = Project.objects.filter(source=source, external_id=external_id).first()
    if existing:
        return existing
    given = (submitted or Submitted()).clean()
    is_doi = preview.source in ("hardwarex", "joh", "doi")
    url = f"https://doi.org/{external_id}" if is_doi else external_id
    who = listed_by.get_username() if listed_by else "staff"
    project = Project(
        slug=_unique_slug(given.title or external_id.rsplit("/", 1)[-1] or "entry"),
        title=given.title or external_id[:300],
        summary=given.summary,
        readme=given.summary,
        license=given.license,
        files_url=given.files_url,
        files_url_source="submitter" if given.files_url else "",
        canonical_url=url[:200],
        doi=external_id[:120] if is_doi else "",
        origin=Project.ORIGIN_INDEXED,
        source=source,
        external_id=external_id,
        index_state=Project.INDEX_HELD,
        visibility=Project.VISIBILITY_PRIVATE,
        listed_by=listed_by,
        indexed_at=timezone.now(),
        gate_license={"ok": False, "found": given.license or "not given", "where": f"chosen by @{who}; not checked against the source"},
        gate_files={"ok": False, "found": given.files_url or "not given", "where": f"entered by @{who}; not checked"},
        source_metadata={"submitted": {"by": who, **given.__dict__}},
    )
    project.save()
    _replace_author_rows(project, split_names(given.authors))
    return project


def authors_editable(record: SourceRecord | None) -> bool:
    """Authors are editable only when OSPREY couldn't read the source.
    Zenodo gives creators with ORCIDs; GitHub gives the repository owner,
    and that is the author of record for a repository."""
    return record is None


def apply_submitted(project: Project, submitted: Submitted | None, by, record: SourceRecord | None = None) -> Project:
    """Submitter's summary, and authors where the source had none worth
    keeping, on an entry the adapter filled in. Title, license and files
    stay the source's: they are the gates."""
    if submitted is None:
        return project
    given = submitted.clean()
    changed: dict = {}
    if given.summary and given.summary != project.summary:
        project.summary = given.summary
        changed["summary"] = given.summary
    names = split_names(given.authors)
    if names and (record is None or authors_editable(record)):
        current = list(project.contributions.filter(user__isnull=True).values_list("display_name", flat=True))
        if names != current:
            _replace_author_rows(project, names)
            changed["authors"] = names
    if changed:
        meta = dict(project.source_metadata or {})
        meta["submitted"] = {"by": by.get_username() if by else "", **changed}
        project.source_metadata = meta
        project.save(update_fields=["summary", "source_metadata"])
    return project


def _tell_submitter(project: Project, *, title: str, body: str = "", url: str = "") -> None:
    if project.listed_by_id is None:
        return
    from notifications.models import send

    send(project.listed_by, kind="index_decision", title=title, body=body, url=url or project.get_absolute_url())


def approve(project: Project, staff, *, title: str = "", license: str = "", files_url: str = "", summary: str = "") -> Project:
    if title:
        project.title = title[:300]
    if license:
        project.license = license[:80]
    if files_url:
        project.files_url = files_url[:200]
        project.files_url_source = "staff"
    if summary:
        project.summary = summary[:280]
    project.index_state = Project.INDEX_LIVE
    project.visibility = Project.VISIBILITY_PUBLIC
    project.gate_license = {**(project.gate_license or {}), "ok": True, "approved_by": staff.get_username(), "approved_at": timezone.now().isoformat()}
    project.gate_files = {**(project.gate_files or {}), "ok": True}
    project.save()
    _tell_submitter(project, title=f"Added to the index: {project.title}"[:200])
    return project


def decline(project: Project, staff, reason: str = "") -> None:
    logger.info("indexed entry declined by %s: %s %s (%s)", staff.get_username(), project.source, project.external_id, reason)
    _tell_submitter(
        project,
        title=f"Not added to the index: {project.title}"[:200],
        body=(reason or "").strip()[:500],
        url=reverse("projects:list"),
    )
    project.delete()


def notify_staff_of_run(results: list[tuple[Preview, Project | None]], by=None, *, public: bool = False) -> None:
    """One inbox line for staff per import run or public submission."""
    from notifications.models import send
    from notifications.events import _staff

    live = sum(1 for p, proj in results if proj is not None and proj.index_state == Project.INDEX_LIVE)
    held = sum(1 for p, proj in results if proj is not None and proj.index_state == Project.INDEX_HELD)
    if not live and not held:
        return
    who = f"@{by.get_username()}" if by else "OSPREY"
    title = f"Index submission from {who}: {held} to review" if public else f"Indexed {live} live, {held} held"
    for user in _staff():
        send(user, kind="index_run", title=title[:200], body=f"Import run by {who}.", url=reverse("index_staff"))
