"""Sweep a journal into the index, paced, resumable.

    manage.py index_journal hardwarex [--since 2017-01-01] [--limit N] [--pause 1.0] [--dry-run]
    manage.py index_journal joh

Lists the journal through its adapter, skips DOIs already indexed, resolves
each article, and creates live or held entries exactly as the staff page
does. Safe to rerun after an interruption. Leaves one inbox notice for
staff at the end.
"""
from __future__ import annotations

import time
from datetime import date

from django.core.management.base import BaseCommand, CommandError

from projects.indexing import service
from projects.indexing.records import SourceError
from projects.models import Project


class Command(BaseCommand):
    help = "Index every article a journal published since a date."

    def add_arguments(self, parser):
        parser.add_argument("source", choices=sorted(service.LISTERS))
        parser.add_argument("--since", help="YYYY-MM-DD. Default: the newest article already indexed, else 2017-01-01.")
        parser.add_argument("--limit", type=int, default=0, help="Stop after this many new articles (0 = all).")
        parser.add_argument("--pause", type=float, default=1.0, help="Seconds between articles.")
        parser.add_argument("--dry-run", action="store_true", help="Resolve and report; create nothing.")

    def handle(self, *args, **options):
        source = options["source"]
        if options["since"]:
            try:
                since = date.fromisoformat(options["since"])
            except ValueError as exc:
                raise CommandError("--since must be YYYY-MM-DD") from exc
        else:
            newest = (
                Project.objects.filter(source=source, published_on__isnull=False)
                .order_by("-published_on").values_list("published_on", flat=True).first()
            )
            since = newest or date(2017, 1, 1)
        try:
            dois = service.LISTERS[source](since)
        except SourceError as exc:
            raise CommandError(f"Couldn't list {source}: {exc}") from exc
        known = {e.lower() for e in Project.objects.filter(source=source).values_list("external_id", flat=True)}
        todo = [d for d in dois if d.lower() not in known]
        if options["limit"]:
            todo = todo[: options["limit"]]
        self.stdout.write(f"{source}: {len(dois)} listed since {since}, {len(todo)} new" + (" (dry run)" if options["dry_run"] else ""))
        results: list[tuple[service.Preview, Project | None]] = []
        counts = {"live": 0, "held": 0, "error": 0, "skipped": 0}
        started = time.monotonic()
        try:
            for n, doi in enumerate(todo, 1):
                preview = service.resolve(doi)
                if preview.can_import:
                    if options["dry_run"]:
                        counts[preview.outcome] += 1
                        reason = "" if preview.outcome == service.LIVE else " " + "; ".join(preview.reasons)
                        self.stdout.write(f"  {preview.outcome:5} {doi}  {preview.record.title[:60]}{reason}")
                    else:
                        project = service.index_record(preview.record)
                        results.append((preview, project))
                        counts[project.index_state] += 1
                elif preview.outcome == service.EXISTS:
                    counts["skipped"] += 1
                else:
                    counts["error"] += 1
                    self.stderr.write(f"  error {doi}: {preview.message}")
                if n % 10 == 0 or n == len(todo):
                    elapsed = int(time.monotonic() - started)
                    self.stdout.write(f"  {n}/{len(todo)}  live {counts['live']}  held {counts['held']}  errors {counts['error']}  {elapsed}s")
                if options["pause"] and n < len(todo):
                    time.sleep(options["pause"])
        except KeyboardInterrupt:
            self.stdout.write("interrupted; rerun to continue where this left off")
        if results:
            service.notify_staff_of_run(results)
        self.stdout.write(f"done: live {counts['live']}, held {counts['held']}, errors {counts['error']}, already indexed {counts['skipped']}")
