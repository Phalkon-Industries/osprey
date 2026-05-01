"""Replace Profile.institution FK with a free-text CharField."""
from django.db import migrations, models


def copy_institution_names(apps, schema_editor):
    Profile = apps.get_model("people", "Profile")
    for p in Profile.objects.select_related("institution").all():
        if p.institution_id and p.institution and p.institution.name:
            p.institution_text = p.institution.name
            p.save(update_fields=["institution_text"])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("people", "0003_alter_profile_orcid_placeholder"),
        ("projects", "0006_project_institution_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="profile",
            name="institution_text",
            field=models.CharField(blank=True, max_length=200, default=""),
            preserve_default=False,
        ),
        migrations.RunPython(copy_institution_names, noop),
        migrations.RemoveField(
            model_name="profile",
            name="institution",
        ),
        migrations.RenameField(
            model_name="profile",
            old_name="institution_text",
            new_name="institution",
        ),
        migrations.AlterField(
            model_name="profile",
            name="institution",
            field=models.CharField(
                blank=True,
                help_text="Institution or lab name. Free text for now.",
                max_length=200,
            ),
        ),
    ]
