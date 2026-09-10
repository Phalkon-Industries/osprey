"""Views for the suggestion box and the privacy request form.

Suggestions come from the floating widget; privacy requests come from the
form linked in the Privacy Policy. Staff review both in a small in-app
inbox, with the Django admin kept as the lower-level back office.
"""

from __future__ import annotations

import base64
import logging
import re

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.files.base import ContentFile
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Feedback, FeedbackReply

logger = logging.getLogger(__name__)

_MAX_MESSAGE_LEN = 4000
_MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024  # 4 MiB after base64 decode

# Coarse parsers. Good enough for triage; not a UA-parsing library.
# A capture group, when present, grabs the version. Order matters:
# Chromium derivatives carry a Chrome/ token, and everything carries
# Safari/, so the more specific patterns come first.
_BROWSER_PATTERNS = [
    ("Edge", r"Edg/([\d.]+)"),
    ("Vivaldi", r"Vivaldi/([\d.]+)"),
    ("Opera", r"OPR/([\d.]+)"),
    ("Opera", r"Opera/([\d.]+)"),
    ("Chrome", r"Chrome/([\d.]+)"),
    ("Firefox", r"Firefox/([\d.]+)"),
    ("Safari", r"Version/([\d.]+).*Safari/"),
    ("Safari", r"Safari/"),
]
# iOS before macOS: iPad UAs contain "like Mac OS X". The macOS version
# is frozen at 10_15_7 by modern browsers (it still separates genuinely
# old systems), and Windows NT 10.0 covers both Windows 10 and 11; the
# browser version is the reliable number.
_OS_PATTERNS = [
    ("iOS", r"(?:iPhone|iPad|iPod).*? OS ([\d_]+)"),
    ("iOS", r"iPhone|iPad|iPod"),
    ("macOS", r"Mac OS X ([\d_.]+)"),
    ("macOS", r"Macintosh"),
    ("Windows", r"Windows NT ([\d.]+)"),
    ("Android", r"Android ([\d.]+)"),
    ("Android", r"Android"),
    ("Linux", r"Linux"),
]


def _label_with_version(ua: str, patterns) -> str:
    for name, pat in patterns:
        match = re.search(pat, ua)
        if not match:
            continue
        version = match.group(1) if match.groups() else ""
        if version:
            version = ".".join(version.replace("_", ".").split(".")[:2])
            return f"{name} {version}"
        return name
    return ""


def _coarse_ua(ua: str) -> tuple[str, str]:
    return (
        _label_with_version(ua, _BROWSER_PATTERNS),
        _label_with_version(ua, _OS_PATTERNS),
    )


def _decode_data_url(data_url: str) -> bytes | None:
    """Turn a data:image/png;base64,... URL into raw bytes, or None."""
    if not data_url or not data_url.startswith("data:image/"):
        return None
    try:
        _, b64 = data_url.split(",", 1)
    except ValueError:
        return None
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None
    if len(raw) > _MAX_SCREENSHOT_BYTES:
        return None
    return raw


@login_required
@require_POST
def submit(request):
    message = (request.POST.get("message") or "").strip()
    if not message:
        return JsonResponse({"ok": False, "error": "Message is required."}, status=400)
    if len(message) > _MAX_MESSAGE_LEN:
        message = message[:_MAX_MESSAGE_LEN]

    page_url = (request.POST.get("page_url") or "")[:500]
    page_title = (request.POST.get("page_title") or "")[:200]
    user_agent = request.META.get("HTTP_USER_AGENT", "")[:400]
    browser, os_name = _coarse_ua(user_agent)

    def _to_int(name: str) -> int | None:
        v = request.POST.get(name)
        if not v:
            return None
        try:
            n = int(v)
        except ValueError:
            return None
        return n if 0 < n < 100000 else None

    fb = Feedback(
        user=request.user,
        message=message,
        page_url=page_url,
        page_title=page_title,
        user_agent=user_agent,
        browser=browser,
        os=os_name,
        viewport_w=_to_int("viewport_w"),
        viewport_h=_to_int("viewport_h"),
    )

    screenshot_data = request.POST.get("screenshot") or ""
    if screenshot_data:
        raw = _decode_data_url(screenshot_data)
        if raw is not None:
            fb.screenshot.save(
                "screenshot.png",
                ContentFile(raw),
                save=False,
            )

    fb.save()
    try:
        from notifications import events

        events.feedback_submitted(fb)
    except Exception:
        logger.exception("feedback_submitted notification failed")
    return JsonResponse({"ok": True, "id": fb.pk})


