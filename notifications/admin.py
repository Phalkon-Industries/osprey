from django import forms
from django.contrib import admin, messages
from django.utils import timezone

from .email import send_test_email
from .models import EmailSettings, Notification


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("user", "kind", "title", "is_read", "created_at")
    list_filter = ("kind", "is_read")
    search_fields = ("user__username", "title", "body")
    readonly_fields = ("created_at", "read_at")


class EmailSettingsForm(forms.ModelForm):
    class Meta:
        model = EmailSettings
        fields = "__all__"
        widgets = {
            # render_value keeps the stored secret visible to staff who
            # open the form; this is an admin-only page.
            "mailjet_secret_key": forms.PasswordInput(render_value=True),
        }


@admin.register(EmailSettings)
class EmailSettingsAdmin(admin.ModelAdmin):
    """Singleton editor for the email transport, with a test-send action."""

    form = EmailSettingsForm
    list_display = (
        "__str__",
        "enabled",
        "from_email",
        "last_test_at",
        "last_test_result",
    )
    readonly_fields = ("last_test_at", "last_test_result", "updated_at")
    actions = ["send_test_email_action"]

    def has_add_permission(self, request):
        # One row only. The changelist "add" button disappears once the
        # singleton exists; EmailSettings.load() creates it on first use.
        return not EmailSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Send a test email")
    def send_test_email_action(self, request, queryset):
        config = EmailSettings.load()
        to_address = config.test_recipient or config.from_email
        if not to_address:
            self.message_user(
                request,
                "Set a test recipient (or a from address) first.",
                level=messages.ERROR,
            )
            return
        try:
            send_test_email(to_address)
        except Exception as exc:  # noqa: BLE001 - surface any provider error
            config.last_test_result = f"failed: {exc}"[:300]
            self.message_user(
                request, f"Test email failed: {exc}", level=messages.ERROR
            )
        else:
            config.last_test_result = f"sent to {to_address}"
            self.message_user(
                request,
                f"Test email sent to {to_address}. If email is disabled or "
                "keys are missing it went to the server log instead.",
                level=messages.SUCCESS,
            )
        config.last_test_at = timezone.now()
        config.save(update_fields=["last_test_at", "last_test_result", "updated_at"])
