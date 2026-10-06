"""JSON endpoints behind the Images tab and README image paste.

Owners and editors only. Every response is JSON so the editor can update
in place without a page reload.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from . import images
from .models import Project, ProjectImage


def _project_for_edit(request, slug: str) -> Project:
    project = get_object_or_404(Project, slug=slug)
    if not project.editable_by(request.user):
        raise Http404
    return project


def _cover_id(project: Project) -> int | None:
    return (
        project.images.filter(kind=ProjectImage.KIND_GALLERY)
        .order_by("order", "id")
        .values_list("pk", flat=True)
        .first()
    )


def _reset_crop_if_cover_changed(project: Project, before: int | None) -> None:
    """The crop belongs to the cover. A different first image starts
    centred, whether or not the form is saved afterwards."""
    if _cover_id(project) != before:
        Project.objects.filter(pk=project.pk).update(
            cover_image_focal_x=50, cover_image_focal_y=50, cover_image_zoom=1
        )


def image_json(img: ProjectImage) -> dict:
    return {
        "id": img.pk,
        "kind": img.kind,
        "caption": img.caption,
        "thumb": img.thumb_url,
        "image": img.image.url,
        "full": img.full_url,
        "width": img.width,
        "height": img.height,
        "markdown": f"![{img.caption or 'image'}]({img.image.url})",
    }


@login_required
@require_POST
@ratelimit(key="user", rate=lambda group, request: settings.RATELIMIT_IMAGE_UPLOAD, method="POST", block=True)
def upload(request, slug: str):
    project = _project_for_edit(request, slug)
    kind = request.POST.get("kind", ProjectImage.KIND_GALLERY)
    if kind not in dict(ProjectImage.KIND_CHOICES):
        kind = ProjectImage.KIND_GALLERY
    created, errors = [], []
    before = _cover_id(project)
    for upload_file in request.FILES.getlist("files"):
        try:
            img = images.store(project, upload_file, kind=kind)
        except images.ImageRejected as exc:
            errors.append(str(exc))
            continue
        created.append(image_json(img))
    _reset_crop_if_cover_changed(project, before)
    status = 200 if created or not errors else 400
    return JsonResponse(
        {"images": created, "errors": errors, "count": project.images.count(), "max": images.MAX_IMAGES},
        status=status,
    )


@login_required
@require_POST
def caption(request, slug: str, image_id: int):
    project = _project_for_edit(request, slug)
    img = get_object_or_404(ProjectImage, pk=image_id, project=project)
    img.caption = request.POST.get("caption", "").strip()[:300]
    img.save(update_fields=["caption"])
    return JsonResponse({"image": image_json(img)})


@login_required
@require_POST
def reorder(request, slug: str):
    """Gallery order from a comma-separated list of ids; the first is the cover."""
    project = _project_for_edit(request, slug)
    before = _cover_id(project)
    wanted = [int(i) for i in request.POST.get("ids", "").split(",") if i.strip().isdigit()]
    gallery = {img.pk: img for img in project.images.filter(kind=ProjectImage.KIND_GALLERY)}
    ordered = [gallery[i] for i in wanted if i in gallery]
    ordered += [img for pk, img in gallery.items() if pk not in wanted]
    for position, img in enumerate(ordered):
        if img.order != position:
            img.order = position
            img.save(update_fields=["order"])
    _reset_crop_if_cover_changed(project, before)
    return JsonResponse({"ids": [img.pk for img in ordered]})


@login_required
@require_POST
def delete(request, slug: str, image_id: int):
    project = _project_for_edit(request, slug)
    img = get_object_or_404(ProjectImage, pk=image_id, project=project)
    before = _cover_id(project)
    images.delete_files(img)
    img.delete()
    _reset_crop_if_cover_changed(project, before)
    return JsonResponse({"deleted": image_id, "count": project.images.count()})
