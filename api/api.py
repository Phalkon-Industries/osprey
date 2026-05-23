"""Django Ninja API for the OSPREY demo.

Endpoints under /api/v1/:
    GET /projects/                list + filter
    GET /projects/{slug}/         detail
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

from people.models import Profile
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
        "updated_at": p.updated_at,
    }


def _verified_profile_orcid(user) -> str:
    account = user.socialaccount_set.filter(provider="orcid").first()
    if account is None:
        return ""
    identifier = (account.extra_data or {}).get("orcid-identifier") or {}
    account_orcid = (identifier.get("path") or "").strip()
    if account_orcid:
        return account_orcid
    try:
        profile_orcid = user.profile.orcid_placeholder
    except Profile.DoesNotExist:
        profile_orcid = ""
    return profile_orcid


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
    qs = Project.objects.filter(visibility=Project.VISIBILITY_PUBLIC).prefetch_related(
        "tags"
    )
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
    p = get_object_or_404(
        Project.objects.prefetch_related(
            "artifact_links", "contributions__user", "tags", "images"
        ),
        slug=slug,
        visibility=Project.VISIBILITY_PUBLIC,
    )
    return _project_detail(p)


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
        "users": [
            {
                "id": u.id,
                "username": u.get_username(),
                "display_name": getattr(getattr(u, "profile", None), "display_name", "")
                or u.get_full_name(),
                "orcid_placeholder": _verified_profile_orcid(u),
                "institution": getattr(getattr(u, "profile", None), "institution", ""),
            }
            for u in User.objects.select_related("profile")
        ],
        "projects": list(
            Project.objects.filter(id__in=public_project_ids).values(
                "id",
                "public_id",
                "slug",
                "title",
                "summary",
                "description",
                "readme",
                "artifact_type",
                "field",
                "license",
                "doi",
                "canonical_url",
                "cover_image_url",
                "cover_image_focal_x",
                "cover_image_focal_y",
                "cover_image_zoom",
                "visibility",
                "institution",
                "funding",
                "self_rating",
                "publications",
                "created_at",
                "updated_at",
            )
        ),
        "contributions": [
            {
                "id": c.id,
                "project_id": c.project_id,
                "user_id": c.user_id,
                "orcid_id": c.verified_orcid_id,
                "display_name": c.display_name,
                "role": c.role,
                "credit_statement": c.credit_statement,
                "order": c.order,
            }
            for c in Contribution.objects.filter(
                project_id__in=public_project_ids
            ).select_related("user")
        ],
        "artifact_links": list(
            ArtifactLink.objects.filter(project_id__in=public_project_ids).values(
                "id", "project_id", "kind", "url", "label"
            )
        ),
        "project_deposits": list(
            ProjectDeposit.objects.filter(project_id__in=public_project_ids).values(
                "id",
                "project_id",
                "provider",
                "sandbox",
                "deposition_id",
                "record_id",
                "concept_id",
                "doi",
                "concept_doi",
                "state",
                "created_at",
                "updated_at",
                "published_at",
            )
        ),
        "project_deposit_versions": list(
            ProjectDepositVersion.objects.filter(
                deposit__project_id__in=public_project_ids
            ).values(
                "id",
                "deposit_id",
                "version_index",
                "deposition_id",
                "record_id",
                "doi",
                "changelog",
                "repo_link",
                "published_at",
            )
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
    }
