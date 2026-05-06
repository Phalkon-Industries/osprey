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

    usertag = forms.CharField(max_length=USER_TAG_MAX_LENGTH, required=True, label="User tag")
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
    ]

    class Meta:
        model = Profile
        fields = ["display_name", "bio", "avatar", "institution"]
        widgets = {
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
        }
        help_texts = {
            "display_name": "How your name appears on project pages. Falls back to your full name or username.",
            "bio": "Optional. Background, interests, current lab affiliation.",
            "avatar": "Optional. A square crop works best.",
        }

    def __init__(self, *args, allow_usertag: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        unlocked = bool(
            self.instance
            and self.instance.user_id
            and not self.instance.usertag_locked
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
