"""Forms for project creation and editing.

Profile editing lives in people/forms.py.
"""

from __future__ import annotations

import secrets

from django import forms
from django.forms import inlineformset_factory
from django.utils.html import format_html, format_html_join
from django.utils.safestring import mark_safe
from django.utils.text import slugify

from .models import MATURITY_LEVELS, Contribution, Project, Tag
from .cover_images import MAX_DOWNLOAD_BYTES, process_cover_image

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
# The license dropdown is a fixed, curated set of open licenses grouped
# by what is being licensed. OSPREY is open-source-only, so there is
# deliberately NO free-text license option: a non-open license must never
# be enterable. Additions are requested through the feedback widget and
# added here after review.
COPYLEFT_LICENSES = [
    ("AGPL-3.0", "AGPL 3.0 — software"),
    ("CERN-OHL-S-2.0", "CERN OHL-S 2.0 — hardware"),
    ("CC-BY-SA-4.0", "CC BY-SA 4.0 — docs and data"),
]
PERMISSIVE_LICENSES = [
    ("MIT", "MIT — software"),
    ("CERN-OHL-P-2.0", "CERN OHL-P 2.0 — hardware"),
    ("CC-BY-4.0", "CC BY 4.0 — docs and data"),
]
OTHER_LICENSES = [
    ("Apache-2.0", "Apache 2.0 — software"),
    ("GPL-3.0", "GPL 3.0 — software"),
    ("CERN-OHL-W-2.0", "CERN OHL-W 2.0 — hardware, weak copyleft"),
    ("CC0-1.0", "CC0 1.0 — data, public-domain dedication"),
]
COMMON_LICENSES = COPYLEFT_LICENSES + PERMISSIVE_LICENSES + OTHER_LICENSES


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
        options = format_html_join(
            "", '<option value="{}">', ((s,) for s in self._suggestions)
        )
        return mark_safe(
            format_html(
                '{}<datalist id="{}">{}</datalist>', html, self._list_id, options
            )
        )


