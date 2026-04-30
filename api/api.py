"""Django Ninja API for the OSPREY demo.

Endpoints under /api/v1/:
    GET /projects/                list + filter
    GET /projects/{slug}/         detail
    GET /lineage/{slug}/          local lineage subgraph
    GET /export/                  full database dump
    GET /schema/                  auto-generated OpenAPI (provided by Ninja)
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Schema

from people.models import Institution, Profile
from projects.models import (
    ArtifactLink,
    Citation,
    Contribution,
    LineageEdge,
    Project,
    ProjectImage,
    Tag,
    TagAssignment,
)


api = NinjaAPI(title="OSPREY demo API", version="1.0.0")


# ---- Schemas -----------------------------------------------------------------


class ContributionOut(Schema):
    user_id: int
    username: str
    role: str
    order: int


class ArtifactLinkOut(Schema):
    kind: str
    url: str
    label: str


class TagOut(Schema):
    name: str


class ProjectSummary(Schema):
    slug: str
    title: str
    summary: str
    field: str
    artifact_type: str
    license: str
    institution: Optional[str] = None
    placeholder_doi: str
    visibility: str
    updated_at: datetime


class ProjectImageOut(Schema):
    url: str
    caption: str
    order: int


class CitationOut(Schema):
    text: str
    url: str
    doi: str
    year: Optional[int] = None


class ProjectDetail(ProjectSummary):
    description: str
    readme: str
    canonical_url: str
    created_at: datetime
    contributors: list[ContributionOut]
    artifact_links: list[ArtifactLinkOut]
    tags: list[TagOut]
    images: list[ProjectImageOut]
    citations: list[CitationOut]


class LineageEdgeOut(Schema):
    parent_slug: str
    child_slug: str
    relation: str


class LineageOut(Schema):
    project: str
    nodes: list[ProjectSummary]
    edges: list[LineageEdgeOut]


# ---- Helpers -----------------------------------------------------------------


def _project_summary(p: Project) -> dict:
    return {
        "slug": p.slug,
        "title": p.title,
        "summary": p.summary,
        "field": p.field,
        "artifact_type": p.artifact_type,
        "license": p.license,
        "institution": p.institution.short_name or p.institution.name
        if p.institution
        else None,
        "placeholder_doi": p.placeholder_doi or f"10.demo/{p.slug}",
        "visibility": p.visibility,
        "updated_at": p.updated_at,
    }


def _project_detail(p: Project) -> dict:
    base = _project_summary(p)
    base.update(
        {
            "description": p.description,
            "readme": p.readme,
            "canonical_url": p.canonical_url,
            "created_at": p.created_at,
            "contributors": [
                {
                    "user_id": c.user_id,
                    "username": c.user.get_username(),
                    "role": c.role,
                    "order": c.order,
                }
                for c in p.contributions.select_related("user").all()
            ],
            "artifact_links": [
                {"kind": a.kind, "url": a.url, "label": a.label}
                for a in p.artifact_links.all()
            ],
            "tags": [{"name": t.name} for t in p.tags.all()],
            "images": [
                {"url": img.image.url, "caption": img.caption, "order": img.order}
                for img in p.images.all()
            ],
            "citations": [
                {"text": c.text, "url": c.url, "doi": c.doi, "year": c.year}
                for c in p.citations.all()
            ],
        }
    )
    return base


# ---- Endpoints ---------------------------------------------------------------


@api.get("/projects/", response=list[ProjectSummary])
def list_projects(
    request,
    q: str = "",
    field: str = "",
    artifact_type: str = "",
    institution: str = "",
    tag: str = "",
):
    qs = Project.objects.filter(visibility=Project.VISIBILITY_PUBLIC).select_related(
        "institution"
    ).prefetch_related("tags")
    if q:
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(summary__icontains=q)
            | Q(description__icontains=q)
            | Q(slug__icontains=q)
        )
    if field:
        qs = qs.filter(field=field)
    if artifact_type:
        qs = qs.filter(artifact_type=artifact_type)
    if institution:
        qs = qs.filter(institution__short_name=institution)
    if tag:
        qs = qs.filter(tags__name=tag)
    return [_project_summary(p) for p in qs.distinct()]


@api.get("/projects/{slug}/", response=ProjectDetail)
def project_detail(request, slug: str):
    p = get_object_or_404(
        Project.objects.select_related("institution").prefetch_related(
            "artifact_links", "contributions__user", "tags", "images", "citations"
        ),
        slug=slug,
        visibility=Project.VISIBILITY_PUBLIC,
    )
    return _project_detail(p)


@api.get("/lineage/{slug}/", response=LineageOut)
def lineage(request, slug: str, depth: int = 2):
    project = get_object_or_404(Project, slug=slug)
    seen_ids: set[int] = {project.pk}
    edges: set[tuple[int, int, str]] = set()
    frontier = {project.pk}
    for _ in range(max(0, depth)):
        if not frontier:
            break
        related = LineageEdge.objects.filter(
            Q(parent_id__in=frontier) | Q(child_id__in=frontier)
        ).select_related("parent", "child")
        next_frontier: set[int] = set()
        for e in related:
            edges.add((e.parent_id, e.child_id, e.relation))
            for pk in (e.parent_id, e.child_id):
                if pk not in seen_ids:
                    seen_ids.add(pk)
                    next_frontier.add(pk)
        frontier = next_frontier

    nodes = Project.objects.filter(pk__in=seen_ids).select_related("institution")
    slug_by_id = {n.pk: n.slug for n in nodes}
    return {
        "project": project.slug,
        "nodes": [_project_summary(n) for n in nodes],
        "edges": [
            {
                "parent_slug": slug_by_id.get(parent_id, ""),
                "child_slug": slug_by_id.get(child_id, ""),
                "relation": relation,
            }
            for parent_id, child_id, relation in edges
        ],
    }


@api.get("/export/")
def full_export(request):
    """Single-shot dump of the public database. Survivability promise made concrete.

    Only `visibility=public` projects are exported; private drafts stay out.
    """

    User = get_user_model()
    public_project_ids = list(
        Project.objects.filter(visibility=Project.VISIBILITY_PUBLIC).values_list(
            "id", flat=True
        )
    )
    return {
        "version": "1",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "institutions": list(
            Institution.objects.values("id", "name", "short_name")
        ),
        "users": [
            {
                "id": u.id,
                "username": u.get_username(),
                "display_name": getattr(getattr(u, "profile", None), "display_name", "")
                or u.get_full_name(),
                "orcid_placeholder": getattr(
                    getattr(u, "profile", None), "orcid_placeholder", ""
                ),
                "institution_id": getattr(
                    getattr(u, "profile", None), "institution_id", None
                ),
            }
            for u in User.objects.select_related("profile", "profile__institution")
        ],
        "projects": list(
            Project.objects.filter(id__in=public_project_ids).values(
                "id",
                "slug",
                "title",
                "summary",
                "description",
                "readme",
                "artifact_type",
                "field",
                "license",
                "placeholder_doi",
                "canonical_url",
                "visibility",
                "institution_id",
                "created_at",
                "updated_at",
            )
        ),
        "contributions": list(
            Contribution.objects.filter(project_id__in=public_project_ids).values(
                "id", "project_id", "user_id", "role", "order"
            )
        ),
        "artifact_links": list(
            ArtifactLink.objects.filter(project_id__in=public_project_ids).values(
                "id", "project_id", "kind", "url", "label"
            )
        ),
        "lineage_edges": list(
            LineageEdge.objects.filter(
                parent_id__in=public_project_ids, child_id__in=public_project_ids
            ).values("id", "parent_id", "child_id", "relation", "note")
        ),
        "tags": list(Tag.objects.values("id", "name")),
        "tag_assignments": list(
            TagAssignment.objects.filter(project_id__in=public_project_ids).values(
                "id", "project_id", "tag_id"
            )
        ),
        "project_images": list(
            ProjectImage.objects.filter(project_id__in=public_project_ids).values(
                "id", "project_id", "image", "caption", "order"
            )
        ),
        "citations": list(
            Citation.objects.filter(project_id__in=public_project_ids).values(
                "id", "project_id", "text", "url", "doi", "year", "order"
            )
        ),
    }
