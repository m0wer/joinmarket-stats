import io
from urllib.error import HTTPError

import pytest

from jmstats.http import Deferred, JsonClient, retry_delay


def test_pacing_and_budget():
    now = [0.0]
    calls = []

    def sleep(seconds):
        now[0] += seconds

    def open_url(*args, **kwargs):
        calls.append(now[0])
        return io.BytesIO(b'{"ok": true}')

    client = JsonClient(
        "https://example.invalid/api", budget=2, sleep=sleep, clock=lambda: now[0], opener=open_url
    )
    assert client.get("/a") == {"ok": True}
    client.get("/b")
    with pytest.raises(Deferred, match="budget"):
        client.get("/c")
    assert calls == [0, 2]


def test_503_honors_retry_after_and_counts_retries():
    sleeps = []
    calls = []

    def open_url(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise HTTPError("url", 503, "unavailable", {"Retry-After": "17"}, None)
        return io.BytesIO(b"123")

    client = JsonClient("https://example.invalid", interval=0, opener=open_url, sleep=sleeps.append)
    assert client.get("/tip") == 123
    assert 17 in sleeps
    assert client.requests == 2


def test_429_is_immediate_circuit_breaker():
    def open_url(*args, **kwargs):
        raise HTTPError("url", 429, "rate limit", {"Retry-After": "17"}, None)

    client = JsonClient("https://example.invalid", opener=open_url)
    with pytest.raises(Deferred) as error:
        client.get("/tip")
    assert error.value.cooldown >= 3600
    assert client.requests == 1


def test_long_retry_after_defers_without_hammering():
    def open_url(*args, **kwargs):
        raise HTTPError("url", 429, "rate limit", {"Retry-After": "3600"}, None)

    client = JsonClient("https://example.invalid", opener=open_url)
    with pytest.raises(Deferred):
        client.get("/tip")
    assert client.requests == 1


def test_404_not_retried():
    def open_url(*args, **kwargs):
        raise HTTPError("url", 404, "not found", {}, None)

    client = JsonClient("https://example.invalid", opener=open_url)
    with pytest.raises(HTTPError):
        client.get("/missing")
    assert client.requests == 1


def test_retry_after_date():
    assert retry_delay("Thu, 01 Jan 1970 00:00:00 GMT", 0) == 0
    assert 5 <= retry_delay("bad header", 0) <= 6
    assert 5 <= retry_delay("Infinity", 0) <= 6
    assert 5 <= retry_delay("NaN", 0) <= 6
