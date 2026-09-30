"""Queued Zenodo work: publish, publish a new version, sync metadata.

The request records a `ZenodoJob` and returns immediately; the notifier
loop calls `run_due_jobs()` every tick. Jobs retry with backoff on
outages (timeouts, 5xx, dropped connections), fail fast on errors that a
retry cannot fix (4xx other than 429, missing configuration), and tell
the project owner how it ended. `ZENODO_JOBS_INLINE=1` runs each job
synchronously at enqueue time, for tests and the smoke seed.

Nothing here talks to Zenodo directly; the actual work lives in
`projects.zenodo` and is unchanged.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import claiming
from . import lineage as lineage_claims
from .models import Project, ProjectDeposit, ZenodoJob
from .zenodo import (
    ZenodoError,
    publish_new_version_now,
    publish_project_now,
    update_published_metadata,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
# Roughly eight hours of patience: 1m, 2m, 5m, 15m, 30m, 1h, 2h, 4h.
BACKOFF_SECONDS = [60, 120, 300, 900, 1800, 3600, 7200, 14400]
STALE_RUNNING_AFTER = timedelta(minutes=30)
_PERMANENT_HTTP = re.compile(r"HTTP 4\d\d")


# --- helpers shared with the views -----------------------------------------


def activate_lineage(project: Project) -> None:
    """Bring dormant lineage claims live after publish; never let it break
    the publish."""
    try:
        lineage_claims.activate_pending(project)
    except Exception:  # noqa: BLE001
        logger.exception("lineage activation failed for %s", project.slug)
    try:
        claiming.sweep_on_publish(project)
    except Exception:  # noqa: BLE001
        logger.exception("contributor invite sweep failed for %s", project.slug)


def notify_project_published(project: Project) -> None:
    """Tell staff and followers a project went public; never let it break
    the publish."""
    try:
        from notifications import events

        events.project_published(project)
    except Exception:  # noqa: BLE001
        logger.exception("project_published notification failed")


# --- enqueue ------------------------------------------------------------------


def _inline() -> bool:
    return bool(getattr(settings, "ZENODO_JOBS_INLINE", False))


def pending_job(project: Project, kind: str | None = None) -> ZenodoJob | None:
    qs = project.zenodo_jobs.filter(status__in=ZenodoJob.PENDING_STATUSES)
    if kind:
        qs = qs.filter(kind=kind)
    return qs.order_by("created_at").first()


def latest_job(project: Project) -> ZenodoJob | None:
    """The job the owner should hear about: a pending one, else the most
    recent failure, else nothing (done jobs are old news)."""
    job = pending_job(project)
    if job is not None:
        return job
    return project.zenodo_jobs.filter(status=ZenodoJob.STATUS_FAILED).order_by("-created_at").first()


def _create(project: Project, kind: str, user, payload: dict | None = None) -> ZenodoJob:
    job = ZenodoJob.objects.create(
        project=project,
        kind=kind,
        payload=payload or {},
        requested_by=user if getattr(user, "is_authenticated", False) else None,
    )
    if _inline():
        run_job(job)
    return job


def _require_native(project: Project) -> None:
    if not project.accepts_publish:
        raise ValueError("Registered projects are published and versioned on Zenodo by their authors.")


def enqueue_publish(project: Project, user) -> ZenodoJob:
    _require_native(project)
    existing = pending_job(project, ZenodoJob.KIND_PUBLISH)
    if existing is not None:
        return existing
    return _create(project, ZenodoJob.KIND_PUBLISH, user)


def enqueue_new_version(project: Project, user, *, changelog: str, repo_link: str = "") -> ZenodoJob:
    _require_native(project)
    existing = pending_job(project, ZenodoJob.KIND_NEW_VERSION)
    if existing is not None:
        return existing
    return _create(
        project,
        ZenodoJob.KIND_NEW_VERSION,
        user,
        {"changelog": changelog, "repo_link": repo_link or ""},
    )


def enqueue_metadata_sync(project: Project, user=None) -> ZenodoJob:
    """Coalesce: one queued sync per project is enough, since the job
    reads the project's current metadata when it runs."""
    existing = project.zenodo_jobs.filter(
        kind=ZenodoJob.KIND_METADATA_SYNC, status=ZenodoJob.STATUS_QUEUED
    ).first()
    if existing is not None:
        return existing
    return _create(project, ZenodoJob.KIND_METADATA_SYNC, user)


