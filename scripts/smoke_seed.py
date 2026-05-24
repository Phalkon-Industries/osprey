"""Seed a tiny smoke-test dataset and print verification info.

Run with:
    docker compose exec -T web python manage.py shell < scripts/smoke_seed.py
"""

from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile

from people.models import Profile
from projects.models import (
    ArtifactLink,
    Citation,
    Contribution,
    LineageEdge,
    Project,
    ProjectImage,
    Tag,
    TagAssignment,
)
from attestations.models import Attestation
from wiki.models import WikiPage, WikiRevision

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

PUMP_V1_README = """\
## What it is

The WHOI Pump v1 is a peristaltic pump for **in-situ** seawater sampling at
depths down to ~500 m. The pump head is a stock part; everything around it
is original work: the pressure housing, the controller, the firmware, and
the deployment harness.

## Why we built it

Commercial in-situ pumps are expensive and rarely repairable in the field.
The v1 prototype was the cheapest thing we could build that didn't
compromise on flow rate or duty cycle.

## How to use it

1. Charge the battery (8.4 V LiFePO4, internal pack).
2. Set the duty cycle and total run time over USB before deployment.
3. Confirm the dry test passes (motor draws < 350 mA at 200 mL/min).
4. Deploy.

See `firmware/README.md` in the upstream repository for the full
configuration protocol.

## Known issues

- The seal stack on the v1 head leaks above 200 m. v2 fixes this.
- The status LED is internal to the housing. Useful, except when the
  housing is closed.
"""

PUMP_V2_README = """\
## What changed from v1

- New seal stack rated to 600 m. Tested on the bench at 700 m equivalent
  pressure for 24 hours.
- Lower-power motor driver. Standby draw dropped from 35 mA to 4 mA.
- External status LED ring on the end cap.

## Compatibility

v2 uses the same battery pack and the same deployment harness as v1.
Firmware is **not** backwards-compatible: the configuration protocol now
includes a hardware revision byte.

## Deployment notes

The v2 has been deployed three times so far, all from the R/V Tioga off
Martha's Vineyard.
"""

parent, _ = Project.objects.get_or_create(
    slug="whoi-pump-v1",
    defaults={
        "title": "WHOI Pump v1",
        "summary": "First-generation peristaltic pump for in-situ ocean sampling.",
        "description": "First-generation peristaltic pump used for in-situ sampling.",
        "readme": PUMP_V1_README,
        "artifact_type": "hardware",
        "field": "oceanography",
        "license": "CERN-OHL-S-2.0",
        "canonical_url": "https://github.com/example/whoi-pump-v1",
        "institution": WHOI,
        "visibility": Project.VISIBILITY_PUBLIC,
    },
)
Project.objects.filter(pk=parent.pk).update(
    visibility=Project.VISIBILITY_PUBLIC,
    readme=PUMP_V1_README,
    summary="First-generation peristaltic pump for in-situ ocean sampling.",
    institution=WHOI,
    cover_image_url="https://placehold.co/1200x600/0b3d5c/ffffff?text=WHOI+Pump+v1",
)

child, _ = Project.objects.get_or_create(
    slug="whoi-pump-v2",
    defaults={
        "title": "WHOI Pump v2",
        "summary": "Improved seal stack and lower power draw on the v1 design.",
        "description": "Revised pump with improved seal stack and lower power draw.",
        "readme": PUMP_V2_README,
        "artifact_type": "hardware",
        "field": "oceanography",
        "license": "CERN-OHL-S-2.0",
        "canonical_url": "https://github.com/example/whoi-pump-v2",
        "institution": WHOI,
        "visibility": Project.VISIBILITY_PUBLIC,
    },
)
Project.objects.filter(pk=child.pk).update(
    visibility=Project.VISIBILITY_PUBLIC,
    readme=PUMP_V2_README,
    summary="Improved seal stack and lower power draw on the v1 design.",
    institution=WHOI,
    cover_image_url="https://placehold.co/1200x600/0b3d5c/ffffff?text=WHOI+Pump+v2",
)

Contribution.objects.update_or_create(
    project=parent,
    display_name="Alice Researcher",
    defaults={"user": alice, "orcid_id": "", "role": "Project lead", "order": 0},
)
Contribution.objects.update_or_create(
    project=parent,
    display_name="Bob Engineer",
    defaults={"user": bob, "orcid_id": "", "role": "Hardware design", "order": 1},
)
Contribution.objects.update_or_create(
    project=parent,
    display_name="Carol Lab Lead",
    defaults={
        "user": None,
        "orcid_id": "",
        "role": "Principal investigator",
        "credit_statement": "Ran the lab effort and supported the field deployment.",
        "order": 2,
    },
)
Contribution.objects.update_or_create(
    project=child,
    display_name="Alice Researcher",
    defaults={"user": alice, "orcid_id": "", "role": "Maintainer", "order": 0},
)

ArtifactLink.objects.get_or_create(
    project=parent,
    url="https://github.com/example/whoi-pump-v1",
    defaults={"kind": "github", "label": "Source repository"},
)
ArtifactLink.objects.get_or_create(
    project=child,
    url="https://github.com/example/whoi-pump-v2",
    defaults={"kind": "github", "label": "Source repository"},
)
ArtifactLink.objects.get_or_create(
    project=child,
    url="https://zenodo.org/record/000000",
    defaults={"kind": "zenodo", "label": "Archived deposit (placeholder)"},
)

