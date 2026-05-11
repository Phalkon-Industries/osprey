"""Add Project.public_id (UUID), populating existing rows in a 3-step migration."""

import uuid

from django.db import migrations, models


def _populate_public_ids(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    for project in Project.objects.filter(public_id__isnull=True):
        project.public_id = uuid.uuid4()
        project.save(update_fields=["public_id"])


def _noop(apps, schema_editor):
    return None


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0014_project_versions"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="public_id",
            field=models.UUIDField(null=True, unique=False),
        ),
        migrations.RunPython(_populate_public_ids, _noop),
        migrations.AlterField(
            model_name="project",
            name="public_id",
            field=models.UUIDField(
                default=uuid.uuid4,
                editable=False,
                unique=True,
                help_text=(
                    "Permanent OSPREY project identifier. Used in the project "
                    "permalink (/p/<public_id>/) and embedded in Zenodo metadata "
                    "so external records keep pointing at this project even if "
                    "the slug or title changes."
                ),
            ),
        ),
    ]
