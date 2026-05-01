from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0006_project_institution_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="cover_image_url",
            field=models.URLField(
                blank=True,
                help_text=(
                    "Optional. Link to a cover image hosted elsewhere "
                    "(GitHub raw URL, Zenodo, lab website). OSPREY does not host images."
                ),
            ),
        ),
    ]
