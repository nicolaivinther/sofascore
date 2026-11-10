"""Unit tests for sofascore.client: ETag/304, 404, backoff and the 403 stop."""

# pylint: disable=missing-function-docstring

import pytest

from sofascore.client import DEFAULT_PAUSE_SECONDS, SofascoreBlockedError, SofascoreClient, SofascoreHTTPError

PATH = "/unique-tournament/264/seasons"


class FakeResponse:
    """The bits of a requests.Response the client reads."""

    def __init__(self, status, data=None, etag=None):
        self.status_code = status
        self._data = data
        self.headers = {"ETag": etag} if etag else {}

    def json(self):
        return self._data


class FakeSession:
    """Returns queued responses in order and records each call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        return self.responses.pop(0)


def _client(*responses):
    sleeps: list = []
    session = FakeSession(responses)
    return SofascoreClient(session, sleep=sleeps.append), session, sleeps


def test_200_returns_body_and_etag():
    client, session, _ = _client(FakeResponse(200, {"seasons": []}, etag='W/"a"'))
    result = client.get(PATH)
    assert (result.status, result.data, result.etag) == (200, {"seasons": []}, 'W/"a"')
    assert session.calls[0][0] == "https://www.sofascore.com/api/v1" + PATH
    assert session.calls[0][1] == {}


def test_etag_is_sent_and_304_keeps_it():
    client, session, _ = _client(FakeResponse(304))
    result = client.get(PATH, 'W/"a"')
    assert result.unchanged and result.data is None and result.etag == 'W/"a"'
    assert session.calls[0][1] == {"If-None-Match": 'W/"a"'}


def test_404_is_not_found_not_an_error():
    client, _, _ = _client(FakeResponse(404))
    assert client.get(PATH).not_found


def test_pause_between_requests_is_random_within_the_range():
    picked = []
    session = FakeSession([FakeResponse(200, {}), FakeResponse(200, {})])
    sleeps: list = []

    def uniform(low, high):
        picked.append((low, high))
        return 4.2

    client = SofascoreClient(session, sleep=sleeps.append, uniform=uniform)
    client.get(PATH)
    client.get(PATH)
    assert picked == [DEFAULT_PAUSE_SECONDS] and sleeps == [4.2]  # no pause before the first request


def test_5xx_backs_off_then_succeeds():
    client, _, sleeps = _client(FakeResponse(503), FakeResponse(200, {"ok": 1}))
    assert client.get(PATH).data == {"ok": 1}
    low, high = DEFAULT_PAUSE_SECONDS
    assert sleeps[0] == 5.0 and low <= sleeps[1] <= high


def test_a_403_raises_at_once_without_asking_again():
    client, session, _ = _client(FakeResponse(403), FakeResponse(200, {}))
    with pytest.raises(SofascoreBlockedError):
        client.get(PATH)
    assert len(session.calls) == 1


def test_a_429_is_a_rate_limit_too():
    client, session, _ = _client(FakeResponse(429), FakeResponse(200, {}))
    with pytest.raises(SofascoreBlockedError):
        client.get(PATH)
    assert len(session.calls) == 1


def test_unexpected_status_raises():
    client, _, _ = _client(FakeResponse(418))
    with pytest.raises(SofascoreHTTPError):
        client.get(PATH)


def test_retries_exhausted_raise():
    client, _, _ = _client(*[FakeResponse(503)] * 6)
    with pytest.raises(SofascoreHTTPError):
        client.get(PATH)
