import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Notification

logger = logging.getLogger(__name__)


def _safe_next(request, fallback_url_name: str) -> str:
    """A POSTed next URL, but only if it stays on this host."""
    next_url = request.POST.get("next") or ""
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}
    ):
        return next_url
    return reverse(fallback_url_name)


@login_required
def inbox(request):
    qs = request.user.notifications.all()
    return render(
        request,
        "notifications/inbox.html",
        {
            "notifications": qs[:200],
            "unread_count": qs.filter(is_read=False).count(),
        },
    )


@login_required
def open_notification(request, pk):
    """Mark a notification as read and redirect to its URL."""
    notification = get_object_or_404(Notification, pk=pk, user=request.user)
    if not notification.is_read:
        notification.is_read = True
        notification.read_at = timezone.now()
        notification.save(update_fields=["is_read", "read_at"])
    return HttpResponseRedirect(notification.url or reverse("notifications:inbox"))


@login_required
@require_POST
def mark_all_read(request):
    request.user.notifications.filter(is_read=False).update(
        is_read=True, read_at=timezone.now()
    )
    return HttpResponseRedirect(reverse("notifications:inbox"))


@login_required
def menu(request):
    """HTML fragment behind the header bell: the latest notifications.

    Fetched lazily by the dropdown so pages don't pay for it unless the
    bell is opened.
    """
    qs = request.user.notifications.all()[:10]
    return render(
        request,
        "notifications/menu.html",
        {
            "notifications": qs,
            "unread_count": request.user.notifications.filter(is_read=False).count(),
        },
    )


@login_required
def notification_settings(request):
    """Email address, master switch, per-group cadence, thread auto-follow."""
    from allauth.account.models import EmailAddress

    from . import emails
    from .models import CADENCE_CHOICES, NotificationPreference, QueuedEmail

    preference = NotificationPreference.for_user(request.user)
    addresses = list(EmailAddress.objects.filter(user=request.user))

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "toggle_email":
            enable = bool(request.POST.get("email_enabled"))
            if enable and not preference.email_enabled:
                preference.email_enabled = True
                preference.save(update_fields=["email_enabled", "updated_at"])
                messages.success(
                    request,
                    "Email notifications are on. Add and confirm an email "
                    "address below to start receiving them.",
                )
            elif not enable and preference.email_enabled:
                # Disabling is a real opt-out: the address is deleted from
                # OSPREY, not just silenced, and queued mail is cancelled.
                preference.email_enabled = False
                preference.consented_at = None
                preference.consent_source = ""
                preference.save(
                    update_fields=[
                        "email_enabled",
                        "consented_at",
                        "consent_source",
                        "updated_at",
                    ]
                )
                EmailAddress.objects.filter(user=request.user).delete()
                QueuedEmail.objects.filter(
                    user=request.user, status=QueuedEmail.STATUS_QUEUED
                ).update(status=QueuedEmail.STATUS_CANCELLED)
                messages.warning(
                    request,
                    "Email notifications are off and your email address has "
                    "been removed from OSPREY. Activity will only appear in "
                    "your inbox here. To turn email back on, you'll need to "
                    "add and confirm an address again.",
                )
            return redirect("notifications:settings")
        if action == "add_email":
            if not preference.email_enabled:
                messages.error(
                    request, "Turn email notifications on before adding an address."
                )
                return redirect("notifications:settings")
            # Each add sends a confirmation email, so this is the one
            # settings action a hostile account could use to bomb an
            # address or burn the send quota. Per-user rate limit.
            from django.conf import settings as django_settings

            from django_ratelimit.core import is_ratelimited

            if django_settings.RATELIMIT_ENABLE and is_ratelimited(
                request,
                group="notifications.add_email",
                key="user",
                rate=django_settings.RATELIMIT_EMAIL_ADD,
                increment=True,
            ):
                messages.error(
                    request,
                    "Too many email changes in a short time. Try again later.",
                )
                return redirect("notifications:settings")
            email = (request.POST.get("email") or "").strip()
            if email:
                try:
                    EmailAddress.objects.add_email(
                        request, request.user, email, confirm=True
                    )
                except Exception:
                    logger.exception("could not add email address")
                    messages.error(
                        request, "That address could not be added. Check it and try again."
                    )
                else:
                    preference.consented_at = timezone.now()
                    preference.consent_source = (
                        request.POST.get("source") or "settings"
                    )
                    preference.save(
                        update_fields=[
                            "consented_at",
                            "consent_source",
                            "updated_at",
                        ]
                    )
                    messages.success(
                        request,
                        f"Confirmation sent to {email}. Click the link in it "
                        "to start receiving notifications.",
                    )
            return redirect(_safe_next(request, "notifications:settings"))
        if action == "remove_email":
            EmailAddress.objects.filter(user=request.user).delete()
            QueuedEmail.objects.filter(
                user=request.user, status=QueuedEmail.STATUS_QUEUED
            ).update(status=QueuedEmail.STATUS_CANCELLED)
            preference.consented_at = None
            preference.consent_source = ""
            preference.save(
                update_fields=["consented_at", "consent_source", "updated_at"]
            )
            messages.info(
                request,
                "Email address removed and queued mail cancelled. You will "
                "not receive any email notifications at all; activity will "
                "only appear in your inbox here.",
            )
            return redirect("notifications:settings")
        if action == "save_cadences":
            if not preference.email_enabled:
                return redirect("notifications:settings")
            valid = dict(CADENCE_CHOICES)
            for group in ("projects", "replies", "follows", "staff"):
                value = request.POST.get(group, "")
                if value in valid:
                    setattr(preference, group, value)
            account_value = request.POST.get("account", "")
            if account_value in ("off", "immediate"):
                preference.account = account_value
            preference.save()
            messages.success(request, "Email settings saved.")
            return redirect("notifications:settings")
        if action == "save_new_projects":
            # Lives outside the email form on purpose: this cadence also
            # gates the in-app rows, so it stays usable with email off.
            value = request.POST.get("new_projects", "")
            if value in dict(CADENCE_CHOICES):
                preference.new_projects = value
                preference.save(update_fields=["new_projects", "updated_at"])
                messages.success(request, "New project settings saved.")
            return redirect("notifications:settings")
        if action == "save_thread_prefs":
            preference.auto_follow_threads = bool(
                request.POST.get("auto_follow_threads")
            )
            preference.save(
                update_fields=["auto_follow_threads", "updated_at"]
            )
            messages.success(request, "Thread settings saved.")
            return redirect("notifications:settings")

    return render(
        request,
        "notifications/settings.html",
        {
            "preference": preference,
            "addresses": addresses,
            "verified_address": emails.verified_address_for(request.user),
            "cadence_choices": CADENCE_CHOICES,
        },
    )


