"""Email transport: routes all outgoing mail through admin-configured
settings (the EmailSettings singleton), delegating to Mailjet when enabled
and to console/log output otherwise. Also holds the test-send helper used
by the admin action and the send_test_email management command.
"""
from __future__ import annotations

import logging
import os

from django.conf import settings
from django.core.mail import EmailMessage
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.console import EmailBackend as ConsoleBackend

from anymail.backends.mailjet import EmailBackend as MailjetBackend

logger = logging.getLogger(__name__)


def _load_config():
    from .models import EmailSettings  # deferred so Django apps are loaded

    return EmailSettings.load()


class OspreyEmailBackend(BaseEmailBackend):
    """Backend wrapper that reads credentials at send time.

    Every send constructs a fresh backend (Django's get_connection), so a
    key change in the admin takes effect on the next email with no
    restart. When email is disabled or keys are missing, messages fall
    through to the console backend so nothing crashes in dev or on a
    half-configured install.
    """

    def __init__(self, fail_silently: bool = False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self.config = _load_config()
        api_key = self.config.mailjet_api_key or os.environ.get("MAILJET_API_KEY", "")
        secret = self.config.mailjet_secret_key or os.environ.get(
            "MAILJET_SECRET_KEY", ""
        )
        if self.config.enabled and api_key and secret:
            self.delegate: BaseEmailBackend = MailjetBackend(
                api_key=api_key,
                secret_key=secret,
                fail_silently=fail_silently,
                **kwargs,
            )
        else:
            if self.config.enabled:
                logger.warning(
                    "Email is enabled but Mailjet keys are missing; "
                    "writing mail to console output instead."
                )
            self.delegate = ConsoleBackend(fail_silently=fail_silently)

    def open(self):
        return self.delegate.open()

    def close(self):
        return self.delegate.close()

    def send_messages(self, email_messages):
        # The admin-entered from address wins over the settings default,
        # so the sender identity is managed from the panel too. Messages
        # with an explicit non-default from keep it.
        from_email = (self.config.from_email or "").strip()
        if from_email:
            for message in email_messages:
                if not message.from_email or (
                    message.from_email == settings.DEFAULT_FROM_EMAIL
                ):
                    message.from_email = from_email
        return self.delegate.send_messages(email_messages)


def send_test_email(to_address: str) -> None:
    """Send the transport-verification email. Raises on failure."""
    message = EmailMessage(
        subject="[OSPREY] Email transport test",
        body=(
            "This is a test message from OSPREY's email settings.\n\n"
            "If you are reading it in an inbox, Mailjet authentication "
            "and DNS are working."
        ),
        to=[to_address],
    )
    message.send(fail_silently=False)
