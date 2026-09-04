# Contributor claiming backfill (decided 2026-09-04). Rows that already
# carried a linked user got that link under the old auto-attach rules
# and conferred edit rights via editable_by; grandfather them as
# verified editors so nobody silently loses the access or the checkmark
# they have today. Owners can revoke the editor flag per row afterward.
from django.db import migrations


def backfill(apps, schema_editor):
    Contribution = apps.get_model("projects", "Contribution")
    Contribution.objects.filter(user__isnull=False).update(
        claim_status="verified", editor=True
    )


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0032_contribution_claim_status_contribution_editor_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
