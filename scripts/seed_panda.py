"""Seed a 'Panda Driver Uno' project and publish it to Zenodo sandbox.

This script creates the project, attaches the v1 ZIP, publishes through
the real Zenodo sandbox API to mint version 1 with a real DOI, then
attaches the v2 ZIP, kicks off a Zenodo new-version, and publishes
again to mint version 2.

Run with:
    docker compose exec -T web python manage.py shell < scripts/seed_panda.py

Requires `ZENODO_ACCESS_TOKEN` and `ZENODO_USE_SANDBOX=1` to be set.
"""
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile

from projects.models import (
    Contribution,
    Project,
    ProjectAttachment,
    ProjectImage,
)
from projects.zenodo import publish_project_now, publish_new_version_now

User = get_user_model()
SLUG = "panda-driver-uno"
BASE = Path(settings.BASE_DIR) / "media" / "projects" / "panda-driver-uno"
V1_ZIP = BASE / "panda-driver-main.zip"
V2_ZIP = BASE / "panda-driver-main-v2.zip"
PCB_IMG = BASE / "pandadriver_pcb.jpg"

PANDA_README = """\
# Panda Driver Uno

A small Arduino-compatible motor driver shield for low-voltage DC motors.
Designed for classroom robotics where the bill of materials needs to stay
under ten dollars per board.

## Features

- Two-channel H-bridge with current sensing
- Reverse-polarity protection
- Footprint-compatible with the Uno R3
- KiCad sources, BOM, and assembly notes in the upstream repository

## Use

Stack the driver on an Arduino Uno R3, connect motor power to the screw
terminals, and run the example sketch.
""".strip()

PANDA_V1_CHANGELOG = """\
**v1.0 - initial release.**

- First fabricated batch of ten boards.
- KiCad sources, schematic PDF, and assembly notes.
- Tested with TT-motor classroom kits at 6 V.
""".strip()

PANDA_V2_CHANGELOG = """\
**v2.0 - fab-friendly revision.**

- Moved the current-sense resistors to 0805 to match the classroom
  reflow oven's minimum part size.
- Added a fused power input so a reversed motor lead no longer kills the
  shield.
- Updated the BOM to drop two unique parts; the v2 board can now be
  assembled from a single JLCPCB Basic-parts kit.
- Documentation: added a one-page assembly checklist for students.
""".strip()


def _ensure_owner():
    owner, _ = User.objects.get_or_create(
        username="alice",
        defaults={"first_name": "Alice", "last_name": "Pfeifer"},
    )
    if owner.has_usable_password():
        owner.set_unusable_password()
        owner.save(update_fields=["password"])
    return owner


def _ensure_project(owner):
    project, _created = Project.objects.get_or_create(
        slug=SLUG,
        defaults={
            "title": "Panda Driver Uno",
            "summary": "Cheap two-channel motor driver shield for the Arduino Uno.",
            "readme": PANDA_README,
            "field": "Electrical engineering",
            "artifact_type": "Hardware",
            "license": "CERN-OHL-S-2.0",
            "canonical_url": "https://github.com/example/panda-driver-uno",
            "institution": "WHOI",
            "visibility": Project.VISIBILITY_PRIVATE,
            "created_by": owner,
        },
    )
    Project.objects.filter(pk=project.pk).update(
        summary="Cheap two-channel motor driver shield for the Arduino Uno.",
        readme=PANDA_README,
        canonical_url="https://github.com/example/panda-driver-uno",
    )
    project.refresh_from_db()
    return project


def _attach_zip(project, path, label):
    if not path.is_file():
        print(f"WARN: {path} not found, skipping attachment.")
        return
    if project.attachments.filter(filename=path.name).exists():
        return
    with open(path, "rb") as fh:
        data = fh.read()
    ProjectAttachment.objects.create(
        project=project,
        file=ContentFile(data, name=path.name),
        filename=path.name,
        size_bytes=len(data),
        label=label,
    )


def main():
    if not settings.ZENODO_ACCESS_TOKEN:
        raise SystemExit("ZENODO_ACCESS_TOKEN not configured; refusing to seed.")
    if not settings.ZENODO_USE_SANDBOX:
        raise SystemExit("Refusing to run seed against production Zenodo.")

    owner = _ensure_owner()
    project = _ensure_project(owner)

    Contribution.objects.update_or_create(
        project=project,
        display_name="Alice Pfeifer",
        defaults={"user": owner, "role": "Project lead", "order": 0},
    )

    if PCB_IMG.is_file() and not project.images.filter(caption="Panda Driver Uno PCB").exists():
        with open(PCB_IMG, "rb") as fh:
            ProjectImage.objects.create(
                project=project,
                caption="Panda Driver Uno PCB",
                image=ContentFile(fh.read(), name=PCB_IMG.name),
            )

    deposit = project.deposits.filter(provider="zenodo").first()
    if deposit and deposit.state == "published":
        print(f"Project already published with deposit {deposit.deposition_id}; skipping initial publish.")
    else:
        _attach_zip(project, V1_ZIP, "v1.0 source archive")
        project.visibility = Project.VISIBILITY_PUBLIC
        project.save(update_fields=["visibility"])
        deposit = publish_project_now(project, owner)
        print(f"Published v1: deposition {deposit.deposition_id}, doi {deposit.doi}, concept {deposit.concept_doi}")

    if not deposit.versions.filter(version_index__gte=2).exists():
        _attach_zip(project, V2_ZIP, "v2.0 source archive")
        publish_new_version_now(
            deposit,
            changelog=PANDA_V2_CHANGELOG,
            repo_link="https://github.com/example/panda-driver-uno/releases/tag/v2.0",
            user=owner,
        )
        deposit.refresh_from_db()
        print(f"Published v2: latest doi {deposit.doi}, concept {deposit.concept_doi}")

    v1 = deposit.versions.filter(version_index=1).first()
    if v1 and not v1.changelog:
        v1.changelog = PANDA_V1_CHANGELOG
        v1.save(update_fields=["changelog"])
    project.refresh_from_db()
    print(f"Done. Project DOI: {project.doi}")


main()
