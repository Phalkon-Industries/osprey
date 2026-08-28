"""Email transport: routes all outgoing mail through admin-configured
settings (the EmailSettings singleton) and a provider-agnostic delivery
backend. The provider is whatever Django email backend
settings.EMAIL_DELIVERY_BACKEND names (anymail ships one per provider,
and plain SMTP works too); nothing in here assumes a particular one.
Also holds the test-send helper used by the admin button and the
send_test_email management command.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.console import EmailBackend as ConsoleBackend
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)


def _load_config():
    from .models import EmailSettings  # deferred so Django apps are loaded

    return EmailSettings.load()


def build_delivery_backend(fail_silently: bool = False, **kwargs) -> BaseEmailBackend:
    """Instantiate the configured provider backend. Raises if unconfigured.

    Provider credentials come from the environment only (never the
    database): anymail backends read them from settings.ANYMAIL, which
    settings.py assembles from ANYMAIL_* env vars; an SMTP backend reads
    Django's EMAIL_HOST* settings.
    """
    backend_cls = import_string(settings.EMAIL_DELIVERY_BACKEND)
    return backend_cls(fail_silently=fail_silently, **kwargs)


class OspreyEmailBackend(BaseEmailBackend):
    """Backend wrapper that reads configuration at send time.

    Every send constructs a fresh backend (Django's get_connection), so
    admin-panel changes take effect on the next email with no restart.
    When email is disabled, or the delivery backend can't initialize
    (missing credentials, bad import path), messages fall through to the
    console backend so nothing crashes in dev or on a half-configured
    install.
    """

    def __init__(self, fail_silently: bool = False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self.config = _load_config()
        self.delegate: BaseEmailBackend | None = None
        if self.config.enabled:
            try:
                self.delegate = build_delivery_backend(
                    fail_silently=fail_silently, **kwargs
                )
            except Exception as exc:  # noqa: BLE001 - any init failure
                # means "not configured yet"; sending must not crash.
                logger.warning(
                    "Email is enabled but the delivery backend %s could not "
                    "be initialized (%s); writing mail to console output "
                    "instead.",
                    settings.EMAIL_DELIVERY_BACKEND,
                    exc,
                )
        if self.delegate is None:
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


def send_test_email(to_address: str) -> str:
    """Send the transport-verification email. Raises on provider failure.

    Returns where the message actually went: "provider" when it was
    handed to the real delivery backend, "console" when it fell through
    to the server log (email disabled or provider unconfigured). Callers
    use this to tell the user the truth instead of a blanket "sent".
    """
    connection = get_connection()
    delegate = getattr(connection, "delegate", None)
    mode = "console" if isinstance(delegate, ConsoleBackend) else "provider"
    message = EmailMessage(
        subject="[OSPREY] Email transport test",
        body=(
            "This is a test message from OSPREY's email settings.\n\n"
            "If you are reading it in an inbox, the delivery provider's "
            "authentication and DNS are working."
        ),
        to=[to_address],
        connection=connection,
    )
    message.send(fail_silently=False)
    return mode
