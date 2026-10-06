"""Project images: processing, limits, endpoints, README rules, page layout."""
from __future__ import annotations

import importlib
import io
import os
import tempfile
from decimal import Decimal

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from projects import images
from projects.models import Contribution, Project, ProjectImage
from projects.templatetags.osprey_md import render_project_markdown
from projects.tests import ProjectTestCase

_MEDIA = tempfile.mkdtemp(prefix="osprey-images-test-")


def photo(name="photo.jpg", size=(3000, 2000), fmt="JPEG", orientation=None, color=(40, 110, 160)) -> SimpleUploadedFile:
    im = Image.new("RGB", size, color)
    buf = io.BytesIO()
    kwargs = {}
    if orientation is not None or fmt == "JPEG":
        exif = Image.Exif()
        exif[0x010F] = "PhoneMaker"
        if orientation is not None:
            exif[0x0112] = orientation
        kwargs["exif"] = exif
    im.save(buf, fmt, **kwargs)
    return SimpleUploadedFile(name, buf.getvalue(), content_type=f"image/{fmt.lower()}")


def animated_gif() -> SimpleUploadedFile:
    frames = [Image.new("RGB", (300, 200), (i * 60, 40, 200 - i * 40)).convert("P", palette=Image.ADAPTIVE) for i in range(4)]
    buf = io.BytesIO()
    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:], duration=120, loop=0)
    return SimpleUploadedFile("anim.gif", buf.getvalue(), content_type="image/gif")


def bomb_png(size=(8000, 7500)) -> SimpleUploadedFile:
    """Sixty megapixels in a few KB: a 1-bit PNG of nothing."""
    buf = io.BytesIO()
    Image.new("1", size).save(buf, "PNG")
    return SimpleUploadedFile("bomb.png", buf.getvalue(), content_type="image/png")


def bomb_gif(frames=13, size=(2000, 2000)) -> SimpleUploadedFile:
    """Fifty-two megapixels spread over frames, each one small on disk."""
    seq = [Image.new("P", size, i % 2) for i in range(frames)]
    buf = io.BytesIO()
    seq[0].save(buf, "GIF", save_all=True, append_images=seq[1:], duration=50, loop=0)
    return SimpleUploadedFile("bomb.gif", buf.getvalue(), content_type="image/gif")


