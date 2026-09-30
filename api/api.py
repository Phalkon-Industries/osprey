"""Django Ninja API for the OSPREY demo.

Endpoints under /api/v1/:
    GET /projects/                list + filter
    GET /projects/{slug}/         detail
    GET /schema/                  auto-generated OpenAPI (provided by Ninja)
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from django.conf import settings
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Schema

from projects.models import (
    ArtifactLink,
    Contribution,
    Project,
    ProjectDeposit,
    ProjectDepositVersion,
    ProjectImage,
    Tag,
    TagAssignment,
)

api = NinjaAPI(title="OSPREY demo API", version="1.0.0")


# ---- Schemas -----------------------------------------------------------------


class ContributionOut(Schema):
    user_id: Optional[int] = None
    username: Optional[str] = None
    orcid_id: str = ""
    display_name: str
    role: str
    credit_statement: str = ""
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
    doi: str
    institution: Optional[str] = None
    institutions: list[str] = []
    visibility: str
    origin: str = "native"
    updated_at: datetime


class ProjectImageOut(Schema):
    url: str
    caption: str
    order: int


class ProjectDetail(ProjectSummary):
    description: str
    readme: str
    canonical_url: str
    cover_image_url: str
    cover_image_focal_x: int
    cover_image_focal_y: int
    cover_image_zoom: float
    funding: str = ""
    self_rating: Optional[int] = None
    publications: str = ""
    created_at: datetime
    contributors: list[ContributionOut]
    artifact_links: list[ArtifactLinkOut]
    tags: list[TagOut]
    images: list[ProjectImageOut]


# ---- Helpers -----------------------------------------------------------------


def _project_summary(p: Project) -> dict:
    return {
        "slug": p.slug,
        "title": p.title,
        "summary": p.summary,
        "field": p.field,
        "artifact_type": p.artifact_type,
        "license": p.license,
        "doi": p.doi,
        "institution": p.institution or None,
        "institutions": p.institutions,
        "visibility": p.visibility,
        "origin": p.origin,
        "updated_at": p.updated_at,
    }


def _project_detail(p: Project) -> dict:
    base = _project_summary(p)
    base.update(
        {
            "description": p.description,
            "readme": p.readme,
            "canonical_url": p.canonical_url,
            "cover_image_url": p.cover_image_url,
            "cover_image_focal_x": p.cover_image_focal_x,
            "cover_image_focal_y": p.cover_image_focal_y,
            "cover_image_zoom": float(p.cover_image_zoom),
            "funding": p.funding,
            "self_rating": p.self_rating,
            "publications": p.publications,
            "created_at": p.created_at,
            "contributors": [
                {
                    "user_id": c.user_id,
                    "username": c.user.get_username() if c.user_id else None,
                    "orcid_id": c.verified_orcid_id,
                    "display_name": c.display_name,
                    "role": c.role,
                    "credit_statement": c.credit_statement,
                    "order": c.order,
                }
                for c in p.credited_contributions.select_related("user").all()
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
    origin: str = "",
):
    qs = Project.objects.filter(
        visibility=Project.VISIBILITY_PUBLIC, is_staff_hidden=False
    ).prefetch_related("tags")
    if origin:
        qs = qs.filter(origin=origin)
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
        qs = qs.filter(institution__iexact=institution)
    if tag:
        qs = qs.filter(tags__name=tag)
    return [_project_summary(p) for p in qs.distinct()]


@api.get("/projects/{slug}/", response=ProjectDetail)
def project_detail(request, slug: str):
    qs = Project.objects.prefetch_related("artifact_links", "contributions__user", "tags", "images")
    p = get_object_or_404(qs, slug=slug, visibility=Project.VISIBILITY_PUBLIC)
    return _project_detail(p)