PRIVACY_REQUEST_TYPES = [
    ("delete_account", "Delete my account and personal data"),
    ("remove_content", "Remove a specific piece of content"),
    ("data_question", "A question about my data"),
]


@login_required
def privacy_request(request):
    """Purpose-built channel for data requests, linked from the Privacy Policy.

    Stores a Feedback row with category=privacy so it rides the same staff
    inbox and reply machinery as suggestions, just clearly flagged. Unlike
    the suggestion box, nothing about the browser or page is recorded.
    """
    if request.method == "POST":
        message = (request.POST.get("message") or "").strip()
        request_type = request.POST.get("request_type") or ""
        type_labels = dict(PRIVACY_REQUEST_TYPES)
        if request_type not in type_labels:
            request_type = ""
        if not message and not request_type:
            messages.error(request, "Please pick a request type or write a message.")
        else:
            label = type_labels.get(request_type, "Not specified")
            fb = Feedback.objects.create(
                user=request.user,
                category=Feedback.CATEGORY_PRIVACY,
                message=f"Request type: {label}\n\n{message[:_MAX_MESSAGE_LEN]}",
            )
            try:
                from notifications import events

                events.feedback_submitted(fb)
            except Exception:
                logger.exception("privacy request notification failed")
            messages.success(
                request,
                "Request received. Staff will reply here on OSPREY; watch "
                "your inbox.",
            )
            return redirect("feedback:mine")
    return render(
        request,
        "feedback/privacy_request.html",
        {"request_types": PRIVACY_REQUEST_TYPES},
    )


def _thread_rate_limited(request) -> bool:
    """Shared limit for user-side thread writes (replies and reopen
    requests)."""
    from django.conf import settings as django_settings
    from django_ratelimit.core import is_ratelimited

    if not django_settings.RATELIMIT_ENABLE:
        return False
    if is_ratelimited(
        request,
        group="feedback.user_thread",
        key="user",
        rate=django_settings.RATELIMIT_STAFF_THREAD_REPLY,
        increment=True,
    ):
        messages.error(
            request, "Too many messages in a short time. Try again later."
        )
        return True
    return False


@login_required
@require_POST
def user_reply(request, feedback_id):
    """A user replying on their own open thread."""
    fb = get_object_or_404(Feedback, pk=feedback_id, user=request.user)
    if fb.is_closed:
        messages.error(
            request, "This thread is closed. You can request to reopen it."
        )
        return redirect("feedback:mine")
    body = (request.POST.get("body") or "").strip()
    if not body:
        messages.error(request, "Write a message first.")
        return redirect("feedback:mine")
    if _thread_rate_limited(request):
        return redirect("feedback:mine")
    reply = FeedbackReply.objects.create(
        feedback=fb, author=request.user, body=body[:4000]
    )
    try:
        from notifications import events

        events.feedback_user_replied(reply)
    except Exception:
        logger.exception("feedback_user_replied notification failed")
    messages.success(request, "Reply sent.")
    return redirect("feedback:mine")


@login_required
@require_POST
def reopen_request(request, feedback_id):
    """A user asking staff to reopen their closed thread. One pending
    request at a time; staff reopen or dismiss it."""
    fb = get_object_or_404(Feedback, pk=feedback_id, user=request.user)
    if not fb.is_closed:
        messages.error(request, "This thread is already open.")
        return redirect("feedback:mine")
    if fb.reopen_requested:
        messages.info(request, "You already asked to reopen this thread.")
        return redirect("feedback:mine")
    if _thread_rate_limited(request):
        return redirect("feedback:mine")
    why = (request.POST.get("body") or "").strip()
    body = "Requested to reopen this thread."
    if why:
        body += f" Reason: {why}"
    FeedbackReply.objects.create(
        feedback=fb, author=request.user, body=body[:4000]
    )
    fb.reopen_requested = True
    fb.save(update_fields=["reopen_requested", "updated_at"])
    try:
        from notifications import events

        events.feedback_reopen_requested(fb, request.user)
    except Exception:
        logger.exception("feedback_reopen_requested notification failed")
    messages.success(request, "Reopen requested. Staff will take a look.")
    return redirect("feedback:mine")


def _staff_required(user) -> bool:
    return user.is_authenticated and user.is_staff