def unsubscribe(request, token: str):
    """One-click unsubscribe from an email link. Works logged out."""
    from . import emails
    from .models import NotificationPreference, QueuedEmail

    data = emails.read_unsubscribe_token(token)
    if not data or data.get("s") not in emails.UNSUBSCRIBE_SCOPES:
        return render(
            request, "notifications/unsubscribe.html", {"invalid": True}
        )
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.filter(pk=data["u"]).first()
    if user is None:
        return render(
            request, "notifications/unsubscribe.html", {"invalid": True}
        )
    scope = data["s"]
    if request.method == "POST":
        preference = NotificationPreference.for_user(user)
        if scope == "all":
            from allauth.account.models import EmailAddress

            preference.email_enabled = False
            preference.consented_at = None
            preference.consent_source = ""
            preference.save(
                update_fields=[
                    "email_enabled",
                    "consented_at",
                    "consent_source",
                    "updated_at",
                ]
            )
            EmailAddress.objects.filter(user=user).delete()
            QueuedEmail.objects.filter(
                user=user, status=QueuedEmail.STATUS_QUEUED
            ).update(status=QueuedEmail.STATUS_CANCELLED)
        else:
            setattr(preference, scope, "off")
            preference.save(update_fields=[scope, "updated_at"])
        return render(
            request,
            "notifications/unsubscribe.html",
            {"done": True, "scope": scope},
        )
    return render(
        request,
        "notifications/unsubscribe.html",
        {"scope": scope, "token": token},
    )


@login_required
@require_POST
def dismiss_email_banner(request):
    """Hide the add-your-email banner for the rest of this session."""
    request.session["email_banner_dismissed"] = True
    return redirect(_safe_next(request, "notifications:inbox"))
