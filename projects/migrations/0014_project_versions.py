"""Add ProjectDepositVersion and the deposit's pending changelog/repo fields."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("projects", "0013_projectdeposit"),
    ]

    operations = [
        migrations.AddField(
            model_name="projectdeposit",
            name="pending_changelog",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Changelog text the user supplied for the in-flight new-version "
                    "draft. Cleared on publish; the published value lives on "
                    "ProjectDepositVersion."
                ),
            ),
        ),
        migrations.AddField(
            model_name="projectdeposit",
            name="repo_link",
            field=models.URLField(
                blank=True,
                default="",
                help_text=(
                    "Optional per-version repository URL (e.g. a GitHub release "
                    "tag) for the in-flight draft. Cleared on publish."
                ),
            ),
        ),
        migrations.CreateModel(
            name="ProjectDepositVersion",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("version_index", models.PositiveIntegerField()),
                ("deposition_id", models.CharField(blank=True, max_length=80)),
                ("record_id", models.CharField(blank=True, max_length=80)),
                ("doi", models.CharField(blank=True, max_length=120)),
                (
                    "changelog",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="What changed in this version. Plain text or Markdown.",
                    ),
                ),
                (
                    "repo_link",
                    models.URLField(
                        blank=True,
                        default="",
                        help_text="Optional repository URL pointing at the release for this version.",
                    ),
                ),
                ("published_at", models.DateTimeField()),
                ("last_response", models.JSONField(blank=True, default=dict)),
                (
                    "deposit",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="versions",
                        to="projects.projectdeposit",
                    ),
                ),
            ],
            options={
                "ordering": ["-version_index"],
                "indexes": [
                    models.Index(
                        fields=["deposit", "version_index"],
                        name="projects_dep_version_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("deposit", "version_index"),
                        name="unique_deposit_version_index",
                    )
                ],
            },
        ),
    ]
