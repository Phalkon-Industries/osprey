from django.contrib import admin

from .models import Profile


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "display_name", "institution", "orcid_placeholder")
    list_select_related = ("user",)
    search_fields = ("user__username", "display_name", "institution", "orcid_placeholder")
