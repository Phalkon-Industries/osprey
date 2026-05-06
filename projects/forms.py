"""Forms for project creation and editing.

Profile editing lives in people/forms.py.
"""
from __future__ import annotations

import re
import secrets

from django import forms
from django.forms import inlineformset_factory
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.utils.text import slugify

from .models import Contribution, Project, Tag


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

ROLE_SUGGESTIONS = [
    "Project lead",
    "Principal investigator",
    "Maintainer",
    "Hardware design",
    "Firmware",
    "Software",
    "Field testing",
    "Data collection",
    "Fabrication",
    "Documentation",
    "Analysis",
]


def _examples_text(items: list[str], n: int = 4) -> str:
    """Format a short 'Examples: a, b, c' helper string."""
    return "Examples: " + ", ".join(items[:n]) + ". Free text."


# Common open-source licenses for OSPREY projects, split into a short
# recommended list (handles most cases) and a longer list of other
# options. OSPREY is open-source-only; proprietary licenses are not
# offered. Users can also pick "Other" and type a custom OSI / OSHWA /
# Creative Commons identifier.
RECOMMENDED_LICENSES = [
    ("MIT", "MIT — permissive code"),
    ("Apache-2.0", "Apache 2.0 — permissive code, with patent grant"),
    ("GPL-3.0-or-later", "GPL 3.0 or later — copyleft code"),
    ("CERN-OHL-S-2.0", "CERN-OHL-S 2.0 — reciprocal open hardware"),
    ("CC-BY-4.0", "CC BY 4.0 — docs / data, attribution"),
]

OTHER_LICENSES = [
    ("BSD-3-Clause", "BSD 3-Clause — permissive code"),
    ("LGPL-3.0-or-later", "LGPL 3.0 or later — weak copyleft library"),
    ("MPL-2.0", "MPL 2.0 — file-level copyleft code"),
    ("CERN-OHL-W-2.0", "CERN-OHL-W 2.0 — weakly reciprocal open hardware"),
    ("CERN-OHL-P-2.0", "CERN-OHL-P 2.0 — permissive open hardware"),
    ("TAPR-OHL-1.0", "TAPR Open Hardware License 1.0"),
    ("CC-BY-SA-4.0", "CC BY-SA 4.0 — docs / data, share-alike"),
    ("CC0-1.0", "CC0 1.0 — public domain dedication"),
    ("Unlicense", "The Unlicense — public domain dedication for code"),
]

COMMON_LICENSES = RECOMMENDED_LICENSES + OTHER_LICENSES


def _generate_slug(title: str) -> str:
    """Slug = slugified title plus a 4-hex-char suffix for uniqueness."""
    base = slugify(title)[:100] or "project"
    for _ in range(8):
        candidate = f"{base}-{secrets.token_hex(2)}"
        if not Project.objects.filter(slug=candidate).exists():
            return candidate
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
        options = format_html_join("", '<option value="{}">', ((s,) for s in self._suggestions))
        return mark_safe(
            format_html('{}<datalist id="{}">{}</datalist>', html, self._list_id, options)
        )


