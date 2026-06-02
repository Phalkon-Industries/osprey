from django.contrib import admin

from .models import UseReport


@admin.register(UseReport)
class UseReportAdmin(admin.ModelAdmin):
    list_display = ("project", "author", "visibility", "created_at")
    list_filter = ("visibility",)
    search_fields = ("project__title", "author__username", "narrative")
    readonly_fields = ("created_at", "updated_at")