def retry(job: ZenodoJob) -> ZenodoJob:
    """Put a failed job back in the queue for an immediate attempt."""
    job.status = ZenodoJob.STATUS_QUEUED
    job.next_attempt_at = timezone.now()
    job.attempts = 0
    job.finished_at = None
    job.save(update_fields=["status", "next_attempt_at", "attempts", "finished_at", "updated_at"])
    if _inline():
        run_job(job)
    return job


# --- run ------------------------------------------------------------------------


def _requeue_stale_running() -> int:
    """A job stuck in `running` past the stale window means the loop died
    mid-job; hand it back to the queue without counting an attempt."""
    cutoff = timezone.now() - STALE_RUNNING_AFTER
    return ZenodoJob.objects.filter(
        status=ZenodoJob.STATUS_RUNNING, updated_at__lt=cutoff
    ).update(status=ZenodoJob.STATUS_QUEUED, next_attempt_at=timezone.now())


SLOW_AFTER_SECONDS = 600


def alert_slow_jobs() -> int:
    """Tell staff, once per job, about work that has waited more than ten
    minutes. The owner's page says staff has been notified; this is what
    makes that true. Returns how many alerts went out."""
    from django.urls import reverse

    cutoff = timezone.now() - timedelta(seconds=SLOW_AFTER_SECONDS)
    sent = 0
    for job in ZenodoJob.objects.filter(status__in=ZenodoJob.PENDING_STATUSES, created_at__lt=cutoff).select_related("project"):
        if job.payload.get("staff_alerted"):
            continue
        title = f"Zenodo job waiting {int((timezone.now() - job.created_at).total_seconds() // 60)} min: {job.project.title}"[:200]
        body = f"{job.get_kind_display()} for {job.project.title}, attempt {job.attempts}. Last error: {job.last_error[:300] or 'none yet'}"
        _notify(lambda events, t=title, b=body: [
            events._emit(u, kind="zenodo_job_slow", title=t, body=b, url=reverse("zenodo_jobs"), dedup_key=f"zenodo_job_slow:{job.pk}")
            for u in events._staff()
        ])
        job.payload = {**job.payload, "staff_alerted": True}
        job.save(update_fields=["payload", "updated_at"])
        sent += 1
    return sent


def run_due_jobs(limit: int = 20) -> int:
    """Claim due jobs under a row lock, then run them one at a time."""
    _requeue_stale_running()
    alert_slow_jobs()
    now = timezone.now()
    with transaction.atomic():
        ids = list(
            ZenodoJob.objects.select_for_update(skip_locked=True)
            .filter(status=ZenodoJob.STATUS_QUEUED, next_attempt_at__lte=now)
            .order_by("created_at")
            .values_list("id", flat=True)[:limit]
        )
        ZenodoJob.objects.filter(id__in=ids).update(status=ZenodoJob.STATUS_RUNNING, updated_at=now)
    for job_id in ids:
        try:
            run_job(ZenodoJob.objects.select_related("project", "requested_by").get(pk=job_id))
        except Exception as exc:  # noqa: BLE001 - a crash before run_job's own guard
            # (loading the job's project, for instance) must still land on the
            # job: attempts counted, retry scheduled, owner told after the last
            # one. Otherwise the job sits "running" and the loop repeats forever.
            logger.exception("Zenodo job %s crashed outside its runner", job_id)
            job = ZenodoJob.objects.filter(pk=job_id).first()
            if job is not None:
                job.attempts += 1
                job.save(update_fields=["attempts", "updated_at"])
                _on_failure(job, exc)
    return len(ids)


