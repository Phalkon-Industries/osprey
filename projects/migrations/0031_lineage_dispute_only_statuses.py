# Dispute-only lineage model (decided 2026-09-04): the accept flow is
# gone, so pending / accepted / auto_accepted all collapse into the new
# default, "active". Disputed and withdrawn are unchanged.
from django.db import migrations


def collapse(apps, schema_editor):
    LineageEdge = apps.get_model("projects", "LineageEdge")
    LineageEdge.objects.filter(
        status__in=["pending", "accepted", "auto_accepted"]
    ).update(status="active")


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0030_alter_lineageedge_status"),
    ]

    operations = [
        migrations.RunPython(collapse, migrations.RunPython.noop),
    ]
