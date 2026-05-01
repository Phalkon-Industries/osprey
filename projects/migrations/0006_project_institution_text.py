"""Replace Project.institution FK with a free-text CharField.

Two-step: add a new CharField, copy values from the FK target, drop the FK,
rename the new field into place.
"""
from django.db import migrations, models


def copy_institution_names(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    for p in Project.objects.select_related("institution").all():
        if p.institution_id and p.institution and p.institution.name:
            p.institution_text = p.institution.name
            p.save(update_fields=["institution_text"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0005_contribution_orcid"),
        ("people", "0003_alter_profile_orcid_placeholder"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="institution_text",
            field=models.CharField(blank=True, max_length=200, default=""),
            preserve_default=False,
        ),
        migrations.RunPython(copy_institution_names, noop),
        migrations.RemoveField(
            model_name="project",
            name="institution",
        ),
        migrations.RenameField(
            model_name="project",
            old_name="institution_text",
            new_name="institution",
        ),
        migrations.AlterField(
            model_name="project",
            name="institution",
            field=models.CharField(
                blank=True,
                help_text="Institution or lab name. Free text for now.",
                max_length=200,
            ),
        ),
    ]
