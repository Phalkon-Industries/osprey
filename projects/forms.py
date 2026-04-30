from django import forms
from django.utils.text import slugify

from .models import Project


class ProjectForm(forms.ModelForm):
    """Logged-in user form for creating or editing a project.

    Distinct from the Django admin form: hides demo placeholders (DOI),
    excludes contributor and lineage management (those are post-create flows),
    and auto-suggests a slug from the title.
    """

    class Meta:
        model = Project
        fields = [
            "title",
            "slug",
            "summary",
            "description",
            "readme",
            "field",
            "artifact_type",
            "license",
            "canonical_url",
            "institution",
            "visibility",
        ]
        widgets = {
            "summary": forms.Textarea(attrs={"rows": 2}),
            "description": forms.Textarea(attrs={"rows": 4}),
            "readme": forms.Textarea(
                attrs={
                    "rows": 16,
                    "placeholder": "# Project README\n\nLong-form description, build notes, usage. Markdown is supported.",
                }
            ),
        }
        help_texts = {
            "slug": "URL-friendly identifier. Leave blank to auto-generate from the title.",
            "description": "Short blurb shown above the README. A paragraph or two.",
            "readme": "The main project page. Markdown: headings, lists, code, links, images.",
            "license": "SPDX identifier preferred (MIT, CERN-OHL-S-2.0, CC-BY-4.0, ...).",
            "canonical_url": "Where the artifact actually lives (GitHub, Codeberg, Zenodo).",
        }

    def clean_slug(self) -> str:
        slug = self.cleaned_data.get("slug") or ""
        if not slug:
            title = self.cleaned_data.get("title") or ""
            slug = slugify(title)[:120]
        if not slug:
            raise forms.ValidationError("Could not derive a slug from the title.")
        qs = Project.objects.filter(slug=slug)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError(
                "A project with this slug already exists. Pick a different one."
            )
        return slug
