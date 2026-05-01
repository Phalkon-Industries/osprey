from django.contrib import admin
from django.utils.html import format_html

from .models import Feedback


@admin.register(Feedback)
class FeedbackAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "status",
        "user",
        "message_preview",
        "page_url_short",
        "browser",
        "os",
        "has_screenshot",
    )
    list_filter = ("status", "browser", "os")
    search_fields = ("message", "page_url", "user__username", "admin_notes")
    list_select_related = ("user",)
    readonly_fields = (
        "user",
        "page_url",
        "page_title",
        "user_agent",
        "browser",
        "os",
        "viewport_w",
        "viewport_h",
        "screenshot_preview",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (None, {"fields": ("status", "admin_notes")}),
        ("Submission", {"fields": ("user", "message", "page_url", "page_title")}),
        (
            "Environment",
            {
                "fields": (
                    "browser",
                    "os",
                    "viewport_w",
                    "viewport_h",
                    "user_agent",
                ),
            },
        ),
        ("Screenshot", {"fields": ("screenshot_preview", "screenshot")}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(description="Message")
    def message_preview(self, obj):
        text = (obj.message or "").strip().replace("\n", " ")
        return text[:80] + ("…" if len(text) > 80 else "")

    @admin.display(description="Page")
    def page_url_short(self, obj):
        url = obj.page_url or ""
        if len(url) > 60:
            url = url[:57] + "…"
        return url

    @admin.display(boolean=True, description="Shot")
    def has_screenshot(self, obj):
        return bool(obj.screenshot)

    @admin.display(description="Screenshot preview")
    def screenshot_preview(self, obj):
        if not obj.screenshot:
            return "—"
        return format_html(
            '<a href="{0}" target="_blank" rel="noopener">'
            '<img src="{0}" style="max-width:480px;max-height:360px;'
            "border:1px solid #ccc;border-radius:4px\"></a>",
            obj.screenshot.url,
        )
