"""Forms for the people app."""
from __future__ import annotations

import re

from django import forms

from .models import Profile


_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")


class ProfileForm(forms.ModelForm):
    """User-facing profile edit form. Also writes through to auth.User names."""

    first_name = forms.CharField(max_length=150, required=False, label="First name")
    last_name = forms.CharField(max_length=150, required=False, label="Last name")

    class Meta:
        model = Profile
        fields = ["display_name", "orcid_placeholder", "bio", "avatar", "institution"]
        widgets = {
            "orcid_placeholder": forms.TextInput(
                attrs={
                    "placeholder": "0000-0000-0000-000X",
                    "pattern": r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]",
                    "size": 22,
                }
            ),
            "institution": forms.TextInput(
                attrs={
                    "placeholder": "Your institution or lab. Free text.",
                }
            ),
            "bio": forms.Textarea(
                attrs={
                    "rows": 8,
                    "placeholder": "A short bio. Markdown is supported.",
                }
            ),
        }
        labels = {
            "display_name": "Display name",
            "orcid_placeholder": "ORCID iD",
        }
        help_texts = {
            "display_name": "How your name appears on project pages. Falls back to your full name or username.",
            "orcid_placeholder": (
                "Self-asserted for now (ORCID OAuth comes later). Setting this "
                "automatically attaches you to any existing project credits "
                "made out to this ORCID iD."
            ),
            "bio": "Optional. Background, interests, current lab affiliation.",
            "avatar": "Optional. A square crop works best.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.user_id:
            self.fields["first_name"].initial = self.instance.user.first_name
            self.fields["last_name"].initial = self.instance.user.last_name

    def clean_orcid_placeholder(self):
        value = (self.cleaned_data.get("orcid_placeholder") or "").strip()
        if value and not _ORCID_RE.match(value):
            raise forms.ValidationError(
                "ORCID iD must look like 0000-0000-0000-000X."
            )
        return value

    def save(self, commit: bool = True) -> Profile:
        profile = super().save(commit=False)
        user = profile.user
        user.first_name = self.cleaned_data.get("first_name", "")
        user.last_name = self.cleaned_data.get("last_name", "")
        if commit:
            user.save()
            profile.save()
        return profile
