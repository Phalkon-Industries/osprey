"""Seed a tiny smoke-test dataset and print verification info.

Run with:
    docker compose exec -T web python manage.py shell < scripts/smoke_seed.py
"""

from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile

from django.utils import timezone

from people.models import Profile
from projects.models import (
    ArtifactLink,
    Citation,
    Contribution,
    LineageEdge,
    Project,
    ProjectDeposit,
    ProjectDepositVersion,
    ProjectImage,
    Tag,
    TagAssignment,
)
from use_reports.models import UseReport
from wiki.models import WikiPage, WikiRevision

# Clean up the old two-project shape (whoi-pump-v1 + whoi-pump-v2 as siblings)
# left over from earlier seeds. The new shape collapses them into one project
# (slug `whoi-pump`) with two ProjectDepositVersion rows.
Project.objects.filter(slug__in=["whoi-pump-v1", "whoi-pump-v2"]).delete()

User = get_user_model()
WHOI = "WHOI"

alice, _ = User.objects.get_or_create(
    username="alice",
    defaults={
        "first_name": "Alice",
        "last_name": "Pfeifer",
        "email": "alice@example.org",
    },
)
if alice.has_usable_password():
    alice.set_unusable_password()
    alice.save(update_fields=["password"])
Profile.objects.filter(user=alice).update(
    display_name="Alice Pfeifer",
    institution=WHOI,
    orcid_placeholder="",
)

bob, _ = User.objects.get_or_create(
    username="bob",
    defaults={"first_name": "Bob", "last_name": "Hall", "email": "bob@example.org"},
)
if bob.has_usable_password():
    bob.set_unusable_password()
    bob.save(update_fields=["password"])
Profile.objects.filter(user=bob).update(
    display_name="Bob Hall",
    institution=WHOI,
    orcid_placeholder="",
)

tag_pump, _ = Tag.objects.get_or_create(name="pump")
tag_co2, _ = Tag.objects.get_or_create(name="co2-sensor")

PUMP_README = """\
## What it is

The WHOI Pump is a peristaltic pump for **in-situ** seawater sampling. The
pump head is a stock part; everything around it is original work: the
pressure housing, the controller, the firmware, and the deployment harness.

The current published version is v2. v1 is preserved as an earlier release
for anyone reproducing the original 2023 work.

## Why we built it

Commercial in-situ pumps are expensive and rarely repairable in the field.
The original v1 prototype was the cheapest thing we could build that
didn't compromise on flow rate or duty cycle. v2 fixes the v1 seal-stack
leak and cuts standby power.

## How to use it (v2)

1. Charge the battery (8.4 V LiFePO4, internal pack).
2. Set the duty cycle and total run time over USB before deployment.
3. Confirm the dry test passes (motor draws < 350 mA at 200 mL/min).
4. Deploy.

See `firmware/README.md` in the upstream repository for the full
configuration protocol. The v2 firmware is not backwards-compatible with
v1 hardware.

## Known issues

- The status LED ring on v2 is bright enough to wash out at the surface in
  daylight; consider a sun shroud.
- v1 hardware still in the field has the seal-stack leak above 200 m. The
  v2 redesign solves it but requires a head-and-housing swap.
"""

PUMP_V1_CHANGELOG = (
    "Initial release. Peristaltic pump head in an aluminium pressure housing "
    "rated to ~200 m. Known seal-stack leak above 200 m and an internal-only "
    "status LED."
)

PUMP_V2_CHANGELOG = (
    "- New seal stack rated to 600 m (24 hours at 700 m equivalent on the bench).\n"
    "- Lower-power motor driver. Standby draw dropped from 35 mA to 4 mA.\n"
    "- External status LED ring on the end cap.\n"
    "- Configuration protocol now includes a hardware revision byte; firmware "
    "  is not backwards-compatible with v1 hardware."
)

