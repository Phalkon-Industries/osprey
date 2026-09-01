from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model, logout
from django.contrib.auth.decorators import login_required
from django.contrib.contenttypes.models import ContentType
from django.http import Http404, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from notifications import events

logger = logging.getLogger(__name__)
from django_ratelimit.decorators import ratelimit

from .forms import ReportForm
from .helpers import log_action
from .models import ModerationLog, Report

User = get_user_model()


# app_label.model strings of content that can be reported. Anything else
# returns 404 from the report form to prevent abuse of the report system
# as a generic reference tracker.
REPORTABLE_MODELS = {
    "projects.project",
    "wiki.wikipage",
    "conversations.projectthread",
    "conversations.projectreply",
    "use_reports.usereport",
    "auth.user",
}


def _resolve_target(app_label: str, model: str, target_id: int):
    key = f"{app_label}.{model}".lower()
    if key not in REPORTABLE_MODELS:
        raise Http404
    try:
        ct = ContentType.objects.get(app_label=app_label, model=model)
    except ContentType.DoesNotExist as exc:
        raise Http404 from exc
    model_class = ct.model_class()
    if model_class is None:
        raise Http404
    obj = get_object_or_404(model_class, pk=target_id)
    return ct, obj


@login_required
@ratelimit(key="user", rate=settings.RATELIMIT_REPORT_SUBMIT, method="POST", block=True)
def report_new(request, app_label: str, model: str, target_id: int):
    ct, target = _resolve_target(app_label, model, target_id)
    context_url = (request.GET.get("next") or request.META.get("HTTP_REFERER") or "")[
        :500
    ]
    if request.method == "POST":
        form = ReportForm(request.POST)
        context_url = (request.POST.get("context_url") or context_url)[:500]
        if form.is_valid():
            report = Report.objects.create(
                reporter=request.user,
                target_ct=ct,
                target_id=target.pk,
                target_repr=str(target)[:300],
                context_url=context_url,
                category=form.cleaned_data["category"],
                reason=form.cleaned_data["reason"],
            )
            try:
                events.content_report_filed(report)
            except Exception:
                logger.exception("content_report notification failed")
            messages.success(request, "Thanks. OSPREY staff will review this shortly.")
            if context_url:
                return redirect(context_url)
            return redirect("home")
    else:
        form = ReportForm()
    return render(
        request,
        "moderation/report_form.html",
        {
            "form": form,
            "target": target,
            "target_label": str(target),
            "context_url": context_url,
        },
    )


@staff_member_required
def report_queue(request):
    status = request.GET.get("status", Report.STATUS_OPEN)
    if status not in {s for s, _ in Report.STATUS_CHOICES}:
        status = Report.STATUS_OPEN
    reports = Report.objects.filter(status=status).select_related(
        "reporter", "target_ct"
    )
    open_count = Report.objects.filter(status=Report.STATUS_OPEN).count()
    return render(
        request,
        "moderation/queue.html",
        {
            "reports": reports,
            "status": status,
            "open_count": open_count,
            "recent_log": ModerationLog.objects.select_related("actor")[:20],
        },
    )


@staff_member_required
def report_detail(request, report_id: int):
    report = get_object_or_404(
        Report.objects.select_related("reporter", "target_ct"), pk=report_id
    )
    if request.method == "POST":
        action = (request.POST.get("action") or "").strip()
        note = (request.POST.get("review_note") or "").strip()
        if action in {"action", "dismiss"}:
            report.status = (
                Report.STATUS_ACTIONED
                if action == "action"
                else Report.STATUS_DISMISSED
            )
            report.reviewed_by = request.user
            report.reviewed_at = timezone.now()
            report.review_note = note
            report.save()
            log_action(
                request.user,
                "report_action" if action == "action" else "report_dismiss",
                target=report,
                reason=note,
            )
            messages.success(request, "Report updated.")
            return redirect("moderation:queue")
    return render(
        request,
        "moderation/report_detail.html",
        {"report": report, "target": report.target},
    )


# --- Project hide / unhide ---


@staff_member_required
@require_POST
def project_hide(request, project_id: int):
    from projects.models import Project

    project = get_object_or_404(Project, pk=project_id)
    reason = (request.POST.get("reason") or "").strip()
    project.is_staff_hidden = True
    project.save(update_fields=["is_staff_hidden", "updated_at"])
    log_action(request.user, "project_hide", target=project, reason=reason)
    try:
        events.project_hidden(project, hidden=True)
    except Exception:
        logger.exception("project_hidden notification failed")
    messages.success(request, f"Hidden '{project.title}'.")
    return redirect(request.POST.get("next") or "projects:list")


@staff_member_required
@require_POST
def project_unhide(request, project_id: int):
    from projects.models import Project

    project = get_object_or_404(Project, pk=project_id)
    reason = (request.POST.get("reason") or "").strip()
    project.is_staff_hidden = False
    project.save(update_fields=["is_staff_hidden", "updated_at"])
    log_action(request.user, "project_unhide", target=project, reason=reason)
    try:
        events.project_hidden(project, hidden=False)
    except Exception:
        logger.exception("project_hidden notification failed")
    messages.success(request, f"Restored '{project.title}'.")
    return redirect(request.POST.get("next") or "projects:list")


