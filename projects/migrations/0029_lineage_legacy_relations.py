# Map legacy lineage relations onto the two-relation vocabulary decided
# 2026-09-03 (see planning/features/lineage.md). forked_from folds into
# derived_from; inspired_by and replaces have no successor (references
# was cut as unfalsifiable, new_version_of was dropped as an edge type),
# so those rows are removed. Pre-existing rows never went through the
# claim flow, so they are grandfathered in as accepted.
from django.db import migrations, models


def remap(apps, schema_editor):
    LineageEdge = apps.get_model("projects", "LineageEdge")
    LineageEdge.objects.filter(relation="forked_from").update(
        relation="derived_from"
    )
    LineageEdge.objects.filter(
        relation__in=["inspired_by", "replaces"]
    ).delete()
    LineageEdge.objects.exclude(
        relation__in=["derived_from", "uses"]
    ).delete()
    # claimed_at must be set or the edges count as dormant draft claims
    # and stay hidden from the parent's lineage page.
    LineageEdge.objects.all().update(
        status="accepted", claimed_at=models.F("declared_at")
    )


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0028_alter_lineageedge_options_and_more"),
    ]

    operations = [
        migrations.RunPython(remap, migrations.RunPython.noop),
    ]
