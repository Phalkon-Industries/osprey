from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("conversations", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectreply",
            name="accepted_by_asker_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
