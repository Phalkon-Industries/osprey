from django import forms

from .models import REPORT_CATEGORY_CHOICES


class ReportForm(forms.Form):
    category = forms.ChoiceField(choices=REPORT_CATEGORY_CHOICES)
    reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 4, "maxlength": 4000}),
        required=False,
        help_text="Optional. Add context to help staff act on this report.",
    )
