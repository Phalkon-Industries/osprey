from django.contrib import admin

from .models import (
    ArtifactLink,
    Citation,
    Contribution,
    LineageEdge,
    Project,
    ProjectImage,
    Tag,
    TagAssignment,
)


class ContributionInline(admin.TabularInline):
    model = Contribution
    extra = 1
    autocomplete_fields = ("user",)


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


class CitationInline(admin.TabularInline):
    model = Citation
    extra = 1
    fields = ("text", "url", "doi", "year", "order")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "slug",
        "visibility",
        "field",
        "artifact_type",
        "institution",
        "updated_at",
    )
    list_filter = ("visibility", "field", "artifact_type", "institution")
    search_fields = ("title", "slug", "summary", "description", "readme")
    prepopulated_fields = {"slug": ("title",)}
    inlines = [
        ContributionInline,
        ArtifactLinkInline,
        TagAssignmentInline,
        ProjectImageInline,
        CitationInline,
    ]


@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(LineageEdge)
class LineageEdgeAdmin(admin.ModelAdmin):
    list_display = ("child", "relation", "parent")
    list_filter = ("relation",)
    autocomplete_fields = ("parent", "child")


admin.site.register(ArtifactLink)
admin.site.register(Contribution)
admin.site.register(ProjectImage)
admin.site.register(Citation)
