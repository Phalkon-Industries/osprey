"""Full-text search on the Projects page and the API.

Ranking weights: title, then summary, tags, contributors and
institution, then README, then wiki pages.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from wiki.models import WikiPage

from .models import (
    Contribution,
    Project,
    ProjectDeposit,
    ProjectDepositVersion,
    Tag,
    TagAssignment,
)


class SearchTestCase(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user(username="search-owner")

    def make(self, slug, **fields):
        fields.setdefault("title", slug.replace("-", " ").title())
        fields.setdefault("visibility", Project.VISIBILITY_PUBLIC)
        return Project.objects.create(slug=slug, created_by=self.owner, **fields)

    def results(self, q, **params):
        response = self.client.get(reverse("projects:list"), {"q": q, **params})
        self.assertEqual(response.status_code, 200)
        return [p.slug for p in response.context["projects"]]

    def api_results(self, q):
        response = self.client.get("/api/v1/projects/", {"q": q})
        self.assertEqual(response.status_code, 200)
        return [p["slug"] for p in response.json()]


class MatchingTests(SearchTestCase):
    def test_word_forms_match(self):
        self.make("seawater-pump", summary="A pump for seawater.")
        self.assertEqual(self.results("pumps"), ["seawater-pump"])
        self.assertEqual(self.results("pumping"), ["seawater-pump"])

    def test_partial_words_match(self):
        self.make("probe", readme="Uses a thermistor on a long lead.")
        self.assertEqual(self.results("therm"), ["probe"])

    def test_every_word_must_match(self):
        self.make("both", summary="Low power pump.")
        self.make("one", summary="Low power logger.")
        self.assertEqual(self.results("low power pump"), ["both"])

    def test_quoted_phrase_matches_exact_phrase(self):
        self.make("phrase", summary="A pressure sensor housing.")
        self.make("scattered", summary="A sensor for pressure housings.")
        self.assertEqual(self.results('"pressure sensor"'), ["phrase"])

    def test_minus_excludes_a_word(self):
        self.make("plain", summary="A tide logger.")
        self.make("arduino", summary="A tide logger on an Arduino.")
        self.assertEqual(self.results("logger -arduino"), ["plain"])

    def test_tags_contributors_institution_and_wiki_match(self):
        tagged = self.make("tagged")
        TagAssignment.objects.create(project=tagged, tag=Tag.objects.create(name="hydrophone"))
        credited = self.make("credited")
        Contribution.objects.create(project=credited, display_name="Anna Michel")
        self.make("institutional", institution="Woods Hole Oceanographic Institution")
        documented = self.make("documented")
        WikiPage.objects.create(project=documented, title="Assembly", body="Torque the flange bolts.")

        self.assertEqual(self.results("hydrophone"), ["tagged"])
        self.assertEqual(self.results("michel"), ["credited"])
        self.assertEqual(self.results("woods hole"), ["institutional"])
        self.assertEqual(self.results("flange"), ["documented"])

    def test_declined_contributors_are_not_searchable(self):
        project = self.make("declined")
        Contribution.objects.create(project=project, display_name="Zed Quimby", claim_status="declined")
        self.assertEqual(self.results("quimby"), [])

    def test_readme_image_and_link_urls_are_not_searchable(self):
        self.make(
            "pictured",
            readme="![Wiring](/media/projects/1/images/abc.webp)\n"
            "See [the datasheet](https://example.org/datasheets/widget.pdf).",
        )
        self.assertEqual(self.results("webp"), [])
        self.assertEqual(self.results("example"), [])
        self.assertEqual(self.results("wiring"), ["pictured"])
        self.assertEqual(self.results("datasheet"), ["pictured"])

    def test_punctuation_does_not_break_search(self):
        self.make("pump")
        for q in ["'", '"', "&|!():*<->", "pump & | !", "-", "''pump''"]:
            self.results(q)

    def test_filters_still_combine_with_search(self):
        self.make("ocean-pump", summary="A pump.", field="Oceanography")
        self.make("lab-pump", summary="A pump.", field="Chemistry")
        self.assertEqual(self.results("pump", field="Oceanography"), ["ocean-pump"])


class RankingTests(SearchTestCase):
    def test_title_match_outranks_repeated_readme_mentions(self):
        self.make("mentions", readme=" ".join(["The peristaltic head."] * 12))
        self.make("titled", title="Peristaltic Sampler")
        self.assertEqual(self.results("peristaltic"), ["titled", "mentions"])

    def test_summary_tags_and_people_outrank_readme(self):
        self.make("in-readme", readme="Built around a hydrophone.")
        tagged = self.make("tagged")
        TagAssignment.objects.create(project=tagged, tag=Tag.objects.create(name="hydrophone"))
        self.assertEqual(self.results("hydrophone"), ["tagged", "in-readme"])

    def test_wiki_ranks_below_readme(self):
        self.make("in-readme", readme="Torque the flange bolts.")
        in_wiki = self.make("in-wiki")
        WikiPage.objects.create(project=in_wiki, title="Assembly", body="Torque the flange bolts.")
        self.assertEqual(self.results("flange"), ["in-readme", "in-wiki"])

    def test_no_query_keeps_recently_updated_order(self):
        older = self.make("older")
        self.make("newer")
        Project.objects.filter(pk=older.pk).update(updated_at=timezone.now() - timezone.timedelta(days=3))
        self.assertEqual(self.results(""), ["newer", "older"])


class DoiTests(SearchTestCase):
    def test_pasted_doi_finds_its_project(self):
        self.make("own-doi", doi="10.5281/zenodo.111")
        deposited = self.make("deposited")
        deposit = ProjectDeposit.objects.create(
            project=deposited, doi="10.5281/zenodo.222", concept_doi="10.5281/zenodo.220"
        )
        ProjectDepositVersion.objects.create(
            deposit=deposit, version_index=1, doi="10.5281/zenodo.221", published_at=timezone.now()
        )
        self.make("other", summary="Mentions zenodo.")

        self.assertEqual(self.results("10.5281/zenodo.111"), ["own-doi"])
        self.assertEqual(self.results("https://doi.org/10.5281/zenodo.222"), ["deposited"])
        self.assertEqual(self.results("doi:10.5281/zenodo.220"), ["deposited"])
        self.assertEqual(self.results("10.5281/ZENODO.221"), ["deposited"])


class IndexFreshnessTests(SearchTestCase):
    def test_title_edit_is_searchable(self):
        project = self.make("renamed", title="Old Name")
        project.title = "Bathythermograph"
        project.save()
        self.assertEqual(self.results("bathythermograph"), ["renamed"])
        self.assertEqual(self.results("old name"), [])

    def test_tags_set_like_the_form_are_searchable(self):
        # ProjectForm.save writes tags with project.tags.set().
        project = self.make("form-tags")
        project.tags.set([Tag.objects.create(name="hydrophone")])
        self.assertEqual(self.results("hydrophone"), ["form-tags"])
        project.tags.clear()
        self.assertEqual(self.results("hydrophone"), [])

    def test_tag_rename_is_searchable(self):
        project = self.make("renamed-tag")
        tag = Tag.objects.create(name="hydrofone")
        TagAssignment.objects.create(project=project, tag=tag)
        tag.name = "hydrophone"
        tag.save()
        self.assertEqual(self.results("hydrophone"), ["renamed-tag"])

    def test_contributor_changes_are_searchable(self):
        project = self.make("people")
        row = Contribution.objects.create(project=project, display_name="Anna Michel")
        row.display_name = "Anna Kowalski"
        row.save()
        self.assertEqual(self.results("kowalski"), ["people"])
        self.assertEqual(self.results("michel"), [])
        row.delete()
        self.assertEqual(self.results("kowalski"), [])

    def test_wiki_changes_are_searchable(self):
        project = self.make("wiki-edits")
        page = WikiPage.objects.create(project=project, title="Assembly", body="Torque the flange.")
        page.body = "Seat the gasket."
        page.save()
        self.assertEqual(self.results("gasket"), ["wiki-edits"])
        self.assertEqual(self.results("flange"), [])
        page.delete()
        self.assertEqual(self.results("gasket"), [])


class VisibilityTests(SearchTestCase):
    def test_private_projects_stay_hidden(self):
        self.make("secret", title="Secret Pump", visibility=Project.VISIBILITY_PRIVATE)
        self.make("hidden", title="Hidden Pump", is_staff_hidden=True)
        self.assertEqual(self.results("pump"), [])
        self.assertEqual(self.api_results("pump"), [])
        self.client.force_login(self.owner)
        self.assertEqual(self.results("pump"), ["secret"])


class ApiParityTests(SearchTestCase):
    def test_api_and_website_agree(self):
        self.make("mentions", readme="The peristaltic head.")
        self.make("titled", title="Peristaltic Sampler")
        self.make("summarised", summary="A peristaltic pump.")
        self.assertEqual(self.api_results("peristaltic"), self.results("peristaltic"))
        self.assertEqual(self.results("peristaltic"), ["titled", "summarised", "mentions"])