class ProjectForm(forms.ModelForm):
    """User-facing form for creating or editing a project."""

    LICENSE_CHOICES = [
        ("", "— pick a license —"),
        ("Copyleft (derivatives must stay open)", COPYLEFT_LICENSES),
        ("Permissive (closed derivatives allowed)", PERMISSIVE_LICENSES),
        ("Other licenses", OTHER_LICENSES),
    ]
    license_choice = forms.ChoiceField(
        choices=LICENSE_CHOICES,
        required=False,
        label="License",
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
            "cover_image",
            "cover_image_focal_x",
            "cover_image_focal_y",
            "cover_image_zoom",
            "readme",
            "field",
            "artifact_type",
            "canonical_url",
            "institution",
            "funding",
            "self_rating",
            "self_rating_note",
            "provided_as_is",
            "publications",
            "wiki_requires_approval",
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
            "field": forms.TextInput(attrs={"placeholder": "e.g. Oceanography"}),
            "artifact_type": forms.TextInput(attrs={"placeholder": "e.g. Hardware"}),
            "institution": forms.Textarea(
                attrs={
                    "rows": 2,
                    "placeholder": "One per line, or comma-separated. e.g. WHOI, MIT",
                }
            ),
            "funding": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "e.g. NSF OCE-1234567 (PI: Smith)\nWHOI internal seed funding",
                }
            ),
            "self_rating": forms.Select(
                choices=[("", "\u2014 select a level \u2014")]
                + [
                    (level, f"{level} \u2014 {label}")
                    for level, label in MATURITY_LEVELS.items()
                ]
            ),
            "self_rating_note": forms.Textarea(
                attrs={
                    "rows": 3,
                    "placeholder": "e.g. 'Reliable on the bench, but the 3.3 V rail droops under load. One field season so far.'",
                }
            ),
            "publications": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "One per line. Include a DOI or URL where you can.",
                }
            ),
            "cover_image": forms.ClearableFileInput(
                attrs={"accept": "image/png,image/jpeg,image/webp,image/gif"}
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
            "canonical_url": "Project repository",
            "cover_image": "Cover image upload",
            "cover_image_focal_x": "Horizontal crop",
            "cover_image_focal_y": "Vertical crop",
            "cover_image_zoom": "Zoom",
            "institution": "Institutions",
            "funding": "Funding sources",
            "self_rating": "Project maturity (1\u201310)",
            "self_rating_note": "Notes on the maturity level (optional)",
            "publications": "Publications that used this project",
        }
        help_texts = {
            "field": _examples_text(FIELD_SUGGESTIONS),
            "artifact_type": _examples_text(PROJECT_TYPE_SUGGESTIONS),
            "readme": (
                "Markdown is supported. For inline images, link to files "
                "hosted elsewhere; OSPREY does not host README images."
            ),
            "canonical_url": "Public source repository (GitHub, Codeberg, GitLab). The link shown on the project page.",
            "cover_image": (
                "Optional. PNG, JPEG, WebP or GIF, up to 10 MiB; SVG isn't "
                "accepted. OSPREY downscales and re-encodes it to WebP."
            ),
            "cover_image_focal_x": "Saved horizontal crop position.",
            "cover_image_focal_y": "Saved vertical crop position.",
            "cover_image_zoom": "Zoom in when important detail is too small in the crop.",
            "institution": (
                "List each affiliation on its own line, or separate them with "
                "commas. Each one becomes a link to its institution page."
            ),
            "funding": (
                "Grants, programs, or sponsors that supported this work. "
                "Academic work usually has funders that deserve credit."
            ),
            "self_rating": (
                "Levels build on each other, so claim a level only if "
                "everything below it also holds. \u201cService conditions\u201d "
                "means the project doing its real job rather than a test."
            ),
            "self_rating_note": (
                "A sentence or two on why it sits at that level. What works, "
                "what doesn't, what to watch for."
            ),
            "publications": (
                "Papers, talks, theses, or reports that used this project. "
                "One per line. A DOI or URL helps readers find each item."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The short description drives cards and search results, so the
        # form requires it even though old rows may predate the rule.
        self.fields["summary"].required = True
        self.fields["provided_as_is"].label = "Provided as-is. No support or updates are planned."
        self.fields["provided_as_is"].help_text = ""
        # Pre-populate the license dropdown from the saved value. A saved
        # license that is no longer on the curated list (all past options
        # were open licenses) stays valid for this project, so editing
        # never forces a relicense.
        existing = (self.instance.license or "").strip() if self.instance else ""
        known = {key for key, _label in COMMON_LICENSES}
        if existing and existing not in known:
            self.fields["license_choice"].choices = [
                self.LICENSE_CHOICES[0],
                (existing, f"Current license: {existing}"),
            ] + self.LICENSE_CHOICES[1:]
        if existing:
            self.fields["license_choice"].initial = existing
        # Pre-populate tags from the saved instance.
        if self.instance and self.instance.pk:
            self.fields["tags_input"].initial = ", ".join(
                self.instance.tags.values_list("name", flat=True)
            )

    def clean(self):
        cleaned = super().clean()
        cleaned["resolved_license"] = cleaned.get("license_choice", "")
        # A published project's license travels with its published files,
        # so it can't be changed here (decided 2026-09-29). It changes with
        # the next version, on the new-version page.
        instance = self.instance
        if instance is not None and instance.pk and instance.is_public and instance.origin == Project.ORIGIN_NATIVE and instance.license:
            cleaned["resolved_license"] = instance.license
            self.errors.pop("license_choice", None)
        return cleaned

    def clean_cover_image(self):
        upload = self.cleaned_data.get("cover_image")
        if not upload:
            return upload
        size = getattr(upload, "size", None)
        if size is not None and size > MAX_DOWNLOAD_BYTES:
            mib = MAX_DOWNLOAD_BYTES / (1024 * 1024)
            raise forms.ValidationError(
                f"Cover image must be {mib:.0f} MiB or smaller."
            )
        return upload

    def save(self, commit: bool = True) -> Project:
        project = super().save(commit=False)
        project.license = self.cleaned_data.get("resolved_license", "") or ""
        # An as-is project has nobody reviewing wiki suggestions, so its wiki
        # is always open to direct edits (decided 2026-09-29).
        if project.provided_as_is:
            project.wiki_requires_approval = False
        if not project.slug:
            project.slug = _generate_slug(project.title)
        # Re-encode cover image (upload or URL) before persisting.
        try:
            process_cover_image(project)
        except Exception:  # pragma: no cover - defensive; processor logs
            pass
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

    def clean(self):
        cleaned = super().clean()
        # The Project Owner's own row can't be removed: ghost-submitting
        # for someone else means staying on the contributor list (an
        # "Uploaded project" role is fine). Silently ignore the delete.
        if (
            cleaned.get("DELETE")
            and self.instance.pk
            and self.instance.user_id
            and self.instance.project_id
            and self.instance.user_id == self.instance.project.created_by_id
        ):
            cleaned["DELETE"] = False
        return cleaned

    class Meta:
        model = Contribution
        fields = [
            "display_name",
            "role",
            "affiliation",
            "orcid_id",
            "credit_statement",
            "order",
        ]
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
            "affiliation": forms.TextInput(
                attrs={
                    "placeholder": "e.g. WHOI, MIT",
                }
            ),
            "orcid_id": forms.HiddenInput(
                attrs={
                    "pattern": r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]",
                    "data-contributor-orcid": "",
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
            "affiliation": "Affiliation (optional)",
            "orcid_id": "ORCID iD (optional)",
            "credit_statement": "Credit statement",
            "order": "Order",
        }
        help_texts = {
            "orcid_id": (
                "Optional. Paste the contributor's public ORCID iD. "
                "It links to their verified profile automatically when they sign in with ORCID."
            ),
        }

    def clean_orcid_id(self) -> str:
        value = (self.cleaned_data.get("orcid_id") or "").strip()
        if not value:
            return ""
        # Strip a pasted URL prefix so users can copy the public profile link.
        for prefix in ("https://orcid.org/", "http://orcid.org/", "orcid.org/"):
            if value.lower().startswith(prefix):
                value = value[len(prefix) :]
                break
        return value.upper()  # only the final checksum digit can be X; uppercase it


ContributionFormSet = inlineformset_factory(
    Project,
    Contribution,
    form=ContributionForm,
    extra=0,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


class NewVersionForm(forms.Form):
    """Form shown when starting a new published Zenodo version."""

    def __init__(self, *args, current_license: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        choices = list(ProjectForm.LICENSE_CHOICES[1:])
        known = {key for key, _label in COMMON_LICENSES}
        if current_license and current_license not in known:
            choices = [(current_license, f"Current license: {current_license}")] + choices
        self.fields["license_choice"].choices = choices
        if current_license:
            self.fields["license_choice"].initial = current_license

    changelog = forms.CharField(
        label="What changed in this version",
        widget=forms.Textarea(attrs={"rows": 6}),
        help_text=(
            "Required. Describe what is different from the previous version. "
            "Plain text or Markdown."
        ),
    )
    license_choice = forms.ChoiceField(
        label="License for this version",
        required=False,
        help_text="Applies to this version's files. Leave as is unless the license changed.",
    )
    # The archive is read from request.FILES by the view; the field exists so
    # archive errors have somewhere to land.
    archive = forms.FileField(required=False)
    repo_link = forms.URLField(
        label="Repository link for this version (optional)",
        required=False,
        help_text=(
            "Optional. A specific URL for this version, for example a GitHub "
            "release tag or a Codeberg tag. Stored on the version row and "
            "advertised in the Zenodo record's related identifiers."
        ),
    )

    def clean_changelog(self) -> str:
        value = (self.cleaned_data.get("changelog") or "").strip()
        if not value:
            raise forms.ValidationError(
                "A changelog is required when publishing a new version."
            )
        return value
