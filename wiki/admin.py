from django.contrib import admin

from .models import WikiPage, WikiRevision


@admin.register(WikiPage)
class WikiPageAdmin(admin.ModelAdmin):
    list_display = ("project", "title", "slug", "is_landing", "updated_at")
    list_filter = ("is_landing",)
    search_fields = ("title", "slug", "project__title")


@admin.register(WikiRevision)
class WikiRevisionAdmin(admin.ModelAdmin):
    list_display = ("page", "author", "status", "created_at")
    list_filter = ("status",)
    search_fields = ("title", "summary", "page__title", "author__username")
