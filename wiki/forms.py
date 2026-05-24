from __future__ import annotations

from django import forms

from .models import WikiPage, WikiRevision


class WikiPageForm(forms.ModelForm):
    summary = forms.CharField(
        max_length=200,
        required=False,
        help_text="Optional one-line summary of what you changed.",
    )

    class Meta:
        model = WikiPage
        fields = ["title", "body"]
        widgets = {
            "title": forms.TextInput(attrs={"placeholder": "Page title"}),
            "body": forms.Textarea(
                attrs={"rows": 18, "placeholder": "Wiki content in Markdown..."}
            ),
        }


class WikiRevisionReviewForm(forms.Form):
    ACTION_APPLY = "apply"
    ACTION_REJECT = "reject"
    action = forms.ChoiceField(
        choices=[(ACTION_APPLY, "Apply"), (ACTION_REJECT, "Reject")]
    )
