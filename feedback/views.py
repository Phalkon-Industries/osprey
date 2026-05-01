"""Views for the feedback widget.

Submissions come from the floating widget. Staff can review them in a small
in-app inbox, with the Django admin kept as the lower-level back office.
"""
from __future__ import annotations

import base64
import re

from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.files.base import ContentFile
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .models import Feedback


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
    return JsonResponse({"ok": True, "id": fb.pk})


def _staff_required(user) -> bool:
    return user.is_authenticated and user.is_staff


@user_passes_test(_staff_required, login_url="/login/")
def review(request):
    if request.method == "POST":
        fb = get_object_or_404(Feedback, pk=request.POST.get("feedback_id"))
        status = request.POST.get("status") or Feedback.STATUS_NEW
        if status in dict(Feedback.STATUS_CHOICES):
            fb.status = status
        fb.admin_notes = request.POST.get("admin_notes", "")
        fb.save(update_fields=["status", "admin_notes", "updated_at"])
        messages.success(request, "Feedback updated.")
        return redirect("feedback:review")

    status = request.GET.get("status", "")
    feedback = Feedback.objects.select_related("user")
    if status:
        feedback = feedback.filter(status=status)
    return render(
        request,
        "feedback/review.html",
        {
            "feedback_items": feedback,
            "status_choices": Feedback.STATUS_CHOICES,
            "active_status": status,
        },
    )
