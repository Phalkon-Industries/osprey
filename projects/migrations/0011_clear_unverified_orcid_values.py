from django.db import migrations


LEGACY_STUB_CREDIT = "Stub credit. Auto-links if Carol later registers with this ORCID iD."


def clear_unverified_orcid_values(apps, schema_editor):
    Contribution = apps.get_model("projects", "Contribution")
    Profile = apps.get_model("people", "Profile")

    table_names = schema_editor.connection.introspection.table_names()
    verified_user_ids = set()
    if "socialaccount_socialaccount" in table_names:
        with schema_editor.connection.cursor() as cursor:
            cursor.execute(
                "SELECT user_id FROM socialaccount_socialaccount WHERE provider = %s",
                ["orcid"],
            )
            verified_user_ids = {row[0] for row in cursor.fetchall()}

    for contribution in Contribution.objects.exclude(orcid_id="").iterator():
        if contribution.user_id and contribution.user_id in verified_user_ids:
            continue
        contribution.orcid_id = ""
        update_fields = ["orcid_id"]
        if contribution.credit_statement == LEGACY_STUB_CREDIT:
            contribution.display_name = "Carol Lab Lead"
            contribution.role = "Principal investigator"
            contribution.credit_statement = "Ran the lab effort and supported the field deployment."
            update_fields.extend(["display_name", "role", "credit_statement"])
        contribution.save(update_fields=update_fields)

    for profile in Profile.objects.exclude(orcid_placeholder="").iterator():
        if profile.user_id and profile.user_id in verified_user_ids:
            continue
        profile.orcid_placeholder = ""
        profile.save(update_fields=["orcid_placeholder"])


class Migration(migrations.Migration):

    dependencies = [
        ("people", "0005_delete_institution"),
        ("projects", "0010_alter_contribution_role_alter_contribution_user"),
    ]

    operations = [
        migrations.RunPython(clear_unverified_orcid_values, migrations.RunPython.noop),
    ]
