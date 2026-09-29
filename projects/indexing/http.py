"""One HTTP helper for the source adapters: stdlib urllib, a User-Agent
with a mailto, short timeout, retry with backoff on 429 and 503."""
from __future__ import annotations

import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings

from .records import SourceError, user_agent

RETRY_STATUSES = (429, 502, 503, 504)


def _guard_test_run(url: str) -> None:
    """Inside a test run only loopback hosts may be called, unless the live
    contract suite was asked for explicitly. Same rule as the fakes: no
    real external service is ever touched by an ordinary test."""
    import os
    from urllib.parse import urlparse

    from osprey import test_runner

    if not getattr(test_runner, "IS_TEST_RUN", False) or os.environ.get("RUN_LIVE_INDEXING") == "1":
        return
    host = urlparse(url).hostname or ""
    if host not in ("127.0.0.1", "localhost", "::1"):
        from projects.zenodo_register import RegistrationError

        class _Blocked(SourceError, RegistrationError):
            pass

        raise _Blocked(f"Network disabled in tests: {host}")


def get(url: str, *, accept: str = "application/json", params: dict | None = None, timeout: int = 15, retries: int = 3, what: str = "the source") -> bytes:
    if params:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"
    _guard_test_run(url)
    headers = {"Accept": accept, "User-Agent": user_agent()}
    mailto = getattr(settings, "EXTERNAL_API_MAILTO", "")
    if mailto:
        headers["User-Agent"] += f" (mailto:{mailto})"
    delay = getattr(settings, "INDEXING_RETRY_SLEEP", 2.0)
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except HTTPError as exc:
            last = exc
            if exc.code == 404:
                raise SourceError(f"Not found at {what}.") from exc
            if exc.code not in RETRY_STATUSES or attempt == retries:
                raise SourceError(f"{what.capitalize()} returned HTTP {exc.code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            last = exc
            if attempt == retries:
                raise SourceError(f"Could not reach {what}: {exc}") from exc
        time.sleep(delay * (attempt + 1))
    raise SourceError(f"Could not reach {what}: {last}")