pump, _ = Project.objects.get_or_create(
    slug="whoi-pump",
    defaults={
        "title": "WHOI Pump",
        "summary": "Peristaltic pump for in-situ ocean sampling. v2 is the current release.",
        "description": "Peristaltic pump for in-situ seawater sampling. Versioned history (v1, v2) preserved through Zenodo.",
        "readme": PUMP_README,
        "artifact_type": "hardware",
        "field": "oceanography",
        "license": "CERN-OHL-S-2.0",
        "canonical_url": "https://github.com/example/whoi-pump",
        "institution": WHOI,
        "visibility": Project.VISIBILITY_PUBLIC,
    },
)
Project.objects.filter(pk=pump.pk).update(
    title="WHOI Pump",
    created_by=alice,
    visibility=Project.VISIBILITY_PUBLIC,
    readme=PUMP_README,
    summary="Peristaltic pump for in-situ ocean sampling. v2 is the current release.",
    institution=WHOI,
    canonical_url="https://github.com/example/whoi-pump",
    doi="10.5281/zenodo.99000",
    cover_image_url="",
    self_rating=8,
    self_rating_note=(
        "Two release generations field-deployed off the R/V Tioga and on Vineyard Sound moorings. "
        "v2 seal stack and external LED resolved the main v1 pain points; power draw cut by ~30%. "
        "Still wants more bench time on the new MCU firmware before I'd call it solid above 600 m."
    ),
    funding="WHOI internal seed funding (2022)\nNSF OCE-2099999 (PI: C. Lab Lead)\nWHOI Ocean Observatories supplement (2024)",
)
pump.refresh_from_db()

# Fake Zenodo deposit + two versions so the version history demo has shape.
# DOIs are placeholders — not real Zenodo records — used for UI testing.
pump_deposit, _ = ProjectDeposit.objects.update_or_create(
    project=pump,
    provider=ProjectDeposit.PROVIDER_ZENODO,
    sandbox=True,
    defaults={
        "deposition_id": "99002",
        "record_id": "99002",
        "concept_id": "99000",
        "doi": "10.5281/zenodo.99002",
        "concept_doi": "10.5281/zenodo.99000",
        "state": ProjectDeposit.STATE_PUBLISHED,
        "created_by": alice,
        "published_at": timezone.now(),
    },
)
ProjectDepositVersion.objects.update_or_create(
    deposit=pump_deposit,
    version_index=1,
    defaults={
        "deposition_id": "99001",
        "record_id": "99001",
        "doi": "10.5281/zenodo.99001",
        "changelog": PUMP_V1_CHANGELOG,
        "repo_link": "https://github.com/example/whoi-pump/releases/tag/v1.0",
        "published_at": timezone.now(),
    },
)
ProjectDepositVersion.objects.update_or_create(
    deposit=pump_deposit,
    version_index=2,
    defaults={
        "deposition_id": "99002",
        "record_id": "99002",
        "doi": "10.5281/zenodo.99002",
        "changelog": PUMP_V2_CHANGELOG,
        "repo_link": "https://github.com/example/whoi-pump/releases/tag/v2.0",
        "published_at": timezone.now(),
    },
)

Contribution.objects.update_or_create(
    project=pump,
    display_name="Alice Researcher",
    defaults={"user": alice, "orcid_id": "", "role": "Project lead", "order": 0},
)
Contribution.objects.update_or_create(
    project=pump,
    display_name="Alice Researcher",
    defaults={"user": alice, "orcid_id": "", "role": "Project lead", "order": 0},
)
Contribution.objects.update_or_create(
    project=pump,
    display_name="Bob Engineer",
    defaults={"user": bob, "orcid_id": "", "role": "Hardware design", "order": 1},
)
Contribution.objects.update_or_create(
    project=pump,
    display_name="Carol Lab Lead",
    defaults={
        "user": None,
        "orcid_id": "",
        "role": "Principal investigator",
        "credit_statement": "Ran the lab effort and supported the field deployment.",
        "order": 2,
    },
)

# No duplicate github ArtifactLink: the project's canonical_url already covers
# the source repository. Only add non-canonical artifact links here.

TagAssignment.objects.get_or_create(project=pump, tag=tag_pump)
TagAssignment.objects.get_or_create(project=pump, tag=tag_co2)


