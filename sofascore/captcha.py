"""Sofascore's captcha token: what it is, where it is kept, and how a browser gets one.

When the API answers 403 ``{"error": {"reason": "challenge"}}``, the web app sends the visitor to
``/captcha.html``. That page runs Cloudflare Turnstile, posts the result to
``/api/v1/token/captcha`` and gets a JWT back, which it stores as the ``sofa_captcha`` cookie;
the app then sends it as ``X-Captcha`` on every API call until its ``exp``. :class:`BrowserSession`
does the same in a real Chrome (patchright) and can also send API calls from that page when curl
is refused even with a token.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sofascore.transport import CAPTCHA_COOKIE, CAPTCHA_HEADER, SITE, Response

CAPTCHA_URL = f"{SITE}/captcha.html?redirectUrl={quote(SITE + '/', safe='')}"
# A token this close to its expiry is not used for a new request.
EXPIRY_MARGIN_S = 120
SOLVE_TIMEOUT_S = 90
TURNSTILE_FRAME = "challenges.cloudflare.com"
# fetch() from the page: the browser adds its own headers; we add only the token and the ETag.
_FETCH_JS = """
async ([url, headers]) => {
    const r = await fetch(url, {headers, cache: 'no-store', credentials: 'include'});
    return {status: r.status, headers: Object.fromEntries(r.headers.entries()), text: await r.text()};
}
"""


class CaptchaError(RuntimeError):
    """The browser could not get a captcha token."""


@dataclass(frozen=True)
class CaptchaToken:
    """The JWT Sofascore hands out for a solved challenge."""

    value: str
    expires_at: float

    @classmethod
    def parse(cls, value: str) -> CaptchaToken:
        """Read ``exp`` from the JWT payload; the signature is Sofascore's business."""
        try:
            payload = value.split(".")[1]
            padded = payload + "=" * (-len(payload) % 4)
            exp = json.loads(base64.urlsafe_b64decode(padded))["exp"]
        except (IndexError, ValueError, KeyError, TypeError) as exc:
            raise CaptchaError(f"not a captcha JWT: {exc}") from exc
        return cls(value, float(exp))

    def usable(self, now: float) -> bool:
        """Still valid for at least :data:`EXPIRY_MARGIN_S`."""
        return self.expires_at - EXPIRY_MARGIN_S > now


class TokenStore:
    """Keeps the token in a file, so the next cron run reuses it instead of opening a browser."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def load(self) -> CaptchaToken | None:
        """The stored token, or None when there is none or it is unreadable."""
        if self.path is None or not self.path.exists():
            return None
        try:
            return CaptchaToken.parse(self.path.read_text(encoding="utf-8").strip())
        except CaptchaError:
            return None

    def save(self, token: CaptchaToken) -> None:
        """Write the token, readable by this user only."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.write_text(token.value, encoding="utf-8")


class BrowserSession:
    """A real Chrome with a persistent profile, launched on first use and kept until closed."""

    def __init__(  # pylint: disable=too-many-arguments
        self,
        profile_dir: Path,
        *,
        channel: str = "chrome",
        headless: bool = True,
        auto_click: bool = True,
        solve_timeout_s: float = SOLVE_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.profile_dir = profile_dir
        self.channel = channel
        self.headless = headless
        self.auto_click = auto_click
        self.solve_timeout_s = solve_timeout_s
        self.clock = clock
        self.sleep = sleep
        self._playwright: Any = None
        self._context: Any = None
        self._page: Any = None

    def solve(self) -> CaptchaToken:
        """Open ``captcha.html`` and wait for the page to store ``sofa_captcha``."""
        page = self._open()
        page.goto(CAPTCHA_URL, wait_until="domcontentloaded")
        deadline = self.clock() + self.solve_timeout_s
        while self.clock() < deadline:
            value = self._cookie(CAPTCHA_COOKIE)
            if value:
                return CaptchaToken.parse(value)
            if self.auto_click:
                self._click_checkbox(page)
            self.sleep(1.0)
        raise CaptchaError(f"no {CAPTCHA_COOKIE} cookie after {self.solve_timeout_s:.0f} s")

    def get(self, url: str, headers: Mapping[str, str]) -> Response:
        """Send one API call as fetch() from a sofascore.com page."""
        page = self._open()
        if not page.url.startswith(SITE):
            page.goto(f"{SITE}/", wait_until="domcontentloaded")
        sent = {k: v for k, v in headers.items() if k in (CAPTCHA_HEADER, "If-None-Match")}
        result = page.evaluate(_FETCH_JS, [url, sent])
        return Response(int(result["status"]), result["headers"], result["text"])

    def close(self) -> None:
        """Shut the browser down; the profile (and its cookies) stays on disk."""
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._playwright = self._context = self._page = None

    def _open(self) -> Any:
        if self._page is None:
            # Imported here: a run that is never challenged never loads the browser driver.
            from patchright.sync_api import (  # pylint: disable=import-outside-toplevel
                sync_playwright,
            )

            self.profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), channel=self.channel, headless=self.headless, no_viewport=True
            )
            pages = self._context.pages
            self._page = pages[0] if pages else self._context.new_page()
        return self._page

    def _cookie(self, name: str) -> str | None:
        for cookie in self._context.cookies(SITE):
            if cookie.get("name") == name and cookie.get("value"):
                return str(cookie["value"])
        return None

    @staticmethod
    def _click_checkbox(page: Any) -> None:
        """Tick the Turnstile box when it asks for a click; most visits pass without one."""
        for frame in page.frames:
            if TURNSTILE_FRAME not in frame.url:
                continue
            box = frame.locator("input[type=checkbox]")
            try:
                if box.count() and box.first.is_visible():
                    box.first.click(timeout=2000)
            except Exception:  # pylint: disable=broad-except
                return  # the widget re-renders while it works; the next poll tries again