@override_settings(MEDIA_ROOT=_MEDIA)
class ProcessingTests(TestCase):
    def test_three_sizes_rotated_and_stripped(self):
        out = images.process(photo(orientation=6))  # camera says: rotate 90
        sizes = {}
        for name, f in out.files.items():
            im = Image.open(io.BytesIO(f.read()))
            self.assertEqual(im.format, "WEBP")
            self.assertEqual(list(im.getexif().keys()), [], "metadata must be stripped")
            sizes[name] = im.size
        self.assertEqual(sizes["full"], (1600, 2400))  # portrait after rotation
        self.assertEqual(sizes["image"], (1067, 1600))
        self.assertEqual(sizes["thumb"], (320, 480))
        self.assertEqual((out.width, out.height), (1067, 1600))

    def test_small_images_are_not_upscaled(self):
        out = images.process(photo(size=(640, 360), fmt="PNG"))
        self.assertEqual(Image.open(io.BytesIO(out.files["full"].read())).size, (640, 360))

    def test_animated_gif_stays_animated(self):
        out = images.process(animated_gif())
        im = Image.open(io.BytesIO(out.files["image"].read()))
        self.assertTrue(getattr(im, "is_animated", False))
        self.assertEqual(im.n_frames, 4)

    def test_rejections(self):
        with self.assertRaises(images.ImageRejected):
            images.process(SimpleUploadedFile("x.svg", b"<svg xmlns='http://www.w3.org/2000/svg'/>", content_type="image/svg+xml"))
        with self.assertRaises(images.ImageRejected):
            images.process(SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain"))
        big = SimpleUploadedFile("big.jpg", b"x" * (images.MAX_UPLOAD_BYTES + 1), content_type="image/jpeg")
        with self.assertRaisesMessage(images.ImageRejected, "over 10 MB"):
            images.process(big)
        buf = io.BytesIO(); Image.new("RGB", (10, 10)).save(buf, "BMP")
        with self.assertRaisesMessage(images.ImageRejected, "Only PNG, JPEG, GIF and WebP"):
            images.process(SimpleUploadedFile("x.bmp", buf.getvalue()))
        # The byte limit says nothing about decoded size; the pixel cap does.
        with self.assertRaisesMessage(images.ImageRejected, "over 50 megapixels"):
            images.process(bomb_png())
        with self.assertRaisesMessage(images.ImageRejected, "over 50 megapixels"):
            images.process(bomb_gif())


@override_settings(MEDIA_ROOT=_MEDIA)
class EndpointTests(ProjectTestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("projects:image_upload", args=[self.private_project.slug])

    def _upload(self, *files, kind="gallery"):
        return self.client.post(self.url, {"files": list(files), "kind": kind})

    def test_permissions(self):
        self.assertEqual(self._upload(photo()).status_code, 302)  # anonymous: to sign-in
        slug = self.private_project.slug
        existing = ProjectImage.objects.create(project=self.private_project, image=ContentFile(b"x", name="p.webp"))
        others = [
            (reverse("projects:image_caption", args=[slug, existing.pk]), {"caption": "x"}),
            (reverse("projects:image_reorder", args=[slug]), {"ids": str(existing.pk)}),
            (reverse("projects:image_delete", args=[slug, existing.pk]), {}),
        ]
        for user in (self.unrelated, self.contributor):  # a listed contributor isn't an editor
            self.client.force_login(user)
            self.assertEqual(self._upload(photo()).status_code, 404)
            for url, data in others:
                self.assertEqual(self.client.post(url, data).status_code, 404, url)
        Contribution.objects.filter(project=self.private_project, user=self.contributor).update(claim_status="verified", editor=True)
        self.assertEqual(self._upload(photo()).status_code, 200)
        for url, data in others:
            self.assertEqual(self.client.post(url, data).status_code, 200, url)
        self.assertEqual(self.private_project.images.count(), 1)  # the upload; `existing` was deleted

    def test_upload_several_with_one_bad_file(self):
        self.client.force_login(self.owner)
        response = self._upload(photo("a.jpg"), photo("b.png", fmt="PNG"), SimpleUploadedFile("c.txt", b"no"))
        data = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(data["images"]), 2)
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["errors"], ["That file isn't an image OSPREY can read."])
        self.assertTrue(data["images"][0]["markdown"].startswith("![]("))

    def test_limit_of_twenty(self):
        self.client.force_login(self.owner)
        for i in range(images.MAX_IMAGES):
            ProjectImage.objects.create(project=self.private_project, image=ContentFile(b"x", name=f"{i}.webp"), order=i)
        data = self._upload(photo()).json()
        self.assertEqual(data["images"], [])
        self.assertEqual(data["errors"], ["A project can have up to 20 images."])

    def test_caption_reorder_delete_and_cover(self):
        self.client.force_login(self.owner)
        a, b, c = (self._upload(photo(f"{n}.jpg")).json()["images"][0]["id"] for n in "abc")
        self.assertEqual(self.private_project.cover.pk, a)
        slug = self.private_project.slug
        self.client.post(reverse("projects:image_caption", args=[slug, b]), {"caption": "Housing"})
        self.assertEqual(ProjectImage.objects.get(pk=b).caption, "Housing")
        # The crop belongs to the cover: it survives a reorder that keeps the
        # first image and resets when a different image becomes the cover.
        def crop():
            return tuple(Project.objects.filter(pk=self.private_project.pk).values_list(
                "cover_image_focal_x", "cover_image_focal_y", "cover_image_zoom")[0])
        Project.objects.filter(pk=self.private_project.pk).update(cover_image_focal_x=30, cover_image_focal_y=70, cover_image_zoom=2)
        self.client.post(reverse("projects:image_reorder", args=[slug]), {"ids": f"{a},{c},{b}"})
        self.assertEqual(crop(), (30, 70, Decimal("2")))
        self.client.post(reverse("projects:image_reorder", args=[slug]), {"ids": f"{c},{a},{b}"})
        self.assertEqual(crop(), (50, 50, Decimal("1")))
        self.assertEqual([i.pk for i in self.private_project.gallery_images], [c, a, b])
        self.assertEqual(self.private_project.cover.pk, c)
        img = ProjectImage.objects.get(pk=c)
        paths = [img.thumb.path, img.image.path, img.full.path]
        Project.objects.filter(pk=self.private_project.pk).update(cover_image_focal_x=30, cover_image_focal_y=70, cover_image_zoom=2)
        self.client.post(reverse("projects:image_delete", args=[slug, c]))
        self.assertFalse(ProjectImage.objects.filter(pk=c).exists())
        self.assertFalse(any(os.path.exists(p) for p in paths))
        self.assertEqual(self.private_project.cover.pk, a)
        self.assertEqual(crop(), (50, 50, Decimal("1")))  # the cover changed again
        # Another project's image can't be touched through this project.
        other = Project.objects.create(slug="other", title="Other", created_by=self.unrelated, artifact_type="Hardware", field="X")
        foreign = ProjectImage.objects.create(project=other, image=ContentFile(b"x", name="f.webp"))
        self.assertEqual(self.client.post(reverse("projects:image_delete", args=[slug, foreign.pk])).status_code, 404)

    def test_readme_images_stay_out_of_the_gallery(self):
        self.client.force_login(self.owner)
        self._upload(photo("g.jpg"))
        self._upload(photo("r.jpg"), kind="readme")
        self.assertEqual(len(self.private_project.gallery_images), 1)
        self.assertEqual(self.private_project.images.count(), 2)


