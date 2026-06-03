from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = (
        "Promote a user to staff (and optionally superuser) by username. "
        "Use --demote to remove staff/superuser instead."
    )

    def add_arguments(self, parser):
        parser.add_argument("username", help="The user's @ tag (no leading @).")
        parser.add_argument(
            "--superuser",
            action="store_true",
            help="Also grant superuser. Default is staff only.",
        )
        parser.add_argument(
            "--demote",
            action="store_true",
            help="Remove staff and superuser instead of granting.",
        )

    def handle(self, *args, **opts):
        User = get_user_model()
        username = opts["username"].lstrip("@")
        try:
            user = User.objects.get(username=username)
        except User.DoesNotExist:
            raise CommandError(f"No user with username '{username}'.")

        if opts["demote"]:
            user.is_staff = False
            user.is_superuser = False
            verb = "Demoted"
        else:
            user.is_staff = True
            if opts["superuser"]:
                user.is_superuser = True
            verb = "Promoted"
        user.save(update_fields=["is_staff", "is_superuser"])

        flags = []
        if user.is_superuser:
            flags.append("superuser")
        elif user.is_staff:
            flags.append("staff")
        else:
            flags.append("no staff/superuser")
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} @{user.username} ({user.email or 'no email'}) -> {', '.join(flags)}"
            )
        )
