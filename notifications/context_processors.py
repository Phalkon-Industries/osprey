def unread_notification_count(request):
    """Surface the unread notification count to the global template context."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"unread_notification_count": 0}
    try:
        count = user.notifications.filter(is_read=False).count()
    except Exception:
        count = 0
    return {"unread_notification_count": count}


def email_nudge(request):
    """Whether to show the add-your-email banner.

    Shown to signed-in users with no verified address who haven't opted
    out of email and haven't dismissed the banner this session; a fresh
    sign-in starts a fresh session, so the nudge returns each login.
    """
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"show_email_banner": False}
    session = getattr(request, "session", None)
    if session is not None and session.get("email_banner_dismissed"):
        return {"show_email_banner": False}
    try:
        from . import emails
        from .models import NotificationPreference

        if emails.verified_address_for(user):
            return {"show_email_banner": False}
        preference = NotificationPreference.for_user(user)
        return {"show_email_banner": preference.email_enabled}
    except Exception:
        return {"show_email_banner": False}
