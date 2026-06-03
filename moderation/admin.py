from django.contrib import admin

from .models import ModerationLog, Report


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ("created_at", "status", "category", "target_repr", "reporter")
    list_filter = ("status", "category")
    search_fields = ("target_repr", "reason", "reporter__username")
    readonly_fields = (
        "reporter",
        "target_ct",
        "target_id",
        "target_repr",
        "category",
        "reason",
        "created_at",
    )


@admin.register(ModerationLog)
class ModerationLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "actor", "target_repr", "reason_short")
    list_filter = ("action",)
    search_fields = ("actor__username", "target_repr", "reason")
    readonly_fields = (
        "actor",
        "action",
        "target_ct",
        "target_id",
        "target_repr",
        "reason",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Reason")
    def reason_short(self, obj):
        return (obj.reason or "")[:80]
