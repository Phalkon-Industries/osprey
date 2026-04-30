from django.contrib import admin

from .models import Institution, Profile


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ("name", "short_name")
    search_fields = ("name", "short_name")


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "display_name", "institution", "orcid_placeholder")
    list_select_related = ("user", "institution")
    search_fields = ("user__username", "display_name", "orcid_placeholder")
