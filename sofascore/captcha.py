"""Sofascore's captcha token and the browser that earns it and then sends the API calls.

When the API answers 403 ``{"error": {"reason": "challenge"}}``, the web app sends the visitor to
``/captcha.html``. That page runs Cloudflare Turnstile, posts the result to
``/api/v1/token/captcha`` and gets a JWT back, which it stores as the ``sofa_captcha`` cookie;
the app then sends it as ``X-Captcha`` on every API call until its ``exp`` (one hour). The token
works only inside the browser session that solved it: curl and a new browser launch are both
challenged again. :class:`BrowserSession` solves in a real Chrome (patchright) and then sends the
API calls as ``fetch()`` from that page.
"""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sofascore.transport import CAPTCHA_COOKIE, CAPTCHA_HEADER, SITE, Response

CAPTCHA_URL = f"{SITE}/captcha.html?redirectUrl={quote(SITE + '/', safe='')}"
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


class BrowserSession:  # pylint: disable=too-many-instance-attributes
    """A visible Chrome with a fresh profile, launched on first use and kept until closed.

    Never headless: Turnstile passes a visible Chrome without a click, not a headless one.

    Every launch gets a new, empty profile under ``profile_root`` (deleted on close), so nothing
    from an earlier, refused session carries over. A token works only inside the session that
    solved it, so it is never kept beyond :meth:`close`.
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        profile_root: Path,
        *,
        channel: str = "chrome",
        auto_click: bool = True,
        solve_timeout_s: float = SOLVE_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.profile_root = profile_root
        self._profile_dir: Path | None = None
        self.channel = channel
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
        if not page.url.startswith(SITE) or "/captcha.html" in page.url:
            page.goto(f"{SITE}/", wait_until="domcontentloaded")
        sent = {k: v for k, v in headers.items() if k in (CAPTCHA_HEADER, "If-None-Match")}
        result = page.evaluate(_FETCH_JS, [url, sent])
        return Response(int(result["status"]), result["headers"], result["text"])

    def close(self) -> None:
        """Shut the browser down and delete its profile."""
        if self._context is not None:
            self._context.close()
        if self._playwright is not None:
            self._playwright.stop()
        if self._profile_dir is not None:
            shutil.rmtree(self._profile_dir, ignore_errors=True)
        self._playwright = self._context = self._page = self._profile_dir = None

    def _open(self) -> Any:
        if self._page is None:
            # Imported here: a run that is never challenged never loads the browser driver.
            from patchright.sync_api import sync_playwright  # pylint: disable=import-outside-toplevel

            self.profile_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._profile_dir = Path(tempfile.mkdtemp(prefix="profile-", dir=self.profile_root))
            self._playwright = sync_playwright().start()
            self._context = self._playwright.chromium.launch_persistent_context(
                str(self._profile_dir), channel=self.channel, headless=False, no_viewport=True
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
