from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("people", "0004_profile_institution_text"),
    ]

    operations = [
        migrations.DeleteModel(name="Institution"),
    ]
