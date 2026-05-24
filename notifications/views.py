from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Notification


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
