from django import forms

from .models import Attestation


class AttestationForm(forms.ModelForm):
    citations = forms.CharField(
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "placeholder": (
                    "One citation per line. Plain text is fine. "
                    "Include a DOI or URL when you have one."
                ),
            }
        ),
        help_text=(
            "Papers, talks, or write-ups where you used this project. "
            "These are tagged as user-submitted on the citations dashboard."
        ),
    )

    class Meta:
        model = Attestation
        fields = ["narrative", "used_at"]
        widgets = {
            "narrative": forms.Textarea(
                attrs={
                    "rows": 6,
                    "placeholder": "What did you use this for? What worked, what didn't?",
                }
            ),
            "used_at": forms.TextInput(
                attrs={"placeholder": "e.g. 2024 cruise; lab benchmark"}
            ),
        }

    def citation_lines(self):
        raw = self.cleaned_data.get("citations") or ""
        return [line.strip() for line in raw.splitlines() if line.strip()]
