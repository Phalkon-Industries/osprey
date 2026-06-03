from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase
from io import StringIO

User = get_user_model()


class PromoteCommandTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="alice", email="a@example.com")

    def _call(self, *args):
        out = StringIO()
        call_command("promote", *args, stdout=out)
        return out.getvalue()

    def test_promote_to_staff(self):
        self._call("alice")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)

    def test_promote_to_superuser(self):
        self._call("alice", "--superuser")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)
        self.assertTrue(self.user.is_superuser)

    def test_demote(self):
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save()
        self._call("alice", "--demote")
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)

    def test_strips_leading_at(self):
        self._call("@alice")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_staff)

    def test_unknown_user_raises(self):
        with self.assertRaises(CommandError):
            self._call("nobody")
