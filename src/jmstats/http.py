"""A deliberately serial, paced JSON client with bounded retries and request budgets."""

from __future__ import annotations

import json
import math
import random
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class Deferred(RuntimeError):
    """The next invocation should resume this unfinished work."""

    def __init__(self, message: str, cooldown: float = 0) -> None:
        super().__init__(message)
        self.cooldown = cooldown


class JsonClient:
    def __init__(
        self,
        base_url: str,
        *,
        interval: float = 2.0,
        budget: int = 600,
        retries: int = 4,
        sleep=time.sleep,
        clock=time.monotonic,
        opener=urlopen,
    ) -> None:
        if interval < 0 or budget < 1 or retries < 0:
            raise ValueError("Invalid HTTP limits")
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("Expected an HTTP(S) API URL")
        self.base_url = base_url.rstrip("/")
        self.interval, self.budget, self.retries = interval, budget, retries
        self.sleep, self.clock, self.opener = sleep, clock, opener
        self.requests = 0
        self.next_request = 0.0

    def get(self, path: str):
        return json.loads(self.fetch(path))

    def text(self, path: str) -> str:
        return self.fetch(path).decode("ascii").strip()

    def fetch(self, path: str) -> bytes:
        for attempt in range(self.retries + 1):
            if self.requests >= self.budget:
                raise Deferred(f"Request budget reached ({self.budget})")
            self.sleep(max(0, self.next_request - self.clock()))
            self.requests += 1
            self.next_request = self.clock() + self.interval
            request = Request(
                self.base_url + path,
                headers={"User-Agent": "joinmarket-stats/0.1", "Accept": "application/json"},
            )
            try:
                with self.opener(request, timeout=45) as response:
                    raw = response.read(32 * 1024 * 1024 + 1)
                    if len(raw) > 32 * 1024 * 1024:
                        raise ValueError("API response exceeds 32 MiB")
                    return raw
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504):
                    raise
                delay = retry_delay(exc.headers.get("Retry-After"), attempt)
                if exc.code == 429:
                    raise Deferred("API HTTP 429, collection paused", max(3600, delay)) from exc
                if attempt == self.retries or delay > 300:
                    raise Deferred(f"API HTTP {exc.code}, defer to next run") from exc
            except (URLError, TimeoutError, ConnectionError) as exc:
                if attempt == self.retries:
                    raise Deferred("API unavailable, defer to next run") from exc
                delay = retry_delay(None, attempt)
            self.sleep(delay)
        raise AssertionError("unreachable")


def retry_delay(value: str | None, attempt: int) -> float:
    """Honor either Retry-After form, never retry earlier than the server requests."""
    if value:
        try:
            seconds = float(value)
            if math.isfinite(seconds):
                return max(0.0, seconds)
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=UTC)
                return max(0.0, (target - datetime.now(UTC)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return min(120, 5 * 2**attempt) + random.uniform(0, 1)