@override_settings(MEDIA_ROOT=_MEDIA)
class ReadmeAndPageTests(ProjectTestCase):
    def _image(self, project, kind="gallery", caption=""):
        return images.store(project, photo(), kind=kind, caption=caption)

    def test_readme_keeps_own_images_and_drops_others(self):
        own = self._image(self.public_project, kind="readme")
        other = Project.objects.create(slug="other", title="Other", created_by=self.unrelated, artifact_type="Hardware", field="X")
        theirs = self._image(other, kind="readme")
        text = (f"![ours]({own.image.url})\n\n![theirs]({theirs.image.url})\n\n"
                "![pixel](https://tracker.example.com/p.gif)\n\nText stays.")
        html = render_project_markdown(text, self.public_project)
        self.assertIn(own.image.url, html)
        self.assertNotIn(theirs.image.url, html)
        self.assertNotIn("tracker.example.com", html)
        self.assertIn("Text stays.", html)
        Project.objects.filter(pk=self.public_project.pk).update(readme=text)
        page = self.client.get(self.public_project.get_absolute_url())
        self.assertContains(page, own.image.url)
        self.assertNotContains(page, "tracker.example.com")

    def test_viewer_strip_banner_and_placeholder(self):
        url = self.public_project.get_absolute_url()
        self.assertNotContains(self.client.get(url), "data-gallery")  # no images: generated banner
        first = self._image(self.public_project, caption="Front")
        page = self.client.get(url)
        self.assertContains(page, "data-gallery")
        self.assertNotContains(page, "gallery-strip")  # one image: no strip
        self.assertContains(page, first.image.url)
        self._image(self.public_project, caption="Back")
        page = self.client.get(url)
        self.assertContains(page, "gallery-strip")
        self.assertContains(page, first.full_url)
        body = page.content.decode()
        self.assertLess(body.index("data-gallery"), body.index("project-header"))
        # Other tabs keep the cropped cover banner, not the viewer.
        lineage = self.client.get(reverse("projects:lineage", args=[self.public_project.slug]))
        self.assertNotContains(lineage, "data-gallery")
        self.assertContains(lineage, first.image.url)
        # Cards use the first image's thumbnail.
        self.assertContains(self.client.get(reverse("projects:list")), first.thumb_url)

    def test_editor_tab(self):
        self.client.force_login(self.owner)
        new = self.client.get(reverse("projects:new"))
        self.assertContains(new, 'data-form-tab="images"')
        self.assertContains(new, "Adding an image saves the draft.")
        self.assertContains(new, "data-new-project")
        self.assertContains(new, "data-readme-image-error")  # where the README guard speaks
        self.assertNotContains(new, 'name="cover_image"')
        edit = self.client.get(reverse("projects:edit", args=[self.private_project.slug]))
        self.assertContains(edit, "data-image-editor")
        self.assertContains(edit, "Up to 20 images, 10 MB each.")
        self.assertContains(edit, "Insert image")

    def test_deleting_a_draft_removes_every_size(self):
        img = self._image(self.private_project)
        paths = [img.thumb.path, img.image.path, img.full.path]
        self.client.force_login(self.owner)
        self.client.post(reverse("projects:delete", args=[self.private_project.slug]))
        self.assertFalse(any(os.path.exists(p) for p in paths))


