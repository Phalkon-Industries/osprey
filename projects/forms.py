"""Forms for project creation, editing, and user profile editing."""
from __future__ import annotations

import secrets

from django import forms
from django.utils.text import slugify

from .models import Project


# Suggested choices for field and project type. Free text is still allowed
# (the form widget is a datalist, not a strict select), but the suggestions
# nudge the data toward consistency.
FIELD_SUGGESTIONS = [
    "Oceanography",
    "Marine biology",
    "Atmospheric science",
    "Ecology",
    "Microbiology",
    "Chemistry",
    "Physics",
    "Electrical engineering",
    "Mechanical engineering",
    "Materials science",
    "Robotics",
    "Geology",
]

PROJECT_TYPE_SUGGESTIONS = [
    "Hardware",
    "Firmware / embedded software",
    "Software / library",
    "Dataset",
    "Protocol or method",
    "Documentation",
    "Analysis pipeline",
]


def _generate_slug(title: str) -> str:
    """Slug = slugified title plus a 4-hex-char suffix for uniqueness."""
    base = slugify(title)[:100] or "project"
    for _ in range(8):
        candidate = f"{base}-{secrets.token_hex(2)}"
        if not Project.objects.filter(slug=candidate).exists():
            return candidate
    # Extreme fallback: longer random suffix.
    return f"{base}-{secrets.token_hex(4)}"


class _DataListInput(forms.TextInput):
    """Text input with a datalist of suggestions (free text still allowed)."""

    def __init__(self, *args, suggestions=None, list_id=None, **kwargs):
        self._suggestions = suggestions or []
        self._list_id = list_id or "datalist"
        attrs = kwargs.pop("attrs", {}) or {}
        attrs["list"] = self._list_id
        super().__init__(*args, attrs=attrs, **kwargs)

    def render(self, name, value, attrs=None, renderer=None):
        html = super().render(name, value, attrs=attrs, renderer=renderer)
        options = "".join(f'<option value="{s}">' for s in self._suggestions)
        return f'{html}<datalist id="{self._list_id}">{options}</datalist>'


class ProjectForm(forms.ModelForm):
    """User-facing form for creating or editing a project.

    Slug is auto-generated. Visibility is controlled by Save Draft vs Publish
    buttons in the view, not by a form field. Description is dropped: the
    short summary plus the long README cover what the user needs.
    """

    class Meta:
        model = Project
        fields = [
            "title",
            "summary",
            "readme",
            "field",
            "artifact_type",
            "license",
            "canonical_url",
            "institution",
        ]
        widgets = {
            "summary": forms.Textarea(
                attrs={
                    "rows": 2,
                    "placeholder": "One-line description shown on cards and in search results.",
                }
            ),
            "readme": forms.Textarea(
                attrs={
                    "rows": 16,
                    "placeholder": "# Project README\n\nLong-form description, build notes, usage. Markdown is supported.",
                }
            ),
            "field": _DataListInput(
                suggestions=FIELD_SUGGESTIONS, list_id="field-suggestions"
            ),
            "artifact_type": _DataListInput(
                suggestions=PROJECT_TYPE_SUGGESTIONS, list_id="project-type-suggestions"
            ),
        }
        labels = {
            "summary": "Short description",
            "readme": "README",
            "artifact_type": "Project type",
            "canonical_url": "Where the files live",
            "license": "License",
        }
        help_texts = {
            "summary": "One sentence. Shown on project cards and in search results.",
            "readme": "The main project page. Headings, lists, code, links, and images.",
            "field": "Pick from the list or type your own.",
            "artifact_type": "Hardware, firmware, dataset, etc. Pick from the list or type your own.",
            "license": "SPDX identifier preferred (MIT, CERN-OHL-S-2.0, CC-BY-4.0, ...).",
            "canonical_url": "Link to the upstream repository or archive (GitHub, Codeberg, Zenodo).",
        }

    def save(self, commit: bool = True) -> Project:
        project = super().save(commit=False)
        if not project.slug:
            project.slug = _generate_slug(project.title)
        if commit:
            project.save()
            self.save_m2m()
        return project
