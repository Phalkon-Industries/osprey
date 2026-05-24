from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import Notification, send


class NotificationSendTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="alice")

    def test_send_creates_row(self):
        n = send(
            self.user,
            kind=Notification.KIND_GENERIC,
            title="Hi",
            body="There",
            url="/x/",
        )
        self.assertIsNotNone(n)
        self.assertEqual(n.user, self.user)
        self.assertEqual(n.title, "Hi")
        self.assertFalse(n.is_read)

    def test_send_to_none_user_is_noop(self):
        self.assertIsNone(send(None, kind="generic", title="x"))

    def test_send_to_inactive_user_is_noop(self):
        self.user.is_active = False
        self.user.save()
        self.assertIsNone(send(self.user, kind="generic", title="x"))


class NotificationViewTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="alice")
        self.other = get_user_model().objects.create_user(username="bob")

    def test_inbox_requires_login(self):
        response = self.client.get(reverse("notifications:inbox"))
        self.assertEqual(response.status_code, 302)

    def test_inbox_lists_own_notifications(self):
        send(self.user, kind="generic", title="Mine")
        send(self.other, kind="generic", title="Not mine")
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications:inbox"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mine")
        self.assertNotContains(response, "Not mine")

    def test_open_marks_read_and_redirects(self):
        n = send(self.user, kind="generic", title="t", url="/somewhere/")
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications:open", args=[n.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/somewhere/")
        n.refresh_from_db()
        self.assertTrue(n.is_read)

    def test_open_someone_elses_notification_is_404(self):
        n = send(self.other, kind="generic", title="t")
        self.client.force_login(self.user)
        response = self.client.get(reverse("notifications:open", args=[n.pk]))
        self.assertEqual(response.status_code, 404)

    def test_mark_all_read(self):
        send(self.user, kind="generic", title="a")
        send(self.user, kind="generic", title="b")
        self.client.force_login(self.user)
        response = self.client.post(reverse("notifications:mark_all_read"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.user.notifications.filter(is_read=False).count(), 0)


class NotificationBadgeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="alice")

    def test_badge_shows_unread_count(self):
        send(self.user, kind="generic", title="One")
        send(self.user, kind="generic", title="Two")
        self.client.force_login(self.user)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Inbox")
        self.assertContains(response, ">2<")
