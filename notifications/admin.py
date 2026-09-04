from django.conf import settings
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone

from .email import build_delivery_backend, send_test_email
from .models import Announcement, EmailSettings, Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "kind", "title", "is_read", "created_at")
    list_filter = ("kind", "is_read")
    search_fields = ("user__username", "title", "body")
    readonly_fields = ("created_at", "read_at")


@admin.register(EmailSettings)
class EmailSettingsAdmin(admin.ModelAdmin):
    """Singleton editor for the email transport.

    Clicking "Email settings" in the admin index lands directly on the
    edit form (no changelist detour), and the form carries a "save and
    send test email" button so the whole transport can be verified from
    the panel.
    """

    fields = (
        "enabled",
        "from_email",
        "test_recipient",
        "delivery_provider_status",
        "last_test_at",
        "last_test_result",
        "updated_at",
    )
    readonly_fields = (
        "delivery_provider_status",
        "last_test_at",
        "last_test_result",
        "updated_at",
    )
    change_form_template = "admin/notifications/emailsettings/change_form.html"

    @admin.display(description="Delivery provider")
    def delivery_provider_status(self, obj):
        # Secrets never live in the database; this only reports whether
        # the env-configured provider backend is ready to construct.
        path = settings.EMAIL_DELIVERY_BACKEND
        try:
            build_delivery_backend(fail_silently=True)
        except Exception as exc:  # noqa: BLE001 - any init failure means
            # the provider isn't configured yet; report it verbatim.
            return (
                f"{path} — not ready: {exc} Set its credentials in the "
                "server's env file (/etc/osprey/.env.prod in production) "
                "and restart."
            )
        return f"{path} — ready"

    def changelist_view(self, request, extra_context=None):
        config = EmailSettings.load()
        return HttpResponseRedirect(
            reverse("admin:notifications_emailsettings_change", args=[config.pk])
        )

    def has_add_permission(self, request):
        # load() creates the singleton; a second row must not exist.
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def response_change(self, request, obj):
        if "_send_test" in request.POST:
            to_address = obj.test_recipient or obj.from_email
            if not to_address:
                self.message_user(
                    request,
                    "Set a test recipient (or a from address) first.",
                    level=messages.ERROR,
                )
                return HttpResponseRedirect(request.path)
            try:
                mode = send_test_email(to_address)
            except Exception as exc:  # noqa: BLE001 - surface any provider error
                obj.last_test_result = f"failed: {exc}"[:300]
                self.message_user(
                    request, f"Test email failed: {exc}", level=messages.ERROR
                )
            else:
                if mode == "provider":
                    obj.last_test_result = f"sent to {to_address}"
                    self.message_user(
                        request,
                        f"Test email sent to {to_address} and accepted by "
                        "the delivery provider.",
                        level=messages.SUCCESS,
                    )
                else:
                    reason = (
                        "email is disabled"
                        if not obj.enabled
                        else "the delivery provider is not configured"
                    )
                    obj.last_test_result = (
                        f"went to server log ({reason}); nothing was "
                        f"delivered to {to_address}"
                    )[:300]
                    self.message_user(
                        request,
                        f"Not delivered: {reason}, so the test message went "
                        f"to the server log instead of {to_address}.",
                        level=messages.WARNING,
                    )
            obj.last_test_at = timezone.now()
            obj.save(
                update_fields=["last_test_at", "last_test_result", "updated_at"]
            )
            return HttpResponseRedirect(request.path)
        return super().response_change(request, obj)


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    """Draft, test, and broadcast service announcements.

    The form carries two extra buttons: "Save and send test to me"
    delivers the announcement to just the signed-in admin (in-app plus
    email, same path as the real send), and "Send to all users" does the
    broadcast once. A sent announcement is frozen; duplicate it to send
    a follow-up.
    """

    list_display = ("subject", "created_by", "created_at", "sent_at", "sent_count")
    readonly_fields = (
        "created_by",
        "created_at",
        "test_sent_at",
        "sent_at",
        "sent_count",
        "emailed_count",
    )
    fields = (
        "subject",
        "body",
        "url",
        "created_by",
        "created_at",
        "test_sent_at",
        "sent_at",
        "sent_count",
        "emailed_count",
    )

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.is_sent:
            return self.readonly_fields + ("subject", "body", "url")
        return self.readonly_fields

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def response_change(self, request, obj):
        from .announcements import send_announcement

        if "_send_test" in request.POST:
            delivered, emailed = send_announcement(
                obj, recipients=[request.user]
            )
            obj.test_sent_at = timezone.now()
            obj.save(update_fields=["test_sent_at"])
            self.message_user(
                request,
                f"Test sent to you: {delivered} in-app notification, "
                f"{emailed} email queued"
                + (
                    "."
                    if emailed
                    else " (no email: address missing or email disabled)."
                ),
                level=messages.SUCCESS,
            )
            return HttpResponseRedirect(request.path)
        if "_send_all" in request.POST:
            if obj.is_sent:
                self.message_user(
                    request,
                    "Already sent. Duplicate the announcement to send again.",
                    level=messages.ERROR,
                )
                return HttpResponseRedirect(request.path)
            delivered, emailed = send_announcement(obj)
            obj.sent_at = timezone.now()
            obj.sent_count = delivered
            obj.emailed_count = emailed
            obj.save(update_fields=["sent_at", "sent_count", "emailed_count"])
            self.message_user(
                request,
                f"Announcement sent: {delivered} users notified in-app, "
                f"{emailed} emails queued (delivery within about a minute "
                "via the notifier loop).",
                level=messages.SUCCESS,
            )
            return HttpResponseRedirect(request.path)
        return super().response_change(request, obj)
