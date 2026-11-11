"""Unit tests for sofascore.client: ETag/304, 404, backoff, refusals and answering a challenge."""

# pylint: disable=missing-function-docstring

import base64
import json

import pytest

from sofascore.captcha import CaptchaError, CaptchaToken
from sofascore.client import DEFAULT_PAUSE_SECONDS, SofascoreBlockedError, SofascoreClient, SofascoreHTTPError
from sofascore.transport import CAPTCHA_HEADER, Response

PATH = "/unique-tournament/264/seasons"
NOW = 1_800_000_000.0
CHALLENGE = Response(403, {}, json.dumps({"error": {"code": 403, "reason": "challenge"}}))


def _jwt(exp):
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.sig"


JWT = _jwt(NOW + 3600)
TOKEN = CaptchaToken(JWT, NOW + 3600)


def ok(data=None, etag=None):
    return Response(200, {"ETag": etag} if etag else {}, json.dumps(data or {}))


class FakeTransport:
    """Returns queued responses in order and records each call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def get(self, url, headers):
        self.calls.append((url, dict(headers)))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


class FakeBrowser(FakeTransport):
    """A browser whose solve hands out ``tokens`` (or raises them) in order."""

    def __init__(self, responses=(), tokens=(TOKEN,)):
        super().__init__(responses)
        self.tokens = list(tokens)
        self.solves = 0

    def solve(self):
        self.solves += 1
        token = self.tokens.pop(0)
        if isinstance(token, Exception):
            raise token
        return token


def _client(*responses, browser=None, clock=lambda: NOW):
    sleeps: list = []
    transport = FakeTransport(responses)
    client = SofascoreClient(transport, browser=browser, sleep=sleeps.append, clock=clock)
    return client, transport, sleeps


def test_200_returns_body_and_etag():
    client, transport, _ = _client(ok({"seasons": []}, etag='W/"a"'))
    result = client.get(PATH)
    assert (result.status, result.data, result.etag) == (200, {"seasons": []}, 'W/"a"')
    assert transport.calls == [("https://www.sofascore.com/api/v1" + PATH, {})]


def test_etag_is_sent_and_304_keeps_it():
    client, transport, _ = _client(Response(304))
    result = client.get(PATH, 'W/"a"')
    assert result.unchanged and result.data is None and result.etag == 'W/"a"'
    assert transport.calls[0][1] == {"If-None-Match": 'W/"a"'}


def test_404_is_not_found_not_an_error():
    client, _, _ = _client(Response(404))
    assert client.get(PATH).not_found


def test_pause_between_requests_is_random_within_the_range():
    picked = []

    def uniform(low, high):
        picked.append((low, high))
        return 4.2

    sleeps: list = []
    client = SofascoreClient(FakeTransport([ok(), ok()]), sleep=sleeps.append, uniform=uniform)
    client.get(PATH)
    client.get(PATH)
    assert picked == [DEFAULT_PAUSE_SECONDS] and sleeps == [4.2]  # no pause before the first request


def test_5xx_backs_off_then_succeeds():
    client, _, sleeps = _client(Response(503), ok({"ok": 1}))
    assert client.get(PATH).data == {"ok": 1}
    low, high = DEFAULT_PAUSE_SECONDS
    assert sleeps[0] == 5.0 and low <= sleeps[1] <= high


@pytest.mark.parametrize("status", [403, 429])
def test_a_plain_refusal_raises_at_once_without_asking_again(status):
    browser = FakeBrowser()
    client, transport, _ = _client(Response(status, {}, '{"error": {"code": 403}}'), ok(), browser=browser)
    with pytest.raises(SofascoreBlockedError, match="refused"):
        client.get(PATH)
    assert len(transport.calls) == 1 and browser.solves == 0


def test_unexpected_status_raises():
    client, _, _ = _client(Response(418))
    with pytest.raises(SofascoreHTTPError):
        client.get(PATH)


def test_retries_exhausted_raise():
    client, _, _ = _client(*[Response(503)] * 6)
    with pytest.raises(SofascoreHTTPError):
        client.get(PATH)


def test_a_challenge_is_solved_and_the_browser_sends_from_then_on():
    browser = FakeBrowser([ok({"ok": 1}), ok({"next": 1})])
    client, transport, sleeps = _client(CHALLENGE, browser=browser)
    assert client.get(PATH).data == {"ok": 1}
    assert client.get(PATH).data == {"next": 1}
    assert len(transport.calls) == 1 and transport.calls[0][1] == {}  # curl only before the challenge
    assert [h for _, h in browser.calls] == [{CAPTCHA_HEADER: JWT}, {CAPTCHA_HEADER: JWT}]
    assert browser.solves == 1 and client.solve_count == 1 and client.browser_request_count == 2
    assert len(sleeps) == 2  # paced as usual, whichever transport sends


def test_the_browser_keeps_the_etag():
    browser = FakeBrowser([Response(304)])
    client, _, _ = _client(CHALLENGE, browser=browser)
    assert client.get(PATH, 'W/"a"').unchanged
    assert browser.calls[0][1] == {"If-None-Match": 'W/"a"', CAPTCHA_HEADER: JWT}


def test_without_a_browser_a_challenge_is_a_block():
    client, transport, _ = _client(CHALLENGE, ok())
    with pytest.raises(SofascoreBlockedError, match="challenge"):
        client.get(PATH)
    assert len(transport.calls) == 1


def test_a_failed_solve_is_a_block():
    browser = FakeBrowser(tokens=[CaptchaError("no cookie")])
    client, _, _ = _client(CHALLENGE, browser=browser)
    with pytest.raises(SofascoreBlockedError, match="could not solve it: no cookie"):
        client.get(PATH)


def test_the_browser_challenged_right_after_a_solve_is_a_block():
    browser = FakeBrowser([CHALLENGE])
    client, _, _ = _client(CHALLENGE, browser=browser)
    with pytest.raises(SofascoreBlockedError, match="challenge"):
        client.get(PATH)
    assert browser.solves == 1


def test_an_expired_token_is_solved_again_in_the_same_browser():
    now = [NOW]
    browser = FakeBrowser([ok(), CHALLENGE, ok({"again": 1})], tokens=[TOKEN, TOKEN])
    client, _, _ = _client(CHALLENGE, browser=browser, clock=lambda: now[0])
    client.get(PATH)
    now[0] += 3600
    assert client.get(PATH).data == {"again": 1}
    assert browser.solves == 2


def test_a_second_challenge_soon_after_a_solve_is_a_block_not_another_solve():
    now = [NOW]
    browser = FakeBrowser([ok(), CHALLENGE], tokens=[TOKEN, TOKEN])
    client, _, _ = _client(CHALLENGE, browser=browser, clock=lambda: now[0])
    client.get(PATH)
    now[0] += 60
    with pytest.raises(SofascoreBlockedError):
        client.get(PATH)
    assert browser.solves == 1


def test_close_closes_transport_and_browser():
    browser = FakeBrowser()
    client, transport, _ = _client(browser=browser)
    client.close()
    assert transport.closed and browser.closed
