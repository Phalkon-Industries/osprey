"""Forms for the people app."""
from __future__ import annotations

from django import forms

from .models import Profile


class ProfileForm(forms.ModelForm):
    """User-facing profile edit form. Also writes through to auth.User names."""

    first_name = forms.CharField(max_length=150, required=False, label="First name")
    last_name = forms.CharField(max_length=150, required=False, label="Last name")

    class Meta:
        model = Profile
        fields = ["display_name", "bio", "avatar", "institution"]
        widgets = {
            "bio": forms.Textarea(
                attrs={
                    "rows": 8,
                    "placeholder": "A short bio. Markdown is supported.",
                }
            ),
        }
        labels = {
            "display_name": "Display name",
        }
        help_texts = {
            "display_name": "How your name appears on project pages. Falls back to your full name or username.",
            "bio": "Optional. Background, interests, current lab affiliation.",
            "avatar": "Optional. A square crop works best.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.user_id:
            self.fields["first_name"].initial = self.instance.user.first_name
            self.fields["last_name"].initial = self.instance.user.last_name

    def save(self, commit: bool = True) -> Profile:
        profile = super().save(commit=False)
        user = profile.user
        user.first_name = self.cleaned_data.get("first_name", "")
        user.last_name = self.cleaned_data.get("last_name", "")
        if commit:
            user.save()
            profile.save()
        return profile
