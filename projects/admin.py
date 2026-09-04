from django.contrib import admin

from .models import (
    ArtifactLink,
    Contribution,
    LineageEdge,
    Project,
    ProjectDeposit,
    ProjectDepositVersion,
    ProjectImage,
    Tag,
    TagAssignment,
)


@admin.register(LineageEdge)
class LineageEdgeAdmin(admin.ModelAdmin):
    list_display = (
        "declared_at",
        "child",
        "relation",
        "parent",
        "status",
        "claimed_at",
        "declared_by",
    )
    list_filter = ("status", "relation")
    search_fields = (
        "parent__title",
        "parent__slug",
        "child__title",
        "child__slug",
        "declared_by__username",
    )
    list_select_related = ("parent", "child", "declared_by")
    readonly_fields = ("declared_at", "claimed_at", "responded_at")
    autocomplete_fields = ("parent", "child")


class ContributionInline(admin.TabularInline):
    model = Contribution
    extra = 1
    autocomplete_fields = ("user",)
    readonly_fields = ("orcid_id",)
    fields = ("user", "orcid_id", "display_name", "role", "credit_statement", "order")


class ArtifactLinkInline(admin.TabularInline):
    model = ArtifactLink
    extra = 1


class TagAssignmentInline(admin.TabularInline):
    model = TagAssignment
    extra = 1
    autocomplete_fields = ("tag",)


class ProjectImageInline(admin.TabularInline):
    model = ProjectImage
    extra = 1
    fields = ("image", "caption", "order")


class ProjectDepositInline(admin.TabularInline):
    model = ProjectDeposit
    extra = 0
    readonly_fields = (
        "provider",
        "sandbox",
        "deposition_id",
        "record_id",
        "concept_id",
        "doi",
        "concept_doi",
        "state",
        "updated_at",
        "published_at",
    )
    fields = readonly_fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "slug",
        "visibility",
        "is_staff_hidden",
        "field",
        "artifact_type",
        "institution",
        "updated_at",
    )
    list_filter = (
        "visibility",
        "is_staff_hidden",
        "field",
        "artifact_type",
        "institution",
    )
    search_fields = ("title", "slug", "summary", "description", "readme")
    actions = ["staff_hide_selected", "staff_unhide_selected"]
    prepopulated_fields = {"slug": ("title",)}
    inlines = [
        ContributionInline,
        ArtifactLinkInline,
        TagAssignmentInline,
        ProjectImageInline,
        ProjectDepositInline,
    ]

    @admin.action(description="Staff-hide selected projects")
    def staff_hide_selected(self, request, queryset):
        from moderation.helpers import log_action

        count = 0
        for project in queryset:
            if project.is_staff_hidden:
                continue
            project.is_staff_hidden = True
            project.save(update_fields=["is_staff_hidden", "updated_at"])
            log_action(
                request.user, "project_hide", target=project, reason="Bulk admin action"
            )
            count += 1
        self.message_user(request, f"Hid {count} project(s).")

    @admin.action(description="Restore selected projects (un-hide)")
    def staff_unhide_selected(self, request, queryset):
        from moderation.helpers import log_action

        count = 0
        for project in queryset:
            if not project.is_staff_hidden:
                continue
            project.is_staff_hidden = False
            project.save(update_fields=["is_staff_hidden", "updated_at"])
            log_action(
                request.user,
                "project_unhide",
                target=project,
                reason="Bulk admin action",
            )
            count += 1
        self.message_user(request, f"Restored {count} project(s).")


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


admin.site.register(ArtifactLink)
admin.site.register(ProjectImage)


@admin.register(Contribution)
class ContributionAdmin(admin.ModelAdmin):
    list_display = ("display_name", "project", "role", "user", "orcid_id")
    list_filter = ("role",)
    search_fields = ("display_name", "role", "credit_statement", "orcid_id")
    autocomplete_fields = ("project", "user")
    readonly_fields = ("orcid_id",)
    fields = (
        "project",
        "user",
        "orcid_id",
        "display_name",
        "role",
        "credit_statement",
        "order",
    )


@admin.register(ProjectDeposit)
class ProjectDepositAdmin(admin.ModelAdmin):
    list_display = (
        "project",
        "provider",
        "sandbox",
        "state",
        "doi",
        "record_id",
        "updated_at",
    )
    list_filter = ("provider", "sandbox", "state")
    search_fields = (
        "project__title",
        "project__slug",
        "deposition_id",
        "record_id",
        "doi",
    )
    readonly_fields = (
        "project",
        "provider",
        "sandbox",
        "deposition_id",
        "bucket_url",
        "record_id",
        "concept_id",
        "doi",
        "concept_doi",
        "state",
        "pending_changelog",
        "repo_link",
        "created_by",
        "last_response",
        "last_error",
        "created_at",
        "updated_at",
        "published_at",
    )


@admin.register(ProjectDepositVersion)
class ProjectDepositVersionAdmin(admin.ModelAdmin):
    list_display = ("deposit", "version_index", "doi", "published_at")
    list_filter = ("deposit__provider", "deposit__sandbox")
    search_fields = (
        "deposit__project__title",
        "deposit__project__slug",
        "deposition_id",
        "record_id",
        "doi",
    )
    readonly_fields = (
        "deposit",
        "version_index",
        "deposition_id",
        "record_id",
        "doi",
        "changelog",
        "repo_link",
        "published_at",
        "last_response",
    )
