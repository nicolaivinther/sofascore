"""Polite single-threaded client for Sofascore's private JSON API.

- one request at a time, with a random 4-6 s pause between calls (AGENTS.md rule 4);
- conditional requests: the caller passes the stored ETag, an unchanged resource comes back as a
  0-byte 304;
- exponential backoff on 5xx;
- **a 403 or 429 raises** :class:`SofascoreBlockedError` at once, without a retry: it is
  Sofascore's rate limit (seen after ~1,500 requests at 2.7 a second), and asking again while
  limited only makes it worse. The worker decides between a long cooldown and stopping.

``api.sofascore.com`` 403s every client profile; ``www.sofascore.com`` serves the same ``/api/v1``
paths to a Firefox/Windows cloudscraper profile. Both are constructor arguments, so a block can be
worked around without a code change.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import cloudscraper

DEFAULT_BASE_URL = "https://www.sofascore.com/api/v1"
DEFAULT_BROWSER: dict[str, Any] = {
    "browser": "firefox",
    "platform": "windows",
    "mobile": False,
}
DEFAULT_PAUSE_SECONDS = (4.0, 6.0)
RETRY_STATUSES = frozenset({500, 502, 503, 504})
LIMIT_STATUSES = frozenset({403, 429})


class SofascoreBlockedError(RuntimeError):
    """Sofascore refused a request (403/429): rate-limited or blocked."""


class SofascoreHTTPError(RuntimeError):
    """Raised on a non-retryable or retries-exhausted HTTP failure."""


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one ``get``: 200 carries ``data``; 304 and 404 do not."""

    status: int
    data: dict[str, Any] | None = None
    etag: str | None = None

    @property
    def unchanged(self) -> bool:
        """True on 304: the stored copy is still current."""
        return self.status == 304

    @property
    def not_found(self) -> bool:
        """True on 404 (no stats for a game, no upcoming games for a season, ...)."""
        return self.status == 404


def create_session(browser: dict[str, Any] | None = None) -> Any:
    """A cloudscraper session with the profile Sofascore currently accepts."""
    return cloudscraper.create_scraper(browser=browser or DEFAULT_BROWSER)


class SofascoreClient:  # pylint: disable=too-many-instance-attributes
    """Fetch ``/api/v1`` paths one at a time; see the module docstring for the rules."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        session: Any = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        pause_seconds: tuple[float, float] = DEFAULT_PAUSE_SECONDS,
        max_retries: int = 5,
        backoff_base_seconds: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.session = session if session is not None else create_session()
        self.base_url = base_url.rstrip("/")
        self.pause_seconds = pause_seconds
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.sleep = sleep
        self.uniform = uniform
        self.request_count = 0

    def get(self, path: str, etag: str | None = None) -> FetchResult:
        """Fetch one API path (``/unique-tournament/264/seasons``), conditionally on ``etag``."""
        headers = {"If-None-Match": etag} if etag else {}
        for attempt in range(self.max_retries + 1):
            response = self._request(path, headers)
            status = response.status_code
            if status in LIMIT_STATUSES:
                raise SofascoreBlockedError(f"HTTP {status} on {path}: Sofascore is limiting this client")
            if status in RETRY_STATUSES:
                self.sleep(self._backoff(attempt))
                continue
            return self._finish(path, response, etag)
        raise SofascoreHTTPError(f"{path}: gave up after {self.max_retries + 1} attempts")

    def _request(self, path: str, headers: dict[str, str]) -> Any:
        if self.request_count:
            self.sleep(self.uniform(*self.pause_seconds))
        self.request_count += 1
        return self.session.get(self.base_url + path, headers=headers, timeout=30)

    @staticmethod
    def _finish(path: str, response: Any, sent_etag: str | None) -> FetchResult:
        status = response.status_code
        new_etag = response.headers.get("ETag")
        if status == 304:
            return FetchResult(status=304, etag=new_etag or sent_etag)
        if status == 404:
            return FetchResult(status=404)
        if status != 200:
            raise SofascoreHTTPError(f"{path}: unexpected HTTP {status}")
        return FetchResult(status=200, data=response.json(), etag=new_etag)

    def _backoff(self, attempt: int) -> float:
        return float(self.backoff_base_seconds * (2**attempt))