class ProjectForm(forms.ModelForm):
    """User-facing form for creating or editing a project."""

    # The license selector is split into a dropdown + a custom-text input.
    # If the user picks "__other__", `license_custom` is what gets saved.
    LICENSE_CHOICES = (
        [("", "— pick a license —")]
        + [("Recommended", RECOMMENDED_LICENSES)]
        + [("Other licenses", OTHER_LICENSES)]
        + [("__other__", "Other (write below)")]
    )
    license_choice = forms.ChoiceField(
        choices=LICENSE_CHOICES,
        required=False,
        label="License",
    )
    license_custom = forms.CharField(
        max_length=80,
        required=False,
        label="Custom license",
        widget=forms.TextInput(
            attrs={"placeholder": "SPDX identifier or short name"}
        ),
    )

    tags_input = forms.CharField(
        max_length=400,
        required=False,
        label="Tags",
        widget=forms.TextInput(
            attrs={
                "placeholder": "Comma-separated. e.g. pump, co2-sensor, ROV",
            }
        ),
        help_text="Comma-separated keywords. Lowercase preferred. New tags are created on save.",
    )

    class Meta:
        model = Project
        fields = [
            "title",
            "summary",
            "readme",
            "field",
            "artifact_type",
            "canonical_url",
            "doi",
            "cover_image_url",
            "cover_image_focal_x",
            "cover_image_focal_y",
            "cover_image_zoom",
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
            "field": forms.TextInput(
                attrs={"placeholder": "e.g. Oceanography"}
            ),
            "artifact_type": forms.TextInput(
                attrs={"placeholder": "e.g. Hardware"}
            ),
            "institution": forms.TextInput(
                attrs={"placeholder": "Your institution or lab. Free text."}
            ),
            "cover_image_url": forms.URLInput(
                attrs={
                    "placeholder": "https://raw.githubusercontent.com/you/repo/main/cover.png",
                }
            ),
            "doi": forms.TextInput(
                attrs={
                    "placeholder": "e.g. 10.5281/zenodo.1234567",
                }
            ),
            "cover_image_focal_x": forms.HiddenInput(),
            "cover_image_focal_y": forms.HiddenInput(),
            "cover_image_zoom": forms.NumberInput(
                attrs={"type": "range", "min": 1, "max": 3, "step": 0.05}
            ),
        }
        labels = {
            "summary": "Short description",
            "readme": "README",
            "artifact_type": "Project type",
            "canonical_url": "Where the files live",
            "doi": "DOI",
            "cover_image_url": "Cover image URL",
            "cover_image_focal_x": "Horizontal crop",
            "cover_image_focal_y": "Vertical crop",
            "cover_image_zoom": "Zoom",
        }
        help_texts = {
            "field": _examples_text(FIELD_SUGGESTIONS),
            "artifact_type": _examples_text(PROJECT_TYPE_SUGGESTIONS),
            "readme": (
                "Markdown is supported. For inline images, link to files "
                "hosted elsewhere; OSPREY does not host README images."
            ),
            "canonical_url": "Upstream repository or archive (GitHub, Codeberg, Zenodo).",
            "doi": (
                "Optional. Paste a DOI from Zenodo, OSF, Figshare, or any "
                "DOI-minting repository. OSPREY-facilitated DOI minting is "
                "in planning; for now, get a DOI elsewhere and paste it here."
            ),
            "cover_image_url": (
                "Optional. A direct link to an image (PNG/JPG) hosted elsewhere. "
                "For GitHub, use the 'raw' URL. Shown on cards and the project page."
            ),
            "cover_image_focal_x": "Saved horizontal crop position.",
            "cover_image_focal_y": "Saved vertical crop position.",
            "cover_image_zoom": "Zoom in when important detail is too small in the crop.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Pre-populate the license dropdown from the saved value.
        existing = (self.instance.license or "").strip() if self.instance else ""
        known = {key for key, _label in COMMON_LICENSES}
        if existing in known:
            self.fields["license_choice"].initial = existing
        elif existing:
            self.fields["license_choice"].initial = "__other__"
            self.fields["license_custom"].initial = existing
        # Pre-populate tags from the saved instance.
        if self.instance and self.instance.pk:
            self.fields["tags_input"].initial = ", ".join(
                self.instance.tags.values_list("name", flat=True)
            )

    def clean(self):
        cleaned = super().clean()
        choice = cleaned.get("license_choice", "")
        custom = (cleaned.get("license_custom") or "").strip()
        if choice == "__other__":
            if not custom:
                self.add_error(
                    "license_custom",
                    "Pick a license from the list or write a custom identifier here.",
                )
            cleaned["resolved_license"] = custom
        else:
            cleaned["resolved_license"] = choice
        return cleaned

    def clean_doi(self) -> str:
        """Accept a bare DOI; tolerate pasted URLs and 'doi:' prefixes."""
        raw = (self.cleaned_data.get("doi") or "").strip()
        if not raw:
            return ""
        # Strip common prefixes a user might paste.
        normalized = raw
        for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
            if normalized.lower().startswith(prefix):
                normalized = normalized[len(prefix):]
                break
        # DOI shape: 10.<registrant>/<suffix>. The suffix can contain almost
        # anything printable; we just check the registrant prefix.
        if not re.match(r"^10\.\d{4,9}/\S+$", normalized):
            raise forms.ValidationError(
                "That doesn't look like a DOI. Expected something like "
                "10.5281/zenodo.1234567."
            )
        return normalized

    def save(self, commit: bool = True) -> Project:
        project = super().save(commit=False)
        project.license = self.cleaned_data.get("resolved_license", "") or ""
        if not project.slug:
            project.slug = _generate_slug(project.title)
        if commit:
            project.save()
            self.save_m2m()
        return project

    def _save_m2m(self):  # type: ignore[override]
        super()._save_m2m()
        self._save_tags(self.instance)

    def _save_tags(self, project: Project) -> None:
        raw = self.cleaned_data.get("tags_input", "") or ""
        names = [t.strip() for t in raw.split(",") if t.strip()]
        seen: set[str] = set()
        normalized: list[str] = []
        for name in names:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(key)
        tag_objs = []
        for name in normalized:
            tag, _ = Tag.objects.get_or_create(name=name)
            tag_objs.append(tag)
        project.tags.set(tag_objs)


class ContributionForm(forms.ModelForm):
    """One row in the contributor formset."""

    class Meta:
        model = Contribution
        fields = ["display_name", "role", "credit_statement", "order"]
        widgets = {
            "display_name": forms.TextInput(
                attrs={"placeholder": "Name as shown on the project page"}
            ),
            "role": forms.TextInput(
                attrs={
                    "list": "contributor-role-suggestions",
                    "placeholder": "e.g. Project lead, Principal investigator, Maintainer",
                }
            ),
            "credit_statement": forms.TextInput(
                attrs={"placeholder": "Optional. What this person did."}
            ),
            "order": forms.HiddenInput(),
        }
        labels = {
            "display_name": "Display name",
            "role": "Role",
            "credit_statement": "Credit statement",
            "order": "Order",
        }


ContributionFormSet = inlineformset_factory(
    Project,
    Contribution,
    form=ContributionForm,
    extra=0,
    can_delete=True,
    min_num=1,
    validate_min=True,
)
