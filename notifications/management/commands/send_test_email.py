"""CLI twin of the admin panel's "send a test email" action."""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from notifications.email import send_test_email
from notifications.models import EmailSettings


class Command(BaseCommand):
    help = "Send a transport-verification email through the configured backend."

    def add_arguments(self, parser):
        parser.add_argument(
            "address",
            nargs="?",
            default="",
            help="Recipient. Defaults to the admin-configured test recipient.",
        )

    def handle(self, *args, **options):
        config = EmailSettings.load()
        to_address = options["address"] or config.test_recipient or config.from_email
        if not to_address:
            raise CommandError(
                "No recipient: pass an address or set one in the admin panel."
            )
        mode = send_test_email(to_address)
        state = (
            "the delivery provider"
            if mode == "provider"
            else "console output (email disabled or provider unconfigured)"
        )
        self.stdout.write(f"Test email to {to_address} handed to {state}.")
