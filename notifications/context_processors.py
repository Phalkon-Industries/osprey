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
