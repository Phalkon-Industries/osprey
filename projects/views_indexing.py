"""Staff page for indexed entries: paste, preview, import, review queue."""
from __future__ import annotations

import json
from datetime import date

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django_ratelimit.decorators import ratelimit
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .forms import COMMON_LICENSES
from .indexing import service
from .indexing.records import SourceError
from .models import Project

MAX_LINES = 50


def _lines(raw: str) -> list[str]:
    seen, out = set(), []
    for line in (raw or "").splitlines():
        line = line.strip()
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out[:MAX_LINES]


@staff_member_required
@require_http_methods(["GET", "POST"])
def index_staff(request):
    previews: list[service.Preview] = []
    pasted = ""
    if request.method == "POST":
        action = request.POST.get("action", "preview")
        if action == "preview":
            pasted = request.POST.get("lines", "")
            previews = [service.resolve(line, request.user) for line in _lines(pasted)]
        elif action == "import":
            lines = _lines(request.POST.get("lines", ""))
            chosen = set(request.POST.getlist("import"))
            results = []
            for line in lines:
                if line not in chosen:
                    continue
                preview = service.resolve(line, request.user)
                if not preview.can_import:
                    continue
                hold = True if request.POST.get(f"hold:{line}") else None
                project = service.index_record(preview.record, listed_by=None, hold=hold)
                results.append((preview, project))
            service.notify_staff_of_run(results, by=request.user)
            live = sum(1 for _, p in results if p.index_state == Project.INDEX_LIVE)
            held = sum(1 for _, p in results if p.index_state == Project.INDEX_HELD)
            messages.success(request, f"Indexed {live} live and {held} held.")
            return redirect(reverse("index_staff"))
        elif action == "fetch_new":
            source = request.POST.get("source", "")
            lister = service.LISTERS.get(source)
            if lister is None:
                messages.error(request, "That source can't be listed.")
                return redirect(reverse("index_staff"))
            last = (
                Project.objects.filter(source=source, published_on__isnull=False)
                .order_by("-published_on").values_list("published_on", flat=True).first()
            )
            since = last or date(2017, 1, 1)
            try:
                dois = lister(since)
            except SourceError as exc:
                messages.error(request, f"Couldn't list {source}: {exc}")
                return redirect(reverse("index_staff"))
            known = set(Project.objects.filter(source=source).values_list("external_id", flat=True))
            new_lines = [d for d in dois if d not in known][:MAX_LINES]
            if not new_lines:
                messages.info(request, f"Nothing new from {source} since {since}.")
                return redirect(reverse("index_staff"))
            pasted = "\n".join(new_lines)
            previews = [service.resolve(line, request.user) for line in new_lines]
            messages.info(request, f"{len(new_lines)} from {source} since {since}. Review and import below.")
        elif action == "recheck":
            project = get_object_or_404(Project, pk=request.POST.get("project_id"), origin=Project.ORIGIN_INDEXED, index_state=Project.INDEX_HELD)
            result = service.recheck_files_link(
                project,
                title=request.POST.get("title", "").strip(),
                summary=request.POST.get("summary", "").strip(),
                files_url=request.POST.get("files_url", "").strip(),
                by=request.user,
            )
            messages.info(request, f"“{project.title}”: {result}")
            return redirect(reverse("index_staff"))
        elif action == "approve":
            project = get_object_or_404(Project, pk=request.POST.get("project_id"), origin=Project.ORIGIN_INDEXED)
            service.approve(
                project,
                request.user,
                title=request.POST.get("title", "").strip(),
                license=request.POST.get("license", "").strip(),
                files_url=request.POST.get("files_url", "").strip(),
                summary=request.POST.get("summary", "").strip(),
            )
            messages.success(request, f"“{project.title}” is live.")
            return redirect(reverse("index_staff"))
        elif action == "decline":
            project = get_object_or_404(Project, pk=request.POST.get("project_id"), origin=Project.ORIGIN_INDEXED)
            title = project.title
            service.decline(project, request.user, request.POST.get("reason", ""))
            messages.success(request, f"Declined “{title}”.")
            return redirect(reverse("index_staff"))
    held_rows = list(
        Project.objects.filter(origin=Project.ORIGIN_INDEXED, index_state=Project.INDEX_HELD)
        .prefetch_related("contributions")
        .order_by("-indexed_at")
    )
    for row in held_rows:
        row.review_state = service.review_state(row)
    order = {"ready": 0, "confirm": 1, "failed": 2}
    held = sorted(held_rows, key=lambda r: order[r.review_state])
    held_counts = {k: sum(1 for r in held_rows if r.review_state == k) for k in order}
    recent = Project.objects.filter(origin=Project.ORIGIN_INDEXED, index_state=Project.INDEX_LIVE).order_by("-indexed_at")[:20]
    return render(
        request,
        "projects/index_staff.html",
        {
            "pasted": pasted,
            "previews": previews,
            "held": held,
            "held_counts": held_counts,
            "recent": recent,
            "licenses": COMMON_LICENSES,
            "max_lines": MAX_LINES,
            "listers": sorted(service.LISTERS),
        },
    )


def _public_lines(raw: str) -> list[str]:
    return _lines(raw)[: settings.INDEX_SUBMIT_MAX_LINES]