# A demo image (a tiny solid-color PNG generated in-memory) so the gallery
# renders with content.
def _placeholder_png(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (640, 360), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if not pump.images.exists():
    ProjectImage.objects.create(
        project=pump,
        image=ContentFile(_placeholder_png((30, 64, 175)), name="whoi-pump.png"),
        caption="Bench photo of the WHOI pump head (placeholder).",
        order=0,
    )


# --- Derivative project from a different team ---------------------------------

DERIV_README = """\
## What it is

A field-portable variant of the WHOI Pump v1, repackaged for shallow
estuary deployments where the pressure housing is overkill. Same pump
head, lighter electronics, surface-tethered.

## What changed from the WHOI Pump

- Pressure housing replaced with a splash-rated enclosure.
- Battery moved topside; tether carries power and serial.
- Controller swapped to a smaller MCU. Firmware is a near-rewrite.

The mechanical pump head, the flow profile, and the seal stack are the
same as the WHOI v1.
"""

deriv, _ = Project.objects.get_or_create(
    slug="whoi-pump-derivative",
    defaults={
        "title": "Estuary Pump",
        "summary": "Splash-rated peristaltic pump for shallow estuary deployments.",
        "description": "A shallow-water peristaltic pump for estuary deployments.",
        "readme": DERIV_README,
        "artifact_type": "hardware",
        "field": "oceanography",
        "license": "CERN-OHL-S-2.0",
        "canonical_url": "https://github.com/example/estuary-pump",
        "institution": "URI Graduate School of Oceanography",
        "visibility": Project.VISIBILITY_PUBLIC,
    },
)
Project.objects.filter(pk=deriv.pk).update(
    visibility=Project.VISIBILITY_PUBLIC,
    created_by=bob,
    readme=DERIV_README,
    summary="Splash-rated peristaltic pump for shallow estuary deployments.",
    institution="URI Graduate School of Oceanography",
    cover_image_url="",
    wiki_requires_approval=True,
    self_rating=5,
    self_rating_note=(
        "One season of estuary deployments in Narragansett Bay. Splash enclosure leaked on a single deployment; "
        "the surface tether is the weak link. Mechanical pump head is unchanged from the WHOI v1 and is solid."
    ),
    funding="URI startup funds (2024)",
)

Contribution.objects.update_or_create(
    project=deriv,
    display_name="Bob Hall",
    defaults={"user": bob, "orcid_id": "", "role": "Maintainer", "order": 0},
)

# No duplicate github ArtifactLink: canonical_url covers it.

TagAssignment.objects.get_or_create(project=deriv, tag=tag_pump)


# --- Wiki configuration --------------------------------------------------------
# The pump wiki is open; the derivative wiki requires approval before edits land.
Project.objects.filter(pk=pump.pk).update(wiki_requires_approval=False)


def _ensure_wiki(project, slug, title, body, *, is_landing=False, author=None):
    page, created = WikiPage.objects.get_or_create(
        project=project,
        slug=slug,
        defaults={
            "title": title,
            "body": body,
            "is_landing": is_landing,
            "last_edited_by": author,
        },
    )
    if not created:
        page.title = title
        page.body = body
        page.is_landing = is_landing
        page.last_edited_by = author
        page.save()
    WikiRevision.objects.get_or_create(
        page=page,
        title=title,
        body=body,
        defaults={
            "author": author,
            "status": WikiRevision.STATUS_APPLIED,
            "summary": "Seed import",
        },
    )
    return page


_ensure_wiki(
    pump,
    "overview",
    "Overview",
    "The WHOI Pump in one page: what it is, who built it, and how to reach the maintainers.",
    is_landing=True,
    author=alice,
)
_ensure_wiki(
    pump,
    "field-notes",
    "Field notes",
    "Open page. Anyone who has deployed the pump in the field is welcome to add notes here.",
    author=bob,
)
_ensure_wiki(
    deriv,
    "overview",
    "Overview",
    "The estuary variant. Maintainer-curated wiki; suggestions are reviewed before they are accepted.",
    is_landing=True,
    author=bob,
)

# A pending suggestion on the derivative wiki so the public pending-visibility
# section has something to show.
deriv_overview = WikiPage.objects.get(project=deriv, slug="overview")
WikiRevision.objects.get_or_create(
    page=deriv_overview,
    author=alice,
    status=WikiRevision.STATUS_PENDING,
    defaults={
        "title": deriv_overview.title,
        "body": deriv_overview.body
        + "\n\nSuggested addition: link back to the WHOI Pump wiki for the original seal-stack docs.",
        "summary": "Add link back to WHOI Pump seal-stack docs.",
    },
)


# --- Use reports ---------------------------------------------------------------


def _ensure_use_report(project, author, narrative, used_at=""):
    report, _ = UseReport.objects.get_or_create(
        project=project,
        author=author,
        narrative=narrative,
        defaults={"used_at": used_at},
    )
    return report


report_pump = _ensure_use_report(
    pump,
    bob,
    "We deployed two v1 units on a coastal mooring for six weeks. Both pulled clean samples; one had the known seal leak above 200 m.",
    used_at="2023 Vineyard Sound mooring",
)
_ensure_use_report(
    pump,
    alice,
    "Used the v1 on a quick lab benchmark before committing to the v2 redesign. Power draw matched the spec within 8%.",
    used_at="2023 lab benchmark",
)
_ensure_use_report(
    pump,
    bob,
    "Three v2 deployments off the R/V Tioga. The new seal stack held; the external LED is genuinely useful.",
    used_at="2024 Tioga cruises",
)
_ensure_use_report(
    deriv,
    alice,
    "Borrowed an Estuary Pump for a Buzzards Bay survey. Tether handling was awkward from a small skiff but the pump itself worked.",
    used_at="2024 Buzzards Bay skiff survey",
)


# --- Citations -----------------------------------------------------------------


def _ensure_citation(
    project,
    text,
    *,
    doi="",
    url="",
    year=None,
    source=Citation.SOURCE_MAINTAINER,
    submitted_by=None,
    use_report=None,
):
    cit, _ = Citation.objects.get_or_create(
        project=project,
        text=text,
        defaults={
            "doi": doi,
            "url": url,
            "year": year,
            "source": source,
            "submitted_by": submitted_by,
            "use_report": use_report,
        },
    )
    return cit


_ensure_citation(
    pump,
    "Pfeifer, A. et al. (2023). A low-cost in-situ pump for coastal sampling. Ocean Engineering Letters.",
    doi="10.5555/example.001",
    year=2023,
    source=Citation.SOURCE_MAINTAINER,
)
_ensure_citation(
    pump,
    "Hall, B. (2023). Field notes from a six-week mooring deployment. Internal WHOI report.",
    year=2023,
    source=Citation.SOURCE_USER,
    submitted_by=bob,
)
_ensure_citation(
    pump,
    "Hall, B. (2023). Vineyard Sound mooring write-up referencing pump v1 deployment.",
    year=2023,
    source=Citation.SOURCE_USE_REPORT,
    submitted_by=bob,
    use_report=report_pump,
)
_ensure_citation(
    pump,
    "Pfeifer, A. (2024). Revised seal stack performance in the WHOI Pump v2. WHOI tech report 2024-03.",
    year=2024,
    source=Citation.SOURCE_MAINTAINER,
)
_ensure_citation(
    deriv,
    "Hall, B. (2024). Estuary Pump: a shallow-water variant of the WHOI Pump. URI GSO working paper.",
    year=2024,
    source=Citation.SOURCE_MAINTAINER,
)


# --- Additional projects to exercise every lineage edge type -----------------

PUMP_MARK_I_README = """\
The original 2018 prototype: a bench-top peristaltic pump with no pressure
housing and a hand-soldered controller. Retired and replaced by the
WHOI Pump in 2023, but kept here so the early design choices and the
mistakes that drove the redesign stay on the public record.
"""

pump_mark_i, _ = Project.objects.get_or_create(
    slug="whoi-pump-mark-i",
    defaults={
        "title": "WHOI Pump Mark I",
        "summary": "Bench-top peristaltic pump prototype from 2018.",
        "description": "2018 bench prototype of a peristaltic pump.",
        "readme": PUMP_MARK_I_README,
        "artifact_type": "hardware",
        "field": "oceanography",
        "license": "CERN-OHL-S-2.0",
        "canonical_url": "https://github.com/example/whoi-pump-mark-i",
        "institution": WHOI,
        "visibility": Project.VISIBILITY_PUBLIC,
    },
)
Project.objects.filter(pk=pump_mark_i.pk).update(
    visibility=Project.VISIBILITY_PUBLIC,
    created_by=alice,
    summary="Bench-top peristaltic pump prototype from 2018.",
    institution=WHOI,
    cover_image_url="",
    self_rating=2,
    self_rating_note="Archival. Listed for historical record.",
)
Contribution.objects.update_or_create(
    project=pump_mark_i,
    display_name="Alice Researcher",
    defaults={"user": alice, "role": "Original designer", "order": 0},
)
TagAssignment.objects.get_or_create(project=pump_mark_i, tag=tag_pump)


OSH_PUMP_README = """\
A community open-source peristaltic pump from the OSH (Open Source
Hardware) ecosystem, c. 2016. Cited here as design inspiration for the
WHOI Pump. The mechanical layout, the open-license stance, and the
philosophy of repairable field hardware all trace back to projects like
this one.
"""

osh_pump, _ = Project.objects.get_or_create(
    slug="osh-peristaltic-pump",
    defaults={
        "title": "OSH Peristaltic Pump",
        "summary": "Community open-source peristaltic pump reference design.",
        "description": "Community open-source peristaltic pump.",
        "readme": OSH_PUMP_README,
        "artifact_type": "hardware",
        "field": "open hardware",
        "license": "CERN-OHL-S-2.0",
        "canonical_url": "https://github.com/example/osh-peristaltic-pump",
        "institution": "Open Source Hardware Association",
        "visibility": Project.VISIBILITY_PUBLIC,
    },
)
Project.objects.filter(pk=osh_pump.pk).update(
    visibility=Project.VISIBILITY_PUBLIC,
    summary="Community open-source peristaltic pump reference design.",
    institution="Open Source Hardware Association",
    cover_image_url="",
    self_rating=6,
    self_rating_note="Catalog entry only; not maintained on OSPREY.",
)
Contribution.objects.update_or_create(
    project=osh_pump,
    display_name="Community contributors",
    defaults={"user": None, "role": "Original authors", "order": 0},
)
TagAssignment.objects.get_or_create(project=osh_pump, tag=tag_pump)


FIRMWARE_FORK_README = """\
Experimental fork of the WHOI Pump firmware that swaps the PID loop for
an adaptive sliding-mode controller. Lives off the main branch on
purpose: the goal is to learn whether the new controller is worth
folding back, not to ship it.
"""

firmware_fork, _ = Project.objects.get_or_create(
    slug="whoi-pump-firmware-experimental",
    defaults={
        "title": "Sliding-Mode Pump Firmware",
        "summary": "Pump controller firmware using an adaptive sliding-mode flow controller.",
        "description": "Pump firmware exploring a sliding-mode flow controller.",
        "readme": FIRMWARE_FORK_README,
        "artifact_type": "firmware",
        "field": "oceanography",
        "license": "MIT",
        "canonical_url": "https://github.com/example/whoi-pump-firmware-experimental",
        "institution": "MIT",
        "visibility": Project.VISIBILITY_PUBLIC,
    },
)
Project.objects.filter(pk=firmware_fork.pk).update(
    visibility=Project.VISIBILITY_PUBLIC,
    created_by=bob,
    summary="Pump controller firmware using an adaptive sliding-mode flow controller.",
    institution="MIT",
    cover_image_url="",
    self_rating=3,
    self_rating_note="Bench only. Controller is unstable above 250 mL/min.",
)
Contribution.objects.update_or_create(
    project=firmware_fork,
    display_name="Bob Hall",
    defaults={"user": bob, "role": "Maintainer of the fork", "order": 0},
)
TagAssignment.objects.get_or_create(project=firmware_fork, tag=tag_pump)


# --- Lineage demo chain ----------------------------------------------------
# Four generations deep (Tide Logger -> Reef Tide Logger -> Reef Array
# Controller -> Reef Array Shore Kit), with version pins going both ways:
# the reef fork starts from Tide Logger v1, and Tide Logger v2 then pulls
# the reef improvements back in. Exercises the graph, the pins, and both
# relation kinds.


def _demo_project(slug, title, summary, owner, rating, note):
    project, _ = Project.objects.get_or_create(
        slug=slug,
        defaults={
            "title": title,
            "summary": summary,
            "readme": f"# {title}\n\n{summary}",
            "artifact_type": "hardware",
            "field": "oceanography",
            "license": "CERN-OHL-S-2.0",
            "institution": WHOI,
            "visibility": Project.VISIBILITY_PUBLIC,
            "created_by": owner,
            "self_rating": rating,
            "self_rating_note": note,
        },
    )
    Project.objects.filter(pk=project.pk).update(
        visibility=Project.VISIBILITY_PUBLIC, created_by=owner
    )
    project.refresh_from_db()
    return project


def _demo_versions(project, base_id, count, owner):
    deposit, _ = ProjectDeposit.objects.update_or_create(
        project=project,
        provider=ProjectDeposit.PROVIDER_ZENODO,
        sandbox=True,
        defaults={
            "deposition_id": str(base_id + count - 1),
            "record_id": str(base_id + count - 1),
            "concept_id": str(base_id),
            "doi": f"10.5281/zenodo.{base_id + count - 1}",
            "concept_doi": f"10.5281/zenodo.{base_id}",
            "state": ProjectDeposit.STATE_PUBLISHED,
            "created_by": owner,
            "published_at": timezone.now(),
        },
    )
    versions = []
    for index in range(1, count + 1):
        version, _ = ProjectDepositVersion.objects.update_or_create(
            deposit=deposit,
            version_index=index,
            defaults={
                "deposition_id": str(base_id + index - 1),
                "record_id": str(base_id + index - 1),
                "doi": f"10.5281/zenodo.{base_id + index - 1}",
                "changelog": f"v{index} release.",
                "published_at": timezone.now(),
            },
        )
        versions.append(version)
    return versions


def _demo_edge(parent, child, relation, parent_version, child_version, declared_by):
    LineageEdge.objects.get_or_create(
        parent=parent,
        child=child,
        relation=relation,
        parent_version=parent_version,
        child_version=child_version,
        defaults={
            "declared_by": declared_by,
            "claimed_at": timezone.now(),
            "status": "active",
        },
    )


tide = _demo_project(
    "tide-logger",
    "Tide Logger Mk I",
    "Self-contained tide logger for harbor moorings.",
    alice,
    7,
    "Two seasons on the Woods Hole town dock.",
)
reef = _demo_project(
    "tide-logger-reef",
    "Reef Tide Logger",
    "Tide Logger reworked for reef-flat deployments.",
    bob,
    5,
    "Survived one field season on the test reef.",
)
array_controller = _demo_project(
    "reef-array-controller",
    "Reef Array Controller",
    "Controller that coordinates a grid of reef tide loggers.",
    bob,
    4,
    "Bench-tested with six loggers on the wall.",
)
shore_kit = _demo_project(
    "reef-array-shore-kit",
    "Reef Array Shore Kit",
    "Shore-station adaptation of the array controller.",
    alice,
    3,
    "First prototype boxed up, not yet deployed.",
)

tide_versions = _demo_versions(tide, 98100, 2, alice)
reef_versions = _demo_versions(reef, 98200, 2, bob)
array_versions = _demo_versions(array_controller, 98300, 1, bob)
shore_versions = _demo_versions(shore_kit, 98400, 1, alice)

# The reef fork started from Tide Logger v1.
_demo_edge(tide, reef, "derived_from", tide_versions[0], reef_versions[0], bob)
# Tide Logger v2 pulled the reef improvements back in.
_demo_edge(reef, tide, "derived_from", reef_versions[0], tide_versions[1], alice)
# The array controller drives reef loggers as components (their v2).
_demo_edge(reef, array_controller, "uses", reef_versions[1], array_versions[0], bob)
# The shore kit is an adaptation of the controller.
_demo_edge(
    array_controller,
    shore_kit,
    "derived_from",
    array_versions[0],
    shore_versions[0],
    alice,
)


# --- Pump family lineage (pinned) ------------------------------------------
# The original pump-family edges predate version pins; recreate them as
# proper pinned, live claims. Old unpinned seed rows are cleaned up so a
# reseeded database doesn't carry both shapes.
_pump_family = [pump, deriv, pump_mark_i, osh_pump, firmware_fork]
LineageEdge.objects.filter(
    parent__in=_pump_family,
    child__in=_pump_family,
    parent_version__isnull=True,
).delete()

mark_i_versions = _demo_versions(pump_mark_i, 98500, 1, alice)
deriv_versions = _demo_versions(deriv, 98600, 1, bob)
osh_versions = _demo_versions(osh_pump, 98700, 1, alice)
fork_versions = _demo_versions(firmware_fork, 98800, 1, bob)
pump_v1 = ProjectDepositVersion.objects.get(deposit=pump_deposit, version_index=1)
pump_v2 = ProjectDepositVersion.objects.get(deposit=pump_deposit, version_index=2)

# The estuary pump kept the v1 mechanical head.
_demo_edge(pump, deriv, "derived_from", pump_v1, deriv_versions[0], bob)
# The pump grew out of the 2018 bench prototype.
_demo_edge(pump_mark_i, pump, "derived_from", mark_i_versions[0], pump_v1, alice)
# The community pump's mechanical layout is used in the WHOI design.
_demo_edge(osh_pump, pump, "uses", osh_versions[0], pump_v1, alice)
# The experimental firmware forked off the v2 release.
_demo_edge(pump, firmware_fork, "derived_from", pump_v2, fork_versions[0], bob)


# Seed rows created with a user attached predate the claim flow; mark
# them verified so the pages render the same on a fresh database as on
# a migrated one.
Contribution.objects.filter(user__isnull=False, claim_status="unclaimed").update(
    claim_status="verified", editor=True
)

print("=== Seeded ===")
print(f"users:           {User.objects.count()}")
print(
    f"projects:        {Project.objects.count()} (public: {Project.objects.filter(visibility=Project.VISIBILITY_PUBLIC).count()})"
)
print(f"contributions:   {Contribution.objects.count()}")
print(f"artifact_links:  {ArtifactLink.objects.count()}")
print(f"tags:            {Tag.objects.count()}")
print(f"images:          {ProjectImage.objects.count()}")
print(f"wiki_pages:      {WikiPage.objects.count()}")
print(f"use reports:     {UseReport.objects.count()}")
print(f"citations:       {Citation.objects.count()}")
print(f"lineage_edges:   {LineageEdge.objects.count()}")


# --- A finished instrument nobody maintains: provided as-is ---------------
TIDE_README = """\
Tide gauge logger built for a 2019 field season: a pressure sensor, a
low-power board, and a housing that survived a winter. It worked, the
season ended, and nobody is developing it further. The design is
complete as of this version.
"""
tide_owner = Project.objects.get(slug="whoi-pump").created_by
tide, _ = Project.objects.get_or_create(
    slug="tide-gauge-logger-2019",
    defaults={
        "title": "Tide Gauge Logger (2019 season)",
        "summary": "A pressure-based tide gauge logger from a finished field season. Provided as-is.",
        "readme": TIDE_README,
        "artifact_type": "hardware",
        "field": "oceanography",
        "license": "CERN-OHL-P-2.0",
        "institution": WHOI,
        "visibility": Project.VISIBILITY_PUBLIC,
        "created_by": tide_owner,
        "self_rating": 7,
        "self_rating_note": "Deployed for one season; documentation complete.",
    },
)
Project.objects.filter(pk=tide.pk).update(
    visibility=Project.VISIBILITY_PUBLIC, created_by=tide_owner, provided_as_is=True, wiki_requires_approval=False
)
Contribution.objects.update_or_create(
    project=tide, display_name="Field Team 2019", defaults={"user": None, "role": "Design and deployment", "order": 0}
)


# --- A project at the image limit: 17 gallery images, 3 in the README -----
import io as _io
import math as _math

from django.core.files.uploadedfile import SimpleUploadedFile as _Upload
from PIL import Image as _Image, ImageDraw as _Draw

from projects import images as _images


def _test_image(w, h, hue, label):
    """A photo-like test card: a gradient, a few shapes, and its own size
    written on it, so aspect handling is easy to judge by eye."""
    im = _Image.new("RGB", (w, h))
    px = im.load()
    r0, g0, b0 = [int(127 + 120 * _math.sin(hue + k)) for k in (0, 2.1, 4.2)]
    for y in range(h):
        t = y / max(h - 1, 1)
        row = (int(r0 * (1 - t) + 20 * t), int(g0 * (1 - t) + 30 * t), int(b0 * (1 - t) + 60 * t))
        for x in range(0, w, 1):
            px[x, y] = row
    d = _Draw.Draw(im)
    m = min(w, h)
    d.ellipse([w * 0.1, h * 0.15, w * 0.1 + m * 0.4, h * 0.15 + m * 0.4], outline=(255, 255, 255), width=max(2, m // 120))
    d.rectangle([w * 0.55, h * 0.55, w * 0.9, h * 0.9], outline=(255, 255, 255), width=max(2, m // 120))
    d.line([0, h - 1, w - 1, 0], fill=(255, 255, 255), width=max(1, m // 300))
    d.text((m * 0.05, m * 0.05), label, fill=(255, 255, 255), font_size=max(14, m // 14))
    buf = _io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return _Upload(f"{label.replace(' ', '_')}.jpg", buf.getvalue(), content_type="image/jpeg")


gallery_owner = Project.objects.get(slug="whoi-pump").created_by
rig, _ = Project.objects.get_or_create(
    slug="image-limit-test-rig",
    defaults={
        "title": "Image Limit Test Rig",
        "summary": "Twenty images in every common shape, for checking the gallery, the cover crop and README images.",
        "readme": "",
        "artifact_type": "hardware",
        "field": "oceanography",
        "license": "CERN-OHL-S-2.0",
        "institution": WHOI,
        "visibility": Project.VISIBILITY_PUBLIC,
        "created_by": gallery_owner,
        "self_rating": 5,
    },
)
Project.objects.filter(pk=rig.pk).update(visibility=Project.VISIBILITY_PUBLIC, created_by=gallery_owner)
Contribution.objects.update_or_create(
    project=rig, display_name="Bench Team", defaults={"user": None, "role": "Testing", "order": 0}
)
if rig.images.count() < _images.MAX_IMAGES:
    for old in rig.images.all():
        _images.delete_files(old)
        old.delete()
    shapes = [
        (1920, 1080, "16x9 landscape"), (1200, 900, "4x3 landscape"), (1500, 1000, "3x2 landscape"),
        (1200, 1200, "1x1 square"), (1080, 1920, "9x16 portrait"), (900, 1200, "3x4 portrait"),
        (2100, 900, "21x9 wide"), (3600, 900, "4x1 panorama"), (800, 2400, "1x3 tall"),
        (1080, 1350, "4x5 portrait"), (1000, 1500, "2x3 portrait"), (1280, 1024, "5x4 landscape"),
        (2000, 1000, "2x1 landscape"), (640, 480, "small 640x480"), (4000, 3000, "large 4000x3000"),
        (500, 500, "small square"), (1600, 1200, "4x3 bench photo"),
    ]
    for i, (w, h, label) in enumerate(shapes):
        _images.store(rig, _test_image(w, h, i * 0.7, label), kind="gallery", caption=label if i % 3 == 0 else "")
    readme_shapes = [(1200, 600, "wiring diagram 2x1"), (800, 800, "schematic 1x1"), (1000, 400, "chart 5x2")]
    readme_imgs = [
        _images.store(rig, _test_image(w, h, 3 + i, label), kind="readme", caption=label)
        for i, (w, h, label) in enumerate(readme_shapes)
    ]
    Project.objects.filter(pk=rig.pk).update(readme=(
        "# Image Limit Test Rig\n\n"
        "A bench rig used to check how OSPREY handles images. The gallery holds seventeen images "
        "in every common shape; three more live in this README.\n\n"
        f"## Wiring\n\n![{readme_imgs[0].caption}]({readme_imgs[0].image.url})\n\n"
        f"## Schematic\n\n![{readme_imgs[1].caption}]({readme_imgs[1].image.url})\n\n"
        f"## Results\n\n![{readme_imgs[2].caption}]({readme_imgs[2].image.url})\n"
    ))
print("image test rig:", rig.images.filter(kind="gallery").count(), "gallery,", rig.images.filter(kind="readme").count(), "README")
