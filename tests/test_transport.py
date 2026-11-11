"""Unit tests for sofascore.transport and the token side of sofascore.captcha (no network)."""

# pylint: disable=missing-function-docstring,missing-class-docstring

import base64
import json
import stat

import pytest

from sofascore.captcha import CaptchaError, CaptchaToken, TokenStore
from sofascore.transport import API_HEADERS, CAPTCHA_HEADER, SITE, CurlTransport, Response, requested_with

API_URL = f"{SITE}/api/v1/unique-tournament/264"


class FakeCookies:
    def __init__(self):
        self.set_calls = []

    def set(self, name, value, domain, path):
        self.set_calls.append((name, value, domain, path))


class FakeCurlResponse:
    def __init__(self, status=200, text="{}"):
        self.status_code = status
        self.headers = {"ETag": 'W/"x"'}
        self.text = text


class FakeCurlSession:
    def __init__(self):
        self.cookies = FakeCookies()
        self.calls = []
        self.closed = False

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers, timeout))
        return FakeCurlResponse()

    def close(self):
        self.closed = True


def _transport():
    session = FakeCurlSession()
    sleeps: list = []
    transport = CurlTransport(
        session, choice=lambda profiles: profiles[0], sleep=sleeps.append, clock=lambda: 1_794_389_400.0
    )
    return transport, session, sleeps


def _jwt(payload):
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.sig"


def test_the_first_request_opens_the_home_page_once():
    transport, session, sleeps = _transport()
    transport.get(API_URL, {})
    transport.get(API_URL, {})
    assert [url for url, _, _ in session.calls] == [f"{SITE}/", API_URL, API_URL]
    assert session.calls[0][1] is None  # a page load: the profile's own navigation headers
    assert len(sleeps) == 1


def test_api_calls_look_like_a_same_origin_fetch():
    transport, session, _ = _transport()
    response = transport.get(API_URL, {"If-None-Match": 'W/"a"'})
    headers = session.calls[1][1]
    assert headers["Sec-Fetch-Mode"] == "cors" and headers["Accept"] == "*/*"
    assert headers["Upgrade-Insecure-Requests"] is None and headers["Sec-Fetch-User"] is None
    assert headers["If-None-Match"] == 'W/"a"'
    assert set(API_HEADERS) <= set(headers)
    assert response == Response(200, {"ETag": 'W/"x"'}, "{}")


def test_the_token_goes_as_header_and_cookie():
    transport, session, _ = _transport()
    transport.get(API_URL, {CAPTCHA_HEADER: "jwt-a"})
    assert session.calls[1][1][CAPTCHA_HEADER] == "jwt-a"
    assert session.cookies.set_calls == [("sofa_captcha", "jwt-a", "www.sofascore.com", "/")]


def test_one_profile_per_session():
    transport, _, _ = _transport()
    assert transport.profile == "chrome136"


@pytest.mark.parametrize(
    ("response", "challenge"),
    [
        (Response(403, {}, '{"error": {"code": 403, "reason": "challenge" }}'), True),
        (Response(403, {}, '{"error": {"code": 403}}'), False),
        (Response(403, {}, "<html>denied</html>"), False),
        (Response(429, {}, '{"error": {"reason": "challenge"}}'), False),
    ],
)
def test_only_a_403_with_reason_challenge_is_a_challenge(response, challenge):
    assert response.is_challenge is challenge


def test_headers_are_case_insensitive():
    assert Response(200, {"etag": "a"}).header("ETag") == "a"


def test_token_expiry_comes_from_the_jwt():
    token = CaptchaToken.parse(_jwt({"exp": 1_800_003_600}))
    assert token.expires_at == 1_800_003_600
    assert token.usable(1_800_000_000) and not token.usable(1_800_003_500)


@pytest.mark.parametrize("value", ["", "not-a-jwt", _jwt({"no": "exp"})])
def test_a_malformed_token_is_rejected(value):
    with pytest.raises(CaptchaError):
        CaptchaToken.parse(value)


def test_token_store_round_trips_privately(tmp_path):
    store = TokenStore(tmp_path / "state" / "token")
    assert store.load() is None
    token = CaptchaToken.parse(_jwt({"exp": 1_800_003_600}))
    store.save(token)
    assert store.load() == token
    assert stat.S_IMODE((tmp_path / "state" / "token").stat().st_mode) == 0o600


def test_an_unreadable_stored_token_is_ignored(tmp_path):
    (tmp_path / "token").write_text("garbage", encoding="utf-8")
    assert TokenStore(tmp_path / "token").load() is None


def test_requested_with_is_the_web_apps_30_minute_slot_hash():
    # Seen in Sofascore's own web app on 2026-11-11 09:17 UTC: x-requested-with 9bf2e5.
    assert requested_with(1_794_388_620.0) == "9bf2e5"
    slot_start = 996_882 * 1800
    assert (
        requested_with(slot_start) == requested_with(slot_start + 1799) != requested_with(slot_start + 1800)
    )


def test_api_calls_carry_x_requested_with():
    transport, session, _ = _transport()
    transport.get(API_URL, {})
    assert session.calls[1][1]["X-Requested-With"] == requested_with(1_794_389_400.0)
