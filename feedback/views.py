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
_BROWSER_PATTERNS = [
    ("Edge", r"Edg/"),
    ("Chrome", r"Chrome/"),
    ("Firefox", r"Firefox/"),
    ("Safari", r"Safari/"),
    ("Vivaldi", r"Vivaldi/"),
    ("Opera", r"OPR/|Opera/"),
]
_OS_PATTERNS = [
    ("Windows", r"Windows NT"),
    ("macOS", r"Mac OS X|Macintosh"),
    ("iOS", r"iPhone|iPad|iPod"),
    ("Android", r"Android"),
    ("Linux", r"Linux"),
]


def _coarse_ua(ua: str) -> tuple[str, str]:
    browser = ""
    os_name = ""
    for name, pat in _BROWSER_PATTERNS:
        if re.search(pat, ua):
            browser = name
            break
    for name, pat in _OS_PATTERNS:
        if re.search(pat, ua):
            os_name = name
            break
    return browser, os_name


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


def _staff_required(user) -> bool:
    return user.is_authenticated and user.is_staff


@user_passes_test(_staff_required, login_url="/login/")
def review(request):
    if request.method == "POST":
        fb = get_object_or_404(Feedback, pk=request.POST.get("feedback_id"))
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
        fb.save(update_fields=["status", "admin_notes", "updated_at"])
        messages.success(request, "Submission updated.")
        return redirect("feedback:review")

    status = request.GET.get("status", "")
    category = request.GET.get("category", "")
    feedback = Feedback.objects.select_related("user").prefetch_related("replies")
    if status:
        feedback = feedback.filter(status=status)
    if category in dict(Feedback.CATEGORY_CHOICES):
        feedback = feedback.filter(category=category)
    return render(
        request,
        "feedback/review.html",
        {
            "feedback_items": feedback,
            "status_choices": Feedback.STATUS_CHOICES,
            "category_choices": Feedback.CATEGORY_CHOICES,
            "active_status": status,
            "active_category": category,
        },
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
