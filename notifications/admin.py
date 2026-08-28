from django.conf import settings
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone

from .email import build_delivery_backend, send_test_email
from .models import EmailSettings, Notification


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
