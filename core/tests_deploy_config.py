"""The deployed compose files: every container that runs OSPREY code
sees the same media volume. The notifier runs the Zenodo queue, which
uploads archives the web container saved; without the mount it looks in
an empty folder (found on prod 2026-10-07)."""
from __future__ import annotations

import re

from django.conf import settings
from django.test import SimpleTestCase


def _services(path) -> dict[str, str]:
    """Each service's block of text under `services:`, by name."""
    blocks: dict[str, list[str]] = {}
    current = None
    in_services = False
    for line in path.read_text().splitlines():
        if re.match(r"^\S", line):
            in_services = line.startswith("services:")
            current = None
            continue
        m = re.match(r"^  ([\w-]+):\s*$", line)
        if in_services and m:
            current = m.group(1)
            blocks[current] = []
        elif current:
            blocks[current].append(line)
    return {name: "\n".join(lines) for name, lines in blocks.items()}


class ComposeMediaTests(SimpleTestCase):
    def test_web_and_notifier_share_the_media_volume(self):
        for stack in ("prod", "sandbox"):
            with self.subTest(stack=stack):
                services = _services(settings.BASE_DIR / f"docker-compose.{stack}.yml")
                mount = f"/srv/osprey-{stack}/media:/app/media"
                self.assertIn(mount, services["web"])
                self.assertIn(mount, services["notifier"], f"the {stack} notifier can't see uploaded files")