# --- User suspend / reinstate / purge ---


@staff_member_required
def user_suspend(request, user_id: int):
    target_user = get_object_or_404(User, pk=user_id)
    if request.method == "POST":
        if target_user.is_superuser:
            return HttpResponseForbidden("Cannot suspend a superuser from this view.")
        reason = (request.POST.get("reason") or "").strip()
        hide_content = request.POST.get("hide_content") == "1"
        target_user.is_active = False
        target_user.save(update_fields=["is_active"])
        log_action(request.user, "user_suspend", target=target_user, reason=reason)
        if hide_content:
            from projects.models import Project

            hidden = Project.objects.filter(
                created_by=target_user, is_staff_hidden=False
            )
            ids = list(hidden.values_list("pk", flat=True))
            hidden.update(is_staff_hidden=True)
            for pid in ids:
                log_action(
                    request.user,
                    "project_hide",
                    target=Project.objects.get(pk=pid),
                    reason=f"Auto-hidden with suspension of @{target_user.get_username()}",
                )
        messages.success(request, f"Suspended @{target_user.get_username()}.")
        return redirect("moderation:queue")
    return render(
        request,
        "moderation/user_suspend.html",
        {"target_user": target_user},
    )


@staff_member_required
@require_POST
def user_reinstate(request, user_id: int):
    target_user = get_object_or_404(User, pk=user_id)
    reason = (request.POST.get("reason") or "").strip()
    target_user.is_active = True
    target_user.save(update_fields=["is_active"])
    log_action(request.user, "user_reinstate", target=target_user, reason=reason)
    messages.success(request, f"Reinstated @{target_user.get_username()}.")
    return redirect("moderation:queue")


def _purge_user_content(target_user) -> dict[str, int]:
    """Hard-delete content authored solely by the user.

    Projects where the user is the sole verified contributor are deleted.
    Projects with other verified contributors keep, but the user's own
    Contribution row is removed. Wiki revisions, use reports, conversation
    posts, feedback, and reports filed by the user are deleted.
    """
    from conversations.models import ProjectReply, ProjectThread
    from feedback.models import Feedback
    from projects.models import Contribution, Project
    from use_reports.models import UseReport
    from wiki.models import WikiRevision

    counts: dict[str, int] = {}

    contributed_projects = Project.objects.filter(
        contributions__user=target_user
    ).distinct()
    solo_to_delete: set[int] = set()
    for project in contributed_projects:
        other_user_contrib = (
            project.contributions.filter(user__isnull=False)
            .exclude(user=target_user)
            .exists()
        )
        created_by_other = (
            project.created_by_id is not None
            and project.created_by_id != target_user.id
        )
        if not other_user_contrib and not created_by_other:
            solo_to_delete.add(project.pk)
    # Also include projects created by the user that have no other verified
    # contributors at all (they may have no Contribution rows yet).
    created_solo_ids = (
        Project.objects.filter(created_by=target_user)
        .exclude(contributions__user__isnull=False)
        .values_list("pk", flat=True)
    )
    solo_to_delete.update(created_solo_ids)

    counts["projects"] = len(solo_to_delete)
    if solo_to_delete:
        Project.objects.filter(pk__in=solo_to_delete).delete()

    counts["contributions"] = Contribution.objects.filter(user=target_user).delete()[0]
    counts["wiki_revisions"] = WikiRevision.objects.filter(author=target_user).delete()[
        0
    ]
    counts["use_reports"] = UseReport.objects.filter(author=target_user).delete()[0]
    counts["conversation_threads"] = ProjectThread.objects.filter(
        author=target_user
    ).delete()[0]
    counts["conversation_replies"] = ProjectReply.objects.filter(
        author=target_user
    ).delete()[0]
    counts["feedback"] = Feedback.objects.filter(user=target_user).delete()[0]
    counts["reports_filed"] = Report.objects.filter(reporter=target_user).delete()[0]
    return counts


@staff_member_required
def user_purge(request, user_id: int):
    target_user = get_object_or_404(User, pk=user_id)
    if target_user.is_superuser:
        return HttpResponseForbidden("Cannot purge a superuser.")
    if request.method == "POST":
        if (request.POST.get("confirm") or "").strip() != target_user.get_username():
            messages.error(
                request,
                "Confirmation text did not match the username. No content was deleted.",
            )
            return redirect("moderation:user_purge", user_id=target_user.pk)
        reason = (request.POST.get("reason") or "").strip()
        counts = _purge_user_content(target_user)
        log_action(
            request.user,
            "user_purge_content",
            target=target_user,
            reason=f"{reason} | counts={counts}",
        )
        messages.success(
            request,
            f"Purged content for @{target_user.get_username()}: "
            + ", ".join(f"{k}={v}" for k, v in counts.items() if v),
        )
        return redirect("moderation:queue")
    return render(
        request,
        "moderation/user_purge.html",
        {"target_user": target_user},
    )
