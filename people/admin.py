from django.contrib import admin, messages
from django.utils.html import format_html
from django.urls import reverse

from moderation.helpers import log_action

from .models import Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "display_name",
        "institution",
        "orcid_placeholder",
        "is_active_display",
        "moderation_links",
    )
    list_select_related = ("user",)
    list_filter = ("user__is_active",)
    search_fields = (
        "user__username",
        "display_name",
        "institution",
        "orcid_placeholder",
    )
    readonly_fields = ("orcid_placeholder", "suspended_at")
    actions = ["suspend_selected", "reinstate_selected"]

    @admin.display(boolean=True, description="Active")
    def is_active_display(self, obj):
        return obj.user.is_active

    @admin.display(description="Moderation")
    def moderation_links(self, obj):
        suspend_url = reverse("moderation:user_suspend", args=[obj.user_id])
        purge_url = reverse("moderation:user_purge", args=[obj.user_id])
        return format_html(
            '<a href="{}">suspend</a> | <a href="{}">purge</a>',
            suspend_url,
            purge_url,
        )

    @admin.action(description="Suspend selected users")
    def suspend_selected(self, request, queryset):
        from django.utils import timezone

        count = 0
        for profile in queryset.select_related("user"):
            if profile.user.is_superuser:
                continue
            profile.user.is_active = False
            profile.user.save(update_fields=["is_active"])
            profile.suspended_at = timezone.now()
            profile.save(update_fields=["suspended_at"])
            log_action(
                request.user,
                "user_suspend",
                target=profile.user,
                reason="Bulk admin action",
            )
            count += 1
        self.message_user(
            request, f"Suspended {count} account(s).", level=messages.SUCCESS
        )

    @admin.action(description="Reinstate selected users")
    def reinstate_selected(self, request, queryset):
        count = 0
        for profile in queryset.select_related("user"):
            profile.user.is_active = True
            profile.user.save(update_fields=["is_active"])
            profile.suspended_at = None
            profile.save(update_fields=["suspended_at"])
            log_action(
                request.user,
                "user_reinstate",
                target=profile.user,
                reason="Bulk admin action",
            )
            count += 1
        self.message_user(
            request, f"Reinstated {count} account(s).", level=messages.SUCCESS
        )