# The review page groups statuses into two piles: open work and the
# archive. Archiving is just setting a closed status.
OPEN_STATUSES = [Feedback.STATUS_NEW, Feedback.STATUS_TRIAGED]
ARCHIVED_STATUSES = [Feedback.STATUS_RESOLVED, Feedback.STATUS_WONTFIX]


@user_passes_test(_staff_required, login_url="/login/")
def review(request):
    if request.method == "POST":
        fb = get_object_or_404(Feedback, pk=request.POST.get("feedback_id"))
        if request.POST.get("action") == "dismiss_reopen":
            fb.reopen_requested = False
            fb.save(update_fields=["reopen_requested", "updated_at"])
            messages.success(
                request, "Reopen request dismissed; the thread stays closed."
            )
            return redirect(request.get_full_path())
        if request.POST.get("action") == "archive":
            fb.status = Feedback.STATUS_RESOLVED
            fb.save(update_fields=["status", "updated_at"])
            messages.success(request, "Archived.")
            return redirect(request.get_full_path())
        reply_body = (request.POST.get("reply_body") or "").strip()
        if reply_body:
            reply = FeedbackReply.objects.create(
                feedback=fb, author=request.user, body=reply_body[:4000]
            )
            try:
                from notifications import events

                events.feedback_replied(reply)
            except Exception:
                logger.exception("feedback_replied notification failed")
        status = request.POST.get("status") or Feedback.STATUS_NEW
        if status in dict(Feedback.STATUS_CHOICES):
            fb.status = status
        fb.admin_notes = request.POST.get("admin_notes", "")
        update_fields = ["status", "admin_notes", "updated_at"]
        if fb.status in OPEN_STATUSES and fb.reopen_requested:
            fb.reopen_requested = False
            update_fields.append("reopen_requested")
        fb.save(update_fields=update_fields)
        messages.success(request, "Submission updated.")
        return redirect(request.get_full_path())

    tab = request.GET.get("tab", "open")
    if tab not in ("open", "archived", "all"):
        tab = "open"
    category = request.GET.get("category", "")
    feedback = Feedback.objects.select_related("user").prefetch_related("replies")
    if category in dict(Feedback.CATEGORY_CHOICES):
        feedback = feedback.filter(category=category)
    show_reopen = request.GET.get("reopen") == "1"
    if show_reopen:
        feedback = feedback.filter(reopen_requested=True)
        tab = "all"
    open_count = feedback.filter(status__in=OPEN_STATUSES).count()
    archived_count = feedback.filter(status__in=ARCHIVED_STATUSES).count()
    if tab == "open":
        feedback = feedback.filter(status__in=OPEN_STATUSES)
    elif tab == "archived":
        feedback = feedback.filter(status__in=ARCHIVED_STATUSES)
    return render(
        request,
        "feedback/review.html",
        {
            "feedback_items": feedback,
            "status_choices": Feedback.STATUS_CHOICES,
            "category_choices": Feedback.CATEGORY_CHOICES,
            "active_tab": tab,
            "active_category": category,
            "show_reopen": show_reopen,
            "reopen_count": Feedback.objects.filter(
                reopen_requested=True
            ).count(),
            "open_count": open_count,
            "archived_count": archived_count,
        },
    )


@user_passes_test(_staff_required, login_url="/login/")
def compose(request):
    """Staff opening a message thread with a user (outreach)."""
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip().lstrip("@")
        body = (request.POST.get("message") or "").strip()
        target = (
            get_user_model()
            .objects.filter(username=username, is_active=True)
            .first()
        )
        if target is None:
            messages.error(request, f"No active user named “{username}”.")
        elif not body:
            messages.error(request, "Write a message.")
        else:
            fb = Feedback.objects.create(
                user=target,
                category=Feedback.CATEGORY_OUTREACH,
                message=body[:4000],
                opened_by=request.user,
                status=Feedback.STATUS_TRIAGED,
            )
            try:
                from notifications import events

                events.staff_message_received(fb)
            except Exception:
                logger.exception("staff_message_received notification failed")
            messages.success(
                request, f"Message sent to @{target.get_username()}."
            )
            return redirect("feedback:review")
    return render(
        request,
        "feedback/compose.html",
        {"username": request.GET.get("to", "")},
    )


@login_required
def mine(request):
    """List the current user's feedback submissions, with staff replies."""
    items = (
        Feedback.objects.filter(user=request.user)
        .prefetch_related("replies__author")
        .order_by("-created_at")
    )
    return render(request, "feedback/mine.html", {"feedback_items": items})
