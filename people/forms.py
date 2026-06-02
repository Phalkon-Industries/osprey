"""Forms for the people app."""

from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model

from .identity import USER_TAG_MAX_LENGTH, clean_user_tag
from .models import Profile


class ProfileForm(forms.ModelForm):
    """User-facing profile form.

    The user tag is only editable during first-login onboarding. After the
    onboarding form is saved, the tag is locked and the regular edit page
    never offers it as an editable field.
    """

    usertag = forms.CharField(
        max_length=USER_TAG_MAX_LENGTH, required=True, label="User tag"
    )
    first_name = forms.CharField(max_length=150, required=False, label="First name")
    last_name = forms.CharField(max_length=150, required=False, label="Last name")

    field_order = [
        "usertag",
        "first_name",
        "last_name",
        "display_name",
        "institution",
        "bio",
        "avatar",
        "avatar_zoom",
        "avatar_focal_x",
        "avatar_focal_y",
    ]

    class Meta:
        model = Profile
        fields = [
            "display_name",
            "bio",
            "avatar",
            "avatar_focal_x",
            "avatar_focal_y",
            "avatar_zoom",
            "institution",
        ]
        widgets = {
            "institution": forms.Textarea(
                attrs={
                    "rows": 2,
                    "placeholder": "One per line, or comma-separated. e.g. WHOI, MIT",
                }
            ),
            "bio": forms.Textarea(
                attrs={
                    "rows": 8,
                    "placeholder": "A short bio. Markdown is supported.",
                }
            ),
            "avatar_focal_x": forms.HiddenInput(),
            "avatar_focal_y": forms.HiddenInput(),
            "avatar_zoom": forms.NumberInput(
                attrs={"type": "range", "min": 1, "max": 3, "step": 0.05}
            ),
        }
        labels = {
            "display_name": "Display name",
            "avatar_focal_x": "Horizontal crop",
            "avatar_focal_y": "Vertical crop",
            "avatar_zoom": "Zoom",
        }
        help_texts = {
            "display_name": "How your name appears on project pages. Falls back to your full name or username.",
            "bio": "Optional. Background, interests, current lab affiliation.",
            "avatar": "Optional. Drag the preview to position; use the slider to zoom.",
            "avatar_focal_x": "Saved horizontal crop position.",
            "avatar_focal_y": "Saved vertical crop position.",
            "avatar_zoom": "Zoom in when the important detail is too small.",
            "institution": (
                "List each affiliation on its own line, or separate them with "
                "commas. Each one becomes a link to its institution page."
            ),
        }

    def __init__(self, *args, allow_usertag: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("avatar_focal_x", "avatar_focal_y", "avatar_zoom"):
            if name in self.fields:
                self.fields[name].required = False
        unlocked = bool(
            self.instance and self.instance.user_id and not self.instance.usertag_locked
        )
        if allow_usertag and unlocked:
            self.fields["usertag"].initial = self.instance.user.username
            self.fields["usertag"].help_text = (
                "Used for @ mentions. You can include or omit the @. "
                "After saving this form, the tag is permanent for now."
            )
        else:
            self.fields.pop("usertag", None)
        if self.instance and self.instance.user_id:
            self.fields["first_name"].initial = self.instance.user.first_name
            self.fields["last_name"].initial = self.instance.user.last_name

    def clean_avatar_focal_x(self):
        v = self.cleaned_data.get("avatar_focal_x")
        return 50 if v in (None, "") else v

    def clean_avatar_focal_y(self):
        v = self.cleaned_data.get("avatar_focal_y")
        return 50 if v in (None, "") else v

    def clean_avatar_zoom(self):
        v = self.cleaned_data.get("avatar_zoom")
        return 1 if v in (None, "") else v

    def clean_usertag(self) -> str:
        tag = clean_user_tag(self.cleaned_data["usertag"])
        if len(tag) < 3:
            raise forms.ValidationError("Use at least 3 characters.")
        if not tag.replace("_", "").isalnum():
            raise forms.ValidationError("Use letters, numbers, or underscores only.")
        User = get_user_model()
        existing = User.objects.filter(username__iexact=tag)
        if self.instance and self.instance.user_id:
            existing = existing.exclude(pk=self.instance.user_id)
        if existing.exists():
            raise forms.ValidationError("That user tag is already taken.")
        return tag

    def save(self, commit: bool = True) -> Profile:
        profile = super().save(commit=False)
        user = profile.user
        if "usertag" in self.cleaned_data and not profile.usertag_locked:
            user.username = self.cleaned_data["usertag"]
            profile.usertag_locked = True
        user.first_name = self.cleaned_data.get("first_name", "")
        user.last_name = self.cleaned_data.get("last_name", "")
        if commit:
            user.save()
            profile.save()
        return profile
