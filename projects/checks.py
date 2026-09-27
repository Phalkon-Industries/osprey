"""Startup checks for the projects app.

The Zenodo endpoint check makes it impossible for a deployed server to
boot pointed at anything but real Zenodo. The only exemption is a test
run (see `osprey/test_runner.py`), which is where the fake Zenodo in
`projects/testing/fake_zenodo.py` lives.
"""

from django.conf import settings
from django.core.checks import Error, Tags, register

# base URL -> whether that endpoint is the sandbox
REAL_ZENODO_HOSTS = {
    "https://zenodo.org": False,
    "https://sandbox.zenodo.org": True,
}


def is_test_run() -> bool:
    from osprey import test_runner

    return bool(test_runner.IS_TEST_RUN)


@register(Tags.security)
def zenodo_endpoint_check(app_configs, **kwargs):
    if is_test_run():
        return []
    base = str(getattr(settings, "ZENODO_API_BASE_URL", "")).rstrip("/")
    if base not in REAL_ZENODO_HOSTS:
        return [
            Error(
                f"ZENODO_API_BASE_URL is {base!r}; only https://zenodo.org or "
                "https://sandbox.zenodo.org are allowed outside a test run.",
                hint="Deposits must never be sent anywhere but real Zenodo. "
                "Fix the env file; a fake endpoint is only for `manage.py test`.",
                id="projects.E001",
            )
        ]
    if REAL_ZENODO_HOSTS[base] != bool(settings.ZENODO_USE_SANDBOX):
        return [
            Error(
                f"ZENODO_USE_SANDBOX={settings.ZENODO_USE_SANDBOX!r} does not match "
                f"ZENODO_API_BASE_URL={base!r}.",
                hint="The 'Zenodo sandbox' label must never disagree with where "
                "deposits actually go. Set both for the same endpoint.",
                id="projects.E002",
            )
        ]
    return []
