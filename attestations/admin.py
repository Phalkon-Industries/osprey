from django.contrib import admin

from .models import Attestation


@admin.register(Attestation)
class AttestationAdmin(admin.ModelAdmin):
    list_display = ("project", "author", "endorsement", "visibility", "created_at")
    list_filter = ("endorsement", "visibility")
    search_fields = ("project__title", "author__username", "narrative")
    readonly_fields = ("created_at", "updated_at", "endorsed_at")
