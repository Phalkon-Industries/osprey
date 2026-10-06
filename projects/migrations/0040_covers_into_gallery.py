"""Move each project's separate cover image into its image set as the first
gallery image (decided 2026-10-06: one set of images, the first is the
cover). The file is copied, not shared, so deleting either side is safe.
The old cover fields stay on the table for now and are no longer edited.
`manage.py reencode_images` regenerates the three sizes afterwards."""
from django.core.files.base import ContentFile
from django.db import migrations


def covers_into_gallery(apps, schema_editor):
    Project = apps.get_model("projects", "Project")
    ProjectImage = apps.get_model("projects", "ProjectImage")
    for project in Project.objects.exclude(cover_image="").exclude(cover_image__isnull=True):
        try:
            project.cover_image.open("rb")
            data = project.cover_image.read()
            project.cover_image.close()
        except Exception:  # noqa: BLE001 - a missing file just isn't migrated
            continue
        ProjectImage.objects.filter(project=project).update(order=models_F("order") + 1)
        name = project.cover_image.name.rsplit("/", 1)[-1]
        img = ProjectImage(project=project, kind="gallery", order=0, caption="")
        img.image.save(f"cover-{name}", ContentFile(data), save=False)
        img.save()


def models_F(name):
    from django.db.models import F

    return F(name)


class Migration(migrations.Migration):
    dependencies = [("projects", "0039_project_images_gallery")]
    operations = [migrations.RunPython(covers_into_gallery, migrations.RunPython.noop)]
