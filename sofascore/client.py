"""Polite single-threaded client for Sofascore's private JSON API.

- one request at a time, with a random 3-5 s pause before every call after the first (AGENTS.md
  rule 4), whichever transport sends it;
- conditional requests: the caller passes the stored ETag, an unchanged resource comes back as a
  0-byte 304;
- exponential backoff on 5xx;
- requests go through curl until Sofascore answers a **challenge** (403 ``reason: challenge``).
  Then the browser passes the captcha, and from then on every request of this client is sent
  from that browser page: the token only works inside the browser session that earned it (not
  from curl, not from a new browser launch);
- any other 403, a 429, a challenge that comes back soon after a solve, or a solve that fails
  **raises** :class:`SofascoreBlockedError` at once: that is a real refusal, and the worker decides
  between a long cooldown and stopping.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sofascore.captcha import BrowserSession, CaptchaToken
from sofascore.transport import CAPTCHA_HEADER, CurlTransport, Response, Transport

DEFAULT_BASE_URL = "https://www.sofascore.com/api/v1"
DEFAULT_PAUSE_SECONDS = (3.0, 5.0)
RETRY_STATUSES = frozenset({500, 502, 503, 504})
LIMIT_STATUSES = frozenset({403, 429})
# A second challenge this soon after a solve means the token is not what is missing. Longer
# than this (the token lasts an hour), the browser may solve again.
MIN_SOLVE_INTERVAL_S = 600.0


class SofascoreBlockedError(RuntimeError):
    """Sofascore refused a request (403/429) and answering a challenge did not help."""


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


class SofascoreClient:  # pylint: disable=too-many-instance-attributes
    """Fetch ``/api/v1`` paths one at a time; see the module docstring for the rules."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        transport: Transport | None = None,
        *,
        browser: BrowserSession | None = None,
        base_url: str = DEFAULT_BASE_URL,
        pause_seconds: tuple[float, float] = DEFAULT_PAUSE_SECONDS,
        max_retries: int = 5,
        backoff_base_seconds: float = 5.0,
        min_solve_interval_s: float = MIN_SOLVE_INTERVAL_S,
        sleep: Callable[[float], None] = time.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.transport: Transport = transport if transport is not None else CurlTransport()
        self.browser = browser
        self.base_url = base_url.rstrip("/")
        self.pause_seconds = pause_seconds
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.min_solve_interval_s = min_solve_interval_s
        self.sleep = sleep
        self.uniform = uniform
        self.clock = clock
        self.request_count = 0
        self.solve_count = 0
        self.browser_request_count = 0
        self._token: CaptchaToken | None = None
        self._solved_at: float | None = None

    def get(self, path: str, etag: str | None = None) -> FetchResult:
        """Fetch one API path (``/unique-tournament/264/seasons``), conditionally on ``etag``."""
        headers = {"If-None-Match": etag} if etag else {}
        for attempt in range(self.max_retries + 1):
            response = self._send(path, headers)
            if response.is_challenge:
                response = self._answer_challenge(path, headers, response)
            status = response.status
            if status in LIMIT_STATUSES:
                kind = "challenge" if response.is_challenge else "refused"
                raise SofascoreBlockedError(
                    f"HTTP {status} ({kind}) on {path}: Sofascore is limiting this client"
                )
            if status in RETRY_STATUSES:
                self.sleep(self._backoff(attempt))
                continue
            return self._finish(path, response, etag)
        raise SofascoreHTTPError(f"{path}: gave up after {self.max_retries + 1} attempts")

    def close(self) -> None:
        """Close the curl session and the browser, if one was opened."""
        self.transport.close()
        if self.browser is not None:
            self.browser.close()

    def _send(self, path: str, headers: dict[str, str]) -> Response:
        if self.request_count:
            self.sleep(self.uniform(*self.pause_seconds))
        self.request_count += 1
        if self.browser is not None and self._token is not None:
            self.browser_request_count += 1
            return self.browser.get(self.base_url + path, {**headers, CAPTCHA_HEADER: self._token.value})
        return self.transport.get(self.base_url + path, headers)

    def _answer_challenge(self, path: str, headers: dict[str, str], challenged: Response) -> Response:
        """Pass the captcha in the browser and send the request again from its page."""
        now = self.clock()
        if self.browser is None:
            return challenged
        if self._solved_at is not None and now - self._solved_at < self.min_solve_interval_s:
            return challenged
        try:
            self._token = self.browser.solve()
        except Exception as exc:  # pylint: disable=broad-except
            raise SofascoreBlockedError(
                f"challenge on {path}; the browser could not solve it: {exc}"
            ) from exc
        self._solved_at = now
        self.solve_count += 1
        return self._send(path, headers)

    @staticmethod
    def _finish(path: str, response: Response, sent_etag: str | None) -> FetchResult:
        status = response.status
        new_etag = response.header("ETag")
        if status == 304:
            return FetchResult(status=304, etag=new_etag or sent_etag)
        if status == 404:
            return FetchResult(status=404)
        if status != 200:
            raise SofascoreHTTPError(f"{path}: unexpected HTTP {status}")
        return FetchResult(status=200, data=response.json(), etag=new_etag)

    def _backoff(self, attempt: int) -> float:
        return float(self.backoff_base_seconds * (2**attempt))
