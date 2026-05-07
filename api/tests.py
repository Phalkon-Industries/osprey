from __future__ import annotations

from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.test import TestCase

from projects.models import ArtifactLink, Contribution, Project, ProjectDeposit, Tag, TagAssignment


def add_orcid_account(user, orcid_id: str = "0000-0001-2345-6789") -> None:
    SocialAccount.objects.create(
        user=user,
        provider="orcid",
        uid=orcid_id,
        extra_data={"orcid-identifier": {"path": orcid_id}},
    )


class ApiTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="alice")
        self.user.profile.display_name = "Alice Researcher"
        self.user.profile.institution = "WHOI"
        self.user.profile.save()
        add_orcid_account(self.user)
        self.public_project = Project.objects.create(
            slug="public-pump",
            title="Public Pump",
            summary="Public pump controller.",
            readme="# Public Pump",
            field="Oceanography",
            artifact_type="Hardware",
            license="MIT",
            doi="10.5072/zenodo.123",
            canonical_url="https://github.com/example/public-pump",
            institution="WHOI",
            visibility=Project.VISIBILITY_PUBLIC,
            created_by=self.user,
        )
        self.private_project = Project.objects.create(
            slug="private-pump",
            title="Private Pump",
            summary="Private pump controller.",
            visibility=Project.VISIBILITY_PRIVATE,
            created_by=self.user,
        )
        Contribution.objects.create(
            project=self.public_project,
            user=self.user,
            orcid_id="0000-0001-2345-6789",
            display_name="Alice Researcher",
            role="Project lead",
            credit_statement="Built it.",
        )
        ArtifactLink.objects.create(
            project=self.public_project,
            kind="github",
            url="https://github.com/example/public-pump",
            label="Repository",
        )
        tag = Tag.objects.create(name="pump")
        TagAssignment.objects.create(project=self.public_project, tag=tag)
        ProjectDeposit.objects.create(
            project=self.public_project,
            sandbox=True,
            deposition_id="123",
            record_id="123",
            doi="10.5072/zenodo.123",
            state=ProjectDeposit.STATE_PUBLISHED,
        )
        ProjectDeposit.objects.create(
            project=self.private_project,
            sandbox=True,
            deposition_id="999",
            record_id="999",
            doi="10.5072/zenodo.999",
            state=ProjectDeposit.STATE_PUBLISHED,
        )

    def test_project_list_returns_public_projects_and_filters(self):
        response = self.client.get("/api/v1/projects/", {"tag": "pump"})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["slug"], "public-pump")

    def test_project_detail_returns_nested_public_record(self):
        response = self.client.get("/api/v1/projects/public-pump/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["doi"], "10.5072/zenodo.123")
        self.assertEqual(payload["contributors"][0]["orcid_id"], "0000-0001-2345-6789")
        self.assertEqual(payload["artifact_links"][0]["kind"], "github")
        self.assertEqual(payload["tags"], [{"name": "pump"}])

    def test_private_project_detail_404s(self):
        response = self.client.get("/api/v1/projects/private-pump/")

        self.assertEqual(response.status_code, 404)

    def test_export_contains_public_survivable_data_only(self):
        response = self.client.get("/api/v1/export/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        project_slugs = {project["slug"] for project in payload["projects"]}
        deposit_dois = {deposit["doi"] for deposit in payload["project_deposits"]}
        contribution_orcids = {row["orcid_id"] for row in payload["contributions"]}

        self.assertIn("public-pump", project_slugs)
        self.assertNotIn("private-pump", project_slugs)
        self.assertIn("10.5072/zenodo.123", deposit_dois)
        self.assertNotIn("10.5072/zenodo.999", deposit_dois)
        self.assertEqual(contribution_orcids, {"0000-0001-2345-6789"})