@override_settings(MEDIA_ROOT=_MEDIA)
class ReencodeCommandTests(ProjectTestCase):
    def _migrated_cover(self, project):
        """The shape migration 0040 leaves behind: a display image, no other sizes."""
        buf = io.BytesIO()
        Image.new("RGB", (2000, 1000), (9, 9, 9)).save(buf, "PNG")
        img = ProjectImage(project=project, kind="gallery", order=0)
        img.image.save("cover-old.png", ContentFile(buf.getvalue()), save=False)
        img.save()
        return img

    def test_fills_in_missing_sizes_and_leaves_finished_images_alone(self):
        old = self._migrated_cover(self.public_project)
        old_path = old.image.path
        done = images.store(self.public_project, photo(), kind="gallery")
        done_names = (done.thumb.name, done.image.name, done.full.name)
        out = io.StringIO()
        call_command("reencode_images", stdout=out)
        self.assertIn("re-encoded 1 image(s), left 1 already complete", out.getvalue())
        old.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual((old.width, old.height), (1600, 800))
        for f in (old.thumb, old.image, old.full):
            self.assertEqual(Image.open(f.path).format, "WEBP")
        self.assertFalse(os.path.exists(old_path))
        self.assertEqual((done.thumb.name, done.image.name, done.full.name), done_names)
        # A second run has nothing to do; --force redoes everything.
        out = io.StringIO()
        call_command("reencode_images", stdout=out)
        self.assertIn("re-encoded 0 image(s), left 2 already complete", out.getvalue())
        out = io.StringIO()
        call_command("reencode_images", "--force", stdout=out)
        self.assertIn("re-encoded 2 image(s)", out.getvalue())
        done.refresh_from_db()
        self.assertNotEqual(done.full.name, done_names[2])
        self.assertFalse(default_storage.exists(done_names[2]))
        self.assertTrue(default_storage.exists(done.full.name))

    def test_dry_run_writes_nothing(self):
        old = self._migrated_cover(self.private_project)
        out = io.StringIO()
        call_command("reencode_images", "--dry-run", stdout=out)
        self.assertIn("would re-encode 1 image(s)", out.getvalue())
        old.refresh_from_db()
        self.assertFalse(old.thumb)
        self.assertIsNone(old.width)
        self.assertTrue(os.path.exists(old.image.path))


@override_settings(MEDIA_ROOT=_MEDIA)
class CoverMigrationTests(ProjectTestCase):
    def test_existing_cover_becomes_image_one(self):
        buf = io.BytesIO(); Image.new("RGB", (800, 450), (1, 2, 3)).save(buf, "WEBP")
        self.public_project.cover_image.save("old-cover.webp", ContentFile(buf.getvalue()), save=True)
        existing = images.store(self.public_project, photo(), kind="gallery")
        migration = importlib.import_module("projects.migrations.0040_covers_into_gallery")
        migration.covers_into_gallery(django_apps, None)
        gallery = self.public_project.gallery_images
        self.assertEqual(len(gallery), 2)
        self.assertTrue(gallery[0].image.name.endswith("cover-old-cover.webp"))
        self.assertEqual(gallery[1].pk, existing.pk)
        self.assertNotEqual(gallery[0].image.name, self.public_project.cover_image.name)  # copied, not shared


