"""How a request reaches Sofascore: browser-TLS curl, or a real browser page.

Both transports take a full URL plus extra headers and return a :class:`Response`; the client
decides what to send and what a status means. A transport never sleeps between API calls (pacing
belongs to the client) and never retries.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from curl_cffi import CurlOpt, requests as curl_requests
from curl_cffi.const import CurlIpResolve

SITE = "https://www.sofascore.com"
CAPTCHA_HEADER = "X-Captcha"
CAPTCHA_COOKIE = "sofa_captcha"
# Desktop Chrome profiles curl_cffi can impersonate (TLS, HTTP/2 and header order). One is picked
# per session and kept: a client that changes browser between two requests is the odd one out.
IMPERSONATE_PROFILES: tuple[str, ...] = ("chrome136", "chrome133a", "chrome131")
# What Chrome sends on a same-origin fetch() from a sofascore.com page. ``None`` drops the headers
# curl_cffi adds for a page navigation.
API_HEADERS: dict[str, str | None] = {
    "Accept": "*/*",
    "Referer": f"{SITE}/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": None,
    "Upgrade-Insecure-Requests": None,
    "Priority": "u=1, i",
}
REQUEST_TIMEOUT_S = 30


def requested_with(now: float) -> str:
    """The web app's ``X-Requested-With``: SHA-256 of the 30-minute slot number, first 6 hex digits."""
    return hashlib.sha256(str(int(now) // 1800).encode()).hexdigest()[:6]


@dataclass(frozen=True)
class Response:
    """One HTTP answer, independent of the transport that got it."""

    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    text: str = ""

    def header(self, name: str) -> str | None:
        """A header value, case-insensitively."""
        lowered = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == lowered), None)

    def json(self) -> Any:
        """The body as JSON."""
        return json.loads(self.text)

    @property
    def is_challenge(self) -> bool:
        """Sofascore wants a captcha token: 403 ``{"error": {"reason": "challenge"}}``."""
        if self.status != 403:
            return False
        try:
            reason = self.json().get("error", {}).get("reason")
        except (ValueError, AttributeError):
            return False
        return bool(reason == "challenge")


def _curl_session(profile: str) -> Any:
    # IPv4 only: the captcha token carries the (IPv4) address it was solved from; on a dual-stack
    # host curl would otherwise connect from a different address over IPv6.
    return curl_requests.Session(
        impersonate=profile,  # type: ignore[arg-type]
        curl_options={CurlOpt.IPRESOLVE: CurlIpResolve.V4},
    )


class Transport(Protocol):
    """Sends one GET."""

    def get(self, url: str, headers: Mapping[str, str]) -> Response:
        """Send ``url`` with ``headers`` (on top of the transport's own)."""
        raise NotImplementedError

    def close(self) -> None:
        """Release the session or browser."""


class CurlTransport:
    """curl_cffi with a desktop Chrome fingerprint, a cookie jar and a one-time warm-up.

    The first request of a session opens the home page like a person would, so the API calls
    that follow carry the cookies the site sets there. The captcha token, when the client has
    one, rides along both as ``X-Captcha`` (what the web app sends) and as the ``sofa_captcha``
    cookie (what the edge reads on page loads).
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        session: Any = None,
        *,
        profiles: Sequence[str] = IMPERSONATE_PROFILES,
        choice: Callable[[Sequence[str]], str] = random.choice,
        sleep: Callable[[float], None] = time.sleep,
        warm_up_pause_s: tuple[float, float] = (1.0, 3.0),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.clock = clock
        self.profile = choice(profiles)
        self.session = session if session is not None else _curl_session(self.profile)
        self.sleep = sleep
        self.warm_up_pause_s = warm_up_pause_s
        self.warmed_up = False

    def get(self, url: str, headers: Mapping[str, str]) -> Response:
        """Send one API call with fetch() headers, the slot hash and the token, if any."""
        if not self.warmed_up:
            self.warm_up()
        token = headers.get(CAPTCHA_HEADER)
        if token:
            self.session.cookies.set(CAPTCHA_COOKIE, token, domain="www.sofascore.com", path="/")
        merged: dict[str, str | None] = {
            **API_HEADERS,
            "X-Requested-With": requested_with(self.clock()),
            **headers,
        }
        response = self.session.get(url, headers=merged, timeout=REQUEST_TIMEOUT_S)
        return Response(response.status_code, dict(response.headers), response.text)

    def warm_up(self) -> None:
        """Open the home page once; whatever it answers, the cookies it sets are kept."""
        self.warmed_up = True
        try:
            self.session.get(f"{SITE}/", timeout=REQUEST_TIMEOUT_S)
        except curl_requests.RequestsError:
            return
        self.sleep(random.uniform(*self.warm_up_pause_s))  # nosec B311 — pacing, not crypto

    def close(self) -> None:
        """Close the curl session."""
        self.session.close()