@login_required
@ratelimit(key="user", rate=lambda group, request: settings.RATELIMIT_INDEX_SUBMIT, method="POST", block=True)
@require_http_methods(["GET", "POST"])
def index_submit(request):
    """Signed-in users suggest work for the index, in two steps.

    Step one pastes lines and previews them. Step two shows one form per
    line: entries the adapters could read take a summary; sources OSPREY
    can't read yet need title, license and files link from the submitter.
    Same resolver and queue as the staff page.
    """
    rows: list[dict] = []
    pasted = ""
    if request.method == "POST":
        action = request.POST.get("action", "preview")
        lines = _public_lines(request.POST.get("lines", ""))
        pasted = "\n".join(lines)
        if action == "edit":
            # Back to step one with the lines still in the box, so one bad
            # line can be removed without retyping the rest.
            lines = []
        for i, line in enumerate(lines):
            preview = service.resolve(line, request.user)
            given = service.Submitted(
                title=request.POST.get(f"title_{i}", ""),
                summary=request.POST.get(f"summary_{i}", ""),
                license=request.POST.get(f"license_{i}", ""),
                files_url=request.POST.get(f"files_url_{i}", ""),
                authors=request.POST.get(f"authors_{i}", ""),
                image_url=request.POST.get(f"image_url_{i}", ""),
            ).clean()
            if action == "preview" and preview.record is not None:
                given.summary = preview.record.summary
                given.authors = ", ".join(a.name for a in preview.record.authors)
            authors_editable = service.authors_editable(preview.record)
            needs_fields = preview.outcome == service.UNSUPPORTED
            errors = []
            if action == "submit" and needs_fields:
                if not given.title:
                    errors.append("Title is required.")
                if not given.license:
                    errors.append("Pick a license.")
                if not given.files_url:
                    errors.append("Link the design files.")
            rows.append({"i": i, "preview": preview, "given": given, "needs_fields": needs_fields, "errors": errors, "authors_editable": authors_editable})
        if action == "submit" and not any(r["errors"] for r in rows):
            results = []
            for row in rows:
                preview, given = row["preview"], row["given"]
                if preview.can_import:
                    # Public submissions always wait for staff (decided
                    # 2026-09-29), whatever the gates said.
                    project = service.index_record(preview.record, listed_by=request.user, hold=True)
                    service.apply_submitted(project, given, request.user, record=preview.record)
                elif preview.outcome == service.UNSUPPORTED:
                    project = service.request_row(preview, listed_by=request.user, submitted=given)
                else:
                    continue
                results.append((preview, project))
            service.notify_staff_of_run(results, by=request.user, public=True)
            held = len(results)
            if held:
                messages.success(request, f"Sent {held} to staff for review. You'll hear back in your inbox.")
            else:
                messages.info(request, "Nothing new to add.")
            return redirect(reverse("projects:list"))
    return render(
        request,
        "projects/index_submit.html",
        {"pasted": pasted, "rows": rows, "max_lines": settings.INDEX_SUBMIT_MAX_LINES, "licenses": COMMON_LICENSES},
    )


@staff_member_required
def licenses_staff(request):
    """Open license findings across native and registered projects."""
    from .indexing import license_check

    if request.method == "POST" and request.POST.get("action") == "recheck":
        project = get_object_or_404(Project, pk=request.POST.get("project_id"))
        license_check.audit(project)
        messages.success(request, f"Re-checked “{project.title}”.")
        return redirect(reverse("licenses_staff"))
    rows = [p for p in Project.objects.exclude(license_check={}).select_related("created_by").order_by("-license_checked_at") if p.license_findings]
    unchecked = license_check.auditable().filter(license_checked_at__isnull=True).count()
    disagreements = _indexed_disagreements()
    if request.GET.get("export") == "disagreements":
        return _disagreements_csv(disagreements)
    return render(
        request,
        "projects/licenses_staff.html",
        {"rows": rows, "unchecked": unchecked, "disagreements": disagreements},
    )


def _indexed_disagreements() -> list[dict]:
    """Indexed entries whose declared license disagrees with the license on
    the record or repository that holds the files. Kept as a log for later
    outreach to the authors (decided 2026-09-29)."""
    import re

    out = []
    for p in Project.objects.filter(origin=Project.ORIGIN_INDEXED).exclude(gate_license={}).prefetch_related("contributions").order_by("published_on"):
        found = (p.gate_license or {}).get("found", "")
        m = re.search(r"declared (\S+) but the linked (\w+) says (\S+)", found)
        if not m:
            continue
        out.append({
            "project": p,
            "declared": m.group(1),
            "linked_source": m.group(2),
            "linked": m.group(3),
            "authors": [(c.display_name, c.orcid_id) for c in p.contributions.all()],
        })
    return out


def _disagreements_csv(disagreements):
    import csv

    from django.http import HttpResponse

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="license-disagreements.csv"'
    writer = csv.writer(response)
    writer.writerow(["source", "doi", "title", "published", "declared_license", "files_at", "files_license", "files_url", "osprey_state", "authors", "orcids"])
    for d in disagreements:
        p = d["project"]
        writer.writerow([
            p.source, p.doi, p.title, p.published_on or "", d["declared"], d["linked_source"], d["linked"], p.files_url, p.index_state,
            "; ".join(n for n, _ in d["authors"]), "; ".join(o for _, o in d["authors"] if o),
        ])
    return response
