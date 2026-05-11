from django.db import migrations


LICENSE_RENAMES = {
    "GPL-3.0-or-later": "GPL-3.0",
    "GPL-3.0-only": "GPL-3.0",
    "LGPL-3.0-or-later": "LGPL-3.0",
    "LGPL-3.0-only": "LGPL-3.0",
    "AGPL-3.0-or-later": "AGPL-3.0",
    "AGPL-3.0-only": "AGPL-3.0",
}


def normalize_forward(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    for old, new in LICENSE_RENAMES.items():
        Project.objects.filter(license=old).update(license=new)


def normalize_reverse(apps, schema_editor):
    # No-op: we cannot tell which "GPL-3.0" rows came from "-or-later" vs
    # "-only". Leaving values as-is is safe.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0015_project_public_id"),
    ]

    operations = [
        migrations.RunPython(normalize_forward, normalize_reverse),
    ]
