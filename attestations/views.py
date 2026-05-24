from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from notifications.models import send as notify
from projects.models import Project

from .forms import AttestationForm
from .models import Attestation


def _get_project(slug):
    project = get_object_or_404(Project, slug=slug)
    return project


def index(request, slug):
    project = _get_project(slug)
    if project.visibility != Project.VISIBILITY_PUBLIC and not project.editable_by(
        request.user
    ):
        raise Http404
    qs = project.attestations.filter(visibility=Attestation.VIS_PUBLIC)
    return render(
        request,
        "attestations/index.html",
        {
            "project": project,
            "attestations": qs,
            "is_maintainer": project.editable_by(request.user),
            "can_attest": request.user.is_authenticated,
        },
    )


@login_required
def new(request, slug):
    project = _get_project(slug)
    if project.visibility != Project.VISIBILITY_PUBLIC and not project.editable_by(
        request.user
    ):
        raise Http404
    if request.method == "POST":
        form = AttestationForm(request.POST)
        if form.is_valid():
            att = form.save(commit=False)
            att.project = project
            att.author = request.user
            att.save()
            from projects.models import Citation

            for line in form.citation_lines():
                Citation.objects.create(
                    project=project,
                    text=line[:600],
                    source=Citation.SOURCE_ATTESTATION,
                    submitted_by=request.user,
                    attestation=att,
                )
            for c in project.contributions.filter(user__isnull=False).select_related(
                "user"
            ):
                if c.user_id == request.user.id:
                    continue
                notify(
                    c.user,
                    kind="attestation",
                    title=f"New attestation on {project.title}",
                    body=f"@{request.user.get_username()} shared how they used your project.",
                    url=reverse("attestations:index", args=[project.slug]),
                    project_slug=project.slug,
                    attestation_id=att.pk,
                )
            messages.success(request, "Thanks for sharing how you used this project.")
            return redirect("attestations:index", slug=project.slug)
    else:
        form = AttestationForm()
    return render(
        request,
        "attestations/new.html",
        {"project": project, "form": form},
    )


@login_required
@require_POST
def moderate(request, slug, attestation_id):
    project = _get_project(slug)
    if not project.editable_by(request.user):
        return HttpResponseForbidden("Only maintainers can moderate attestations.")
    att = get_object_or_404(Attestation, pk=attestation_id, project=project)
    action = (request.POST.get("action") or "").strip()
    now = timezone.now()
    if action == "endorse":
        att.endorsement = Attestation.ENDORSE_ACKNOWLEDGED
        att.endorsed_at = now
        att.endorsed_by = request.user
    elif action == "feature":
        att.endorsement = Attestation.ENDORSE_FEATURED
        att.endorsed_at = now
        att.endorsed_by = request.user
    elif action == "unendorse":
        att.endorsement = Attestation.ENDORSE_NONE
        att.endorsed_at = None
        att.endorsed_by = None
    elif action == "hide":
        att.visibility = Attestation.VIS_HIDDEN
    elif action == "show":
        att.visibility = Attestation.VIS_PUBLIC
    else:
        return redirect("attestations:index", slug=project.slug)
    att.save()
    if (
        action in {"endorse", "feature"}
        and att.author
        and att.author_id != request.user.id
    ):
        notify(
            att.author,
            kind="attestation",
            title=f"A maintainer {att.get_endorsement_display().lower()} your attestation on {project.title}",
            url=reverse("attestations:index", args=[project.slug]),
        )
    return redirect("attestations:index", slug=project.slug)
