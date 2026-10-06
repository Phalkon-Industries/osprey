"""Generate the three WebP sizes for every project image that is missing
one, from the largest stored copy: the covers migration 0040 moved into the
image set, and any image stored before the sizes existed. Images that
already have all three sizes are left alone, so the command is safe to
rerun. --force re-encodes every image at the current quality setting; use
it after changing QUALITY in projects/images.py and not otherwise, since
each pass re-encodes lossy WebP.

    manage.py reencode_images [--project slug] [--force] [--dry-run]
"""
from __future__ import annotations

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from projects import images
from projects.models import ProjectImage


class Command(BaseCommand):
    help = "Generate missing project image sizes; --force re-encodes every image at the current WebP quality."

    def add_arguments(self, parser):
        parser.add_argument("--project", help="Only this project's images.")
        parser.add_argument("--force", action="store_true", help="Re-encode images that already have every size.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        qs = ProjectImage.objects.select_related("project").order_by("pk")
        if options["project"]:
            qs = qs.filter(project__slug=options["project"])
        before = after = done = skipped = complete = 0
        for img in qs:
            if not options["force"] and img.thumb and img.full and img.width:
                complete += 1
                continue
            source = img.full or img.image
            try:
                source.open("rb")
                data = source.read()
                source.close()
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(f"  skip {img.pk}: {exc}")
                skipped += 1
                continue
            old = sum(f.size for f in (img.thumb, img.image, img.full) if f and f.storage.exists(f.name))
            try:
                processed = images.process(ContentFile(data, name="source"))
            except images.ImageRejected as exc:
                self.stderr.write(f"  skip {img.pk}: {exc}")
                skipped += 1
                continue
            before += old
            after += sum(f.size for f in processed.files.values())
            done += 1
            if options["dry_run"]:
                continue
            stale = [f.name for f in (img.thumb, img.image, img.full) if f]
            img.thumb.save(processed.files["thumb"].name, processed.files["thumb"], save=False)
            img.image.save(processed.files["image"].name, processed.files["image"], save=False)
            img.full.save(processed.files["full"].name, processed.files["full"], save=False)
            img.width, img.height = processed.width, processed.height
            img.save()
            keep = {img.thumb.name, img.image.name, img.full.name}
            for name in stale:
                if name not in keep:
                    img.image.storage.delete(name)
        verb = "would re-encode" if options["dry_run"] else "re-encoded"
        self.stdout.write(
            f"{verb} {done} image(s), left {complete} already complete, skipped {skipped}; "
            f"{before // 1024} KB before, {after // 1024} KB after, all sizes"
        )
