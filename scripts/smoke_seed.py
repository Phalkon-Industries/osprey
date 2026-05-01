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

User = get_user_model()

WHOI = "WHOI"

alice, created = User.objects.get_or_create(
    username="alice",
    defaults={"first_name": "Alice", "last_name": "Pfeifer", "email": "alice@example.org"},
)
if created:
    alice.set_password("demo")
    alice.save()
Profile.objects.filter(user=alice).update(
    display_name="Alice Pfeifer",
    institution=WHOI,
    orcid_placeholder="0000-0001-0000-0001",
)

bob, created = User.objects.get_or_create(
    username="bob",
    defaults={"first_name": "Bob", "last_name": "Hall", "email": "bob@example.org"},
)
if created:
    bob.set_password("demo")
    bob.save()
Profile.objects.filter(user=bob).update(
    display_name="Bob Hall",
    institution=WHOI,
    orcid_placeholder="0000-0001-0000-0002",
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
Woods Hole. Logs and bench data live in the upstream repository.
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
        "placeholder_doi": "10.demo/whoi-pump-v1",
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
        "placeholder_doi": "10.demo/whoi-pump-v2",
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
    orcid_id="0000-0001-0000-0001",
    defaults={"user": alice, "display_name": "Alice Researcher", "role": "Author", "order": 0},
)
Contribution.objects.update_or_create(
    project=parent,
    orcid_id="0000-0001-0000-0002",
    defaults={"user": bob, "display_name": "Bob Engineer", "role": "Contributor", "order": 1},
)
Contribution.objects.update_or_create(
    project=parent,
    orcid_id="0000-0009-9999-9999",
    defaults={
        "user": None,
        "display_name": "Carol Stub (no OSPREY account yet)",
        "role": "Advisor",
        "credit_statement": "Stub credit. Auto-links if Carol later registers with this ORCID iD.",
        "order": 2,
    },
)
Contribution.objects.update_or_create(
    project=child,
    orcid_id="0000-0001-0000-0001",
    defaults={"user": alice, "display_name": "Alice Researcher", "role": "Author", "order": 0},
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

LineageEdge.objects.get_or_create(
    parent=parent,
    child=child,
    relation="derived_from",
    defaults={"note": "Same pump head, redesigned electronics."},
)

# A demo image (a tiny solid-color PNG generated in-memory) and a citation,
# so the gallery and "Cited by" sections render with content.
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

Citation.objects.get_or_create(
    project=parent,
    text="Pfeifer, A. et al. Field deployment of the WHOI Pump v1. (demo citation)",
    defaults={
        "year": 2024,
        "doi": "10.demo/citation-001",
        "url": "https://example.org/papers/whoi-pump-v1",
    },
)

print("=== Seeded ===")
print(f"users:           {User.objects.count()}")
print(f"projects:        {Project.objects.count()} (public: {Project.objects.filter(visibility=Project.VISIBILITY_PUBLIC).count()})")
print(f"contributions:   {Contribution.objects.count()}")
print(f"artifact_links:  {ArtifactLink.objects.count()}")
print(f"lineage_edges:   {LineageEdge.objects.count()}")
print(f"tags:            {Tag.objects.count()}")
print(f"images:          {ProjectImage.objects.count()}")
print(f"citations:       {Citation.objects.count()}")