TagAssignment.objects.get_or_create(project=parent, tag=tag_pump)
TagAssignment.objects.get_or_create(project=child, tag=tag_pump)
TagAssignment.objects.get_or_create(project=child, tag=tag_co2)


# A demo image (a tiny solid-color PNG generated in-memory) so the gallery
# renders with content.
def _placeholder_png(color: tuple[int, int, int]) -> bytes:
    from PIL import Image

    img = Image.new("RGB", (640, 360), color)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if not parent.images.exists():
    ProjectImage.objects.create(
        project=parent,
        image=ContentFile(_placeholder_png((30, 64, 175)), name="whoi-pump-v1.png"),
        caption="Bench photo of the v1 pump head (placeholder).",
        order=0,
    )

if not child.images.exists():
    ProjectImage.objects.create(
        project=child,
        image=ContentFile(_placeholder_png((22, 101, 52)), name="whoi-pump-v2.png"),
        caption="Bench photo of the v2 pump head (placeholder).",
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
        "title": "Estuary Pump (derivative of WHOI Pump v1)",
        "summary": "Shallow-water reskin of the WHOI Pump v1 for estuary work.",
        "description": "A shallow-water variant of the WHOI pump for estuary deployments.",
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
    readme=DERIV_README,
    summary="Shallow-water reskin of the WHOI Pump v1 for estuary work.",
    institution="URI Graduate School of Oceanography",
    cover_image_url="https://placehold.co/1200x600/8b5e34/ffffff?text=Estuary+Pump",
    wiki_requires_approval=True,
)

Contribution.objects.update_or_create(
    project=deriv,
    display_name="Bob Hall",
    defaults={"user": bob, "orcid_id": "", "role": "Maintainer", "order": 0},
)

ArtifactLink.objects.get_or_create(
    project=deriv,
    url="https://github.com/example/estuary-pump",
    defaults={"kind": "github", "label": "Source repository"},
)

TagAssignment.objects.get_or_create(project=deriv, tag=tag_pump)


# --- Wiki configuration --------------------------------------------------------
# v1 has an open wiki; derivative requires approval. v2 inherits the project
# default, which is now approval-required.
Project.objects.filter(pk=parent.pk).update(wiki_requires_approval=False)
Project.objects.filter(pk=child.pk).update(wiki_requires_approval=True)


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
    parent,
    "overview",
    "Overview",
    "The v1 pump in one page: what it is, who built it, and how to reach the maintainers.",
    is_landing=True,
    author=alice,
)
_ensure_wiki(
    parent,
    "field-notes",
    "Field notes",
    "Open page. Anyone who has deployed the v1 in the field is welcome to add notes here.",
    author=bob,
)
_ensure_wiki(
    child,
    "overview",
    "Overview",
    "The v2 pump page. Edits to this wiki are reviewed before publishing.",
    is_landing=True,
    author=alice,
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
        + "\n\nSuggested addition: link back to the WHOI v1 wiki for the original seal-stack docs.",
        "summary": "Add link back to v1 seal-stack docs.",
    },
)


# --- Attestations --------------------------------------------------------------


def _ensure_attestation(
    project, author, narrative, used_at="", endorsement=Attestation.ENDORSE_NONE
):
    att, _ = Attestation.objects.get_or_create(
        project=project,
        author=author,
        narrative=narrative,
        defaults={"used_at": used_at, "endorsement": endorsement},
    )
    return att


att_v1 = _ensure_attestation(
    parent,
    bob,
    "We deployed two v1 units on a coastal mooring for six weeks. Both pulled clean samples; one had the known seal leak above 200 m.",
    used_at="2023 Vineyard Sound mooring",
    endorsement=Attestation.ENDORSE_FEATURED,
)
_ensure_attestation(
    parent,
    alice,
    "Used the v1 on a quick lab benchmark before committing to the v2 redesign. Power draw matched the spec within 8%.",
    used_at="2023 lab benchmark",
)
_ensure_attestation(
    child,
    bob,
    "Three deployments off the R/V Tioga. The seal stack held; the external LED is genuinely useful.",
    used_at="2024 Tioga cruises",
    endorsement=Attestation.ENDORSE_ACKNOWLEDGED,
)
_ensure_attestation(
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
    attestation=None,
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
            "attestation": attestation,
        },
    )
    return cit


_ensure_citation(
    parent,
    "Pfeifer, A. et al. (2023). A low-cost in-situ pump for coastal sampling. Ocean Engineering Letters.",
    doi="10.5555/example.001",
    year=2023,
    source=Citation.SOURCE_MAINTAINER,
)
_ensure_citation(
    parent,
    "Hall, B. (2023). Field notes from a six-week mooring deployment. Internal WHOI report.",
    year=2023,
    source=Citation.SOURCE_USER,
    submitted_by=bob,
)
_ensure_citation(
    parent,
    "Hall, B. (2023). Vineyard Sound mooring write-up referencing pump v1 deployment.",
    year=2023,
    source=Citation.SOURCE_ATTESTATION,
    submitted_by=bob,
    attestation=att_v1,
)
_ensure_citation(
    child,
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


# --- Lineage edges -------------------------------------------------------------

LineageEdge.objects.get_or_create(parent=parent, child=child, relation="replaces")
LineageEdge.objects.get_or_create(parent=parent, child=deriv, relation="derived_from")


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
print(f"attestations:    {Attestation.objects.count()}")
print(f"citations:       {Citation.objects.count()}")
print(f"lineage_edges:   {LineageEdge.objects.count()}")
