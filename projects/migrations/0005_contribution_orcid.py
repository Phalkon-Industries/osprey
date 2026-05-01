from django.db import migrations, models
import django.db.models.deletion
import django.core.validators
from django.conf import settings


def backfill_contributions(apps, schema_editor):
    """Populate orcid_id and display_name for existing rows.

    Existing seed rows all have a user FK. Pull display_name from the
    related Profile or User. Pull orcid_id from Profile.orcid_placeholder
    if set, otherwise synthesize a deterministic placeholder ORCID-shaped
    string keyed off the user pk so the unique constraint and validator
    both pass. Real ORCIDs are required going forward via the form.
    """
    Contribution = apps.get_model("projects", "Contribution")
    for c in Contribution.objects.select_related("user").all():
        user = c.user
        if user is None:
            # Should not happen pre-migration; bail with safe defaults.
            c.display_name = "Unknown"
            c.orcid_id = f"0000-0000-0000-{c.pk:04d}"[-19:]
            c.save(update_fields=["display_name", "orcid_id"])
            continue
        # Display name: profile.display_name -> full name -> username.
        display = ""
        try:
            display = (user.profile.display_name or "").strip()
        except Exception:
            display = ""
        if not display:
            display = (user.get_full_name() or "").strip()
        if not display:
            display = user.get_username()
        c.display_name = display[:200]
        # ORCID iD: from profile if it looks valid, else synthesize.
        orcid = ""
        try:
            orcid = (user.profile.orcid_placeholder or "").strip()
        except Exception:
            orcid = ""
        import re
        if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", orcid):
            # Deterministic per-user placeholder so tests stay reproducible.
            uid = user.pk
            block = f"{uid:012d}"
            orcid = f"0000-{block[0:4]}-{block[4:8]}-{block[8:12]}"
        c.orcid_id = orcid
        c.save(update_fields=["display_name", "orcid_id"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("projects", "0004_alter_project_description"),
    ]

    operations = [
        # Drop the old unique-on-(project,user,role) before changing things.
        migrations.RemoveConstraint(
            model_name="contribution",
            name="unique_contribution_role",
        ),
        # Allow user to be nullable so stub rows can exist.
        migrations.AlterField(
            model_name="contribution",
            name="user",
            field=models.ForeignKey(
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="contributions",
                to=settings.AUTH_USER_MODEL,
                help_text="Filled in once a user with this ORCID iD exists on OSPREY.",
            ),
        ),
        # Add new fields as nullable for backfill.
        migrations.AddField(
            model_name="contribution",
            name="orcid_id",
            field=models.CharField(
                max_length=19,
                null=True,
                validators=[
                    django.core.validators.RegexValidator(
                        regex=r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$",
                        message="ORCID iD must look like 0000-0000-0000-000X.",
                    )
                ],
                help_text="ORCID iD of the contributor. Required.",
            ),
        ),
        migrations.AddField(
            model_name="contribution",
            name="display_name",
            field=models.CharField(
                max_length=200,
                null=True,
                help_text="Name as it should appear on the project page.",
            ),
        ),
        migrations.AddField(
            model_name="contribution",
            name="credit_statement",
            field=models.CharField(
                max_length=400,
                blank=True,
                default="",
                help_text="Optional. A sentence describing what this person did.",
            ),
        ),
        # Widen role to free text.
        migrations.AlterField(
            model_name="contribution",
            name="role",
            field=models.CharField(
                max_length=80,
                default="Author",
                help_text="Free text. Pick from the suggestions or write your own.",
            ),
        ),
        # Backfill orcid_id and display_name on existing rows.
        migrations.RunPython(backfill_contributions, noop_reverse),
        # Now make the new fields non-null.
        migrations.AlterField(
            model_name="contribution",
            name="orcid_id",
            field=models.CharField(
                max_length=19,
                validators=[
                    django.core.validators.RegexValidator(
                        regex=r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$",
                        message="ORCID iD must look like 0000-0000-0000-000X.",
                    )
                ],
                help_text="ORCID iD of the contributor. Required.",
            ),
        ),
        migrations.AlterField(
            model_name="contribution",
            name="display_name",
            field=models.CharField(
                max_length=200,
                help_text="Name as it should appear on the project page.",
            ),
        ),
        # Add the new unique constraint and the orcid index.
        migrations.AddConstraint(
            model_name="contribution",
            constraint=models.UniqueConstraint(
                fields=("project", "orcid_id"),
                name="unique_contribution_per_project",
            ),
        ),
        migrations.AddIndex(
            model_name="contribution",
            index=models.Index(fields=["orcid_id"], name="projects_co_orcid_i_idx"),
        ),
    ]