def run_job(job: ZenodoJob) -> ZenodoJob:
    job.status = ZenodoJob.STATUS_RUNNING
    job.attempts += 1
    job.save(update_fields=["status", "attempts", "updated_at"])
    try:
        _execute(job)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job
        _on_failure(job, exc)
        return job
    job.status = ZenodoJob.STATUS_DONE
    job.last_error = ""
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "last_error", "finished_at", "updated_at"])
    return job


def _execute(job: ZenodoJob) -> None:
    project = job.project
    user = job.requested_by
    if job.kind == ZenodoJob.KIND_PUBLISH:
        deposit = publish_project_now(project, user)
        Project.objects.filter(pk=project.pk).update(visibility=Project.VISIBILITY_PUBLIC)
        project.refresh_from_db()
        activate_lineage(project)
        notify_project_published(project)
        _notify(lambda events: events.deposit_published(project, deposit))
    elif job.kind == ZenodoJob.KIND_NEW_VERSION:
        deposit = project.deposits.filter(provider=ProjectDeposit.PROVIDER_ZENODO).first()
        if deposit is None:
            raise ZenodoError("This project has no Zenodo deposit to version.")
        publish_new_version_now(
            deposit,
            changelog=job.payload.get("changelog", ""),
            repo_link=job.payload.get("repo_link", ""),
            user=user,
        )
        activate_lineage(project)
        _notify(lambda events: events.new_version_published(project, actor=user))
        _notify(lambda events: events.deposit_published(project, deposit, new_version=True))
    elif job.kind == ZenodoJob.KIND_METADATA_SYNC:
        if project.is_registered:
            raise ZenodoError("HTTP 403: registered records are not edited by OSPREY.")
        update_published_metadata(project)
    else:  # pragma: no cover - guarded by model choices
        raise ZenodoError(f"Unknown Zenodo job kind {job.kind!r}.")


def _notify(fn) -> None:
    try:
        from notifications import events

        fn(events)
    except Exception:  # noqa: BLE001 - notifications never break a job
        logger.exception("notification after Zenodo job failed")


def _is_permanent(exc: Exception) -> bool:
    if not isinstance(exc, ZenodoError):
        return False
    message = str(exc)
    if "HTTP 429" in message:
        return False
    return bool(_PERMANENT_HTTP.search(message)) or "not configured" in message


def _on_failure(job: ZenodoJob, exc: Exception) -> None:
    job.last_error = str(exc)[:2000]
    if _is_permanent(exc) or job.attempts >= MAX_ATTEMPTS:
        job.status = ZenodoJob.STATUS_FAILED
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "last_error", "finished_at", "updated_at"])
        logger.warning("Zenodo job %s failed for good: %s", job.pk, job.last_error)
        if job.kind in (ZenodoJob.KIND_PUBLISH, ZenodoJob.KIND_NEW_VERSION):
            _notify(lambda events: events.deposit_failed(job.project, job))
        return
    delay = BACKOFF_SECONDS[min(job.attempts - 1, len(BACKOFF_SECONDS) - 1)]
    job.status = ZenodoJob.STATUS_QUEUED
    job.next_attempt_at = timezone.now() + timedelta(seconds=delay)
    job.save(update_fields=["status", "last_error", "next_attempt_at", "updated_at"])
    logger.info("Zenodo job %s attempt %s failed, retry in %ss: %s", job.pk, job.attempts, delay, job.last_error)


# --- status -----------------------------------------------------------------------


def health() -> dict:
    """For the staff dashboard: is Zenodo answering, and what's waiting."""
    last_done = ZenodoJob.objects.filter(status=ZenodoJob.STATUS_DONE).order_by("-finished_at").first()
    since = last_done.finished_at if last_done else None
    failing = ZenodoJob.objects.exclude(last_error="")
    if since:
        failing = failing.filter(updated_at__gt=since)
    return {
        "last_success_at": since,
        "recent_failures": failing.count(),
        "queued": ZenodoJob.objects.filter(status=ZenodoJob.STATUS_QUEUED).count(),
        "running": ZenodoJob.objects.filter(status=ZenodoJob.STATUS_RUNNING).count(),
        "failed": ZenodoJob.objects.filter(status=ZenodoJob.STATUS_FAILED).count(),
    }
