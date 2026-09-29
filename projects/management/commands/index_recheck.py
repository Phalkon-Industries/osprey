"""Re-read indexed entries from their sources and apply the current gates.

    manage.py index_recheck [--source hardwarex] [--state live|held] [--linked-only] [--pause 0.5] [--limit N]

Live entries that no longer pass are pulled back to held with the reason
on the entry; held entries are refreshed and stay held. --linked-only
restricts the pass to entries whose files link is a GitHub repository or
Zenodo record, where the license cross-check applies.
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand
from django.db.models import Q

from projects.indexing import service
from projects.models import Project


class Command(BaseCommand):
    help = "Re-read indexed entries and re-apply the gates."

    def add_arguments(self, parser):
        parser.add_argument("--source", choices=sorted(service.ADAPTERS))
        parser.add_argument("--state", choices=["live", "held"])
        parser.add_argument("--linked-only", action="store_true")
        parser.add_argument("--pause", type=float, default=0.5)
        parser.add_argument("--limit", type=int, default=0)
        parser.add_argument(
            "--relabel", action="store_true",
            help="No network: re-read stored declared license text on held entries with today's normalizer.",
        )
        parser.add_argument(
            "--promote-ready", action="store_true",
            help="No refetch: held entries imported by staff whose stored gates are all green go live.",
        )

    def handle(self, *args, **options):
        if options["relabel"]:
            from projects.indexing.records import INDEX_ACCEPTED_LICENSES, normalize_license
            from projects.indexing.hardwarex_source import hardware_license

            held = Project.objects.filter(origin=Project.ORIGIN_INDEXED, index_state=Project.INDEX_HELD)
            if options["source"]:
                held = held.filter(source=options["source"])
            fixed = 0
            for project in held:
                gate = dict(project.gate_license or {})
                if gate.get("ok") or not gate.get("found"):
                    continue
                raw = gate["found"].split(": open, but not")[0]
                key = hardware_license(raw) if project.source == "hardwarex" else normalize_license(raw)
                if key and key in INDEX_ACCEPTED_LICENSES:
                    project.license = key
                    gate.update(ok=True, confirm=False)
                    project.gate_license = gate
                    project.save(update_fields=["license", "gate_license"])
                    fixed += 1
            self.stdout.write(f"relabelled {fixed} held entries")
            return
        if options["promote_ready"]:
            held = Project.objects.filter(origin=Project.ORIGIN_INDEXED, index_state=Project.INDEX_HELD, listed_by__isnull=True)
            if options["source"]:
                held = held.filter(source=options["source"])
            promoted = 0
            for project in held:
                if service.review_state(project) == "ready":
                    project.index_state = Project.INDEX_LIVE
                    project.visibility = Project.VISIBILITY_PUBLIC
                    project.save(update_fields=["index_state", "visibility"])
                    promoted += 1
            self.stdout.write(f"promoted {promoted} ready staff-imported entries to live")
            return
        qs = Project.objects.filter(origin=Project.ORIGIN_INDEXED, source__in=sorted(service.ADAPTERS)).order_by("pk")
        if options["source"]:
            qs = qs.filter(source=options["source"])
        if options["state"]:
            qs = qs.filter(index_state=options["state"])
        if options["linked_only"]:
            qs = qs.filter(Q(files_url__icontains="github.com/") | Q(files_url__icontains="zenodo.org/") | Q(files_url__icontains="10.5281/zenodo."))
        rows = list(qs[: options["limit"]] if options["limit"] else qs)
        self.stdout.write(f"rechecking {len(rows)} entries")
        counts = {"still_live": 0, "pulled_back": 0, "held": 0, "errors": 0}
        for n, project in enumerate(rows, 1):
            was_live = project.index_state == Project.INDEX_LIVE
            preview = service.refresh_entry(project)
            if preview.outcome == service.ERROR:
                counts["errors"] += 1
                self.stderr.write(f"  error {project.external_id}: {preview.message}")
            elif was_live and preview.outcome == service.HELD:
                counts["pulled_back"] += 1
                self.stdout.write(f"  pulled back {project.external_id}: {' '.join(preview.reasons)}")
            elif was_live:
                counts["still_live"] += 1
            else:
                counts["held"] += 1
            if n % 25 == 0 or n == len(rows):
                self.stdout.write(f"  {n}/{len(rows)}  {counts}")
            if options["pause"] and n < len(rows):
                time.sleep(options["pause"])
        self.stdout.write(f"done: {counts}")
