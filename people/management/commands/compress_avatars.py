"""Re-encode stored avatars that predate upload-time compression.

Safe to re-run: already-processed files (WebP within the size cap) are
skipped, and a file that fails to decode is reported and left alone.
The old full-size file is deleted once the replacement is stored.
"""

from django.core.management.base import BaseCommand

from people import avatars
from people.models import Profile


class Command(BaseCommand):
    help = "Re-encode existing avatars as capped WebP and delete the originals."

    def handle(self, *args, **options):
        converted = skipped = failed = 0
        for profile in Profile.objects.exclude(avatar="").iterator():
            field = profile.avatar
            if avatars.is_processed(field):
                skipped += 1
                continue
            old_name = field.name
            try:
                with field.open("rb") as handle:
                    content = avatars.process(handle, name_hint=old_name)
            except Exception as exc:  # noqa: BLE001 - report and move on
                failed += 1
                self.stderr.write(f"failed {old_name}: {exc}")
                continue
            profile.avatar.save(content.name, content, save=False)
            profile.save(update_fields=["avatar"])
            if profile.avatar.name != old_name:
                field.storage.delete(old_name)
            converted += 1
            self.stdout.write(f"converted {old_name} -> {profile.avatar.name}")
        self.stdout.write(
            f"done: {converted} converted, {skipped} already fine, "
            f"{failed} failed"
        )
