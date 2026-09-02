"""Bounded, privacy-safe HTTP retries for idempotent reads."""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import requests


DEFAULT_TIMEOUT = (10, 30)


class HttpFailure(RuntimeError):
    def __init__(self, source: str, category: str, attempt: int, status: int | None = None):
        self.source = source
        self.category = category
        self.attempt = attempt
        self.status = status
        suffix = f" status={status}" if status is not None else ""
        super().__init__(f"{source}: {category} after attempt {attempt}{suffix}")


@dataclass(frozen=True)
class JsonResponse:
    data: Any
    status_code: int
    headers: dict[str, str]


def _retry_after_seconds(raw: str | None, now: Callable[[], float] = time.time) -> float | None:
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(raw).timestamp() - now())
        except (TypeError, ValueError, OverflowError):
            return None


class HttpClient:
    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = lambda: random.uniform(0, 0.25),
    ) -> None:
        self.session = session or requests.Session()
        self.sleep = sleep
        self.jitter = jitter

    def request_json(
        self,
        method: str,
        url: str,
        *,
        source: str,
        timeout: tuple[float, float] | float = DEFAULT_TIMEOUT,
        idempotent: bool | None = None,
        **kwargs: Any,
    ) -> JsonResponse:
        method = method.upper()
        may_retry = method in {"GET", "HEAD"} if idempotent is None else idempotent
        attempts = 3 if may_retry else 1
        last_failure: HttpFailure | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.request(method, url, timeout=timeout, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_failure = HttpFailure(source, "connection_failure", attempt)
                if attempt == attempts:
                    raise last_failure from exc
                self.sleep((2 ** (attempt - 1)) + self.jitter())
                continue

            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable:
                category = "rate_limited" if response.status_code == 429 else "temporary_failure"
                last_failure = HttpFailure(source, category, attempt, response.status_code)
                if attempt == attempts:
                    raise last_failure
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                if retry_after is not None and retry_after > 60:
                    raise HttpFailure(source, "retry_after_too_long", attempt, response.status_code)
                self.sleep(retry_after if retry_after is not None else 2 ** (attempt - 1) + self.jitter())
                continue
            if response.status_code >= 400:
                raise HttpFailure(source, "http_error", attempt, response.status_code)
            try:
                body = response.json()
            except ValueError as exc:
                raise HttpFailure(source, "invalid_json", attempt, response.status_code) from exc
            return JsonResponse(body, response.status_code, dict(response.headers))
        assert last_failure is not None
        raise last_failure