@override_settings(MEDIA_ROOT=_MEDIA)
class ReadmeCaptionTests(ProjectTestCase):
    """README image captions live in the Markdown brackets and stay in sync
    with the image's tile (decided 2026-10-06)."""

    def _readme_image(self, caption=""):
        return images.store(self.public_project, photo(), kind="readme", caption=caption)

    def test_caption_helpers(self):
        url = "/media/projects/1/images/abc-image.webp"
        self.assertEqual(images.markdown_caption("Fig [1]\nhousing  "), "Fig 1 housing")
        readme = f"![old]({url})\n\ntext\n\n![other]({url}) and ![x](/media/projects/1/images/zzz-image.webp)"
        self.assertEqual(images.readme_caption(readme, url), "old")
        self.assertIsNone(images.readme_caption(readme, "/media/projects/1/images/none-image.webp"))
        rewritten = images.set_readme_caption(readme, url, "new [cap]")
        self.assertEqual(rewritten.count(f"![new cap]({url})"), 2)
        self.assertIn("![x](/media/projects/1/images/zzz-image.webp)", rewritten)

    def test_bracket_text_renders_as_a_caption_under_the_image(self):
        img = self._readme_image()
        html = render_project_markdown(f"Intro.\n\n![Wiring for the pressure sensor]({img.image.url})\n\nAfter.", self.public_project)
        self.assertIn("<figure", html)
        self.assertIn("<figcaption>Wiring for the pressure sensor</figcaption>", html)
        self.assertIn(img.image.url, html)
        plain = render_project_markdown(f"![]({img.image.url})", self.public_project)
        self.assertIn(img.image.url, plain)
        self.assertNotIn("<figcaption", plain)

    def test_inserted_markdown_carries_the_caption_or_nothing(self):
        from projects.views_images import image_json

        self.assertEqual(image_json(self._readme_image())["markdown"], f"![]({self.public_project.images.last().image.url})")
        captioned = self._readme_image(caption="Schematic")
        self.assertEqual(image_json(captioned)["markdown"], f"![Schematic]({captioned.image.url})")

    def test_tile_caption_rewrites_the_brackets_in_the_readme(self):
        img = self._readme_image()
        Project.objects.filter(pk=self.public_project.pk).update(readme=f"# Pump\n\n![]({img.image.url})\n\nText.")
        self.client.force_login(self.owner)
        response = self.client.post(reverse("projects:image_caption", args=[self.public_project.slug, img.pk]), {"caption": "Pump housing"})
        self.public_project.refresh_from_db()
        self.assertEqual(self.public_project.readme, f"# Pump\n\n![Pump housing]({img.image.url})\n\nText.")
        self.assertEqual(response.json()["readme"], self.public_project.readme)
        # Gallery captions never touch the README.
        gallery = images.store(self.public_project, photo(), kind="gallery")
        response = self.client.post(reverse("projects:image_caption", args=[self.public_project.slug, gallery.pk]), {"caption": "Front"})
        self.assertNotIn("readme", response.json())

    def test_editing_the_brackets_updates_the_caption_on_save(self):
        img = self._readme_image(caption="Old caption")
        self.client.force_login(self.owner)
        data = self.project_form_post_data(action="save")
        data["title"] = self.public_project.title
        data["readme"] = f"# Pump\n\n![New caption from the README]({img.image.url})\n"
        self.client.post(reverse("projects:edit", args=[self.public_project.slug]), data)
        img.refresh_from_db()
        self.assertEqual(img.caption, "New caption from the README")

    def test_images_left_out_of_the_readme_are_marked_not_used(self):
        used = self._readme_image(caption="Used")
        unused = self._readme_image(caption="Unused")
        Project.objects.filter(pk=self.public_project.pk).update(readme=f"![Used]({used.image.url})")
        self.client.force_login(self.owner)
        page = self.client.get(reverse("projects:edit", args=[self.public_project.slug]))
        body = page.content.decode()
        tile = body[body.index(f'data-id="{unused.pk}"'):]
        tile = tile[: tile.index("</li>")]
        self.assertIn("Not used in the README", tile)
        self.assertIn("data-tile-insert", tile)
        used_tile = body[body.index(f'data-id="{used.pk}"'):]
        used_tile = used_tile[: used_tile.index("</li>")]
        self.assertIn("hidden", used_tile[used_tile.index("Not used in the README") - 60:used_tile.index("Not used in the README")])
