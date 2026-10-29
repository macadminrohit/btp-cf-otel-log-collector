"""HTTP retry layer: exponential backoff with jitter, Retry-After handling.

``RetryingHTTP`` wraps an ``httpx.AsyncClient``. It retries only what is safe and useful to
retry (network errors, timeouts, HTTP 429 and HTTP 5xx) and returns every other response
(2xx, 401, 403, 404, other 4xx) untouched so that callers can apply their own semantics.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from sap_cf_exporter.errors import CFError, NetworkError, RateLimitedError, ServerError

RequestObserver = Callable[[str, str], None]
"""Called as ``observer(endpoint, code)`` after every HTTP attempt.

``endpoint`` is a low-cardinality logical name (e.g. ``process_stats``) and ``code`` is the
HTTP status code as a string, or ``"error"`` for transport failures.
"""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 4
    backoff_base_seconds: float = 1.0
    backoff_max_seconds: float = 30.0
    max_retry_after_seconds: float = 120.0


def backoff_delay(policy: RetryPolicy, attempt: int, rng: random.Random) -> float:
    """Exponential backoff with "equal jitter": half fixed, half random.

    ``attempt`` is zero-based. Equal jitter avoids retries clustering at ~0 s while still
    de-synchronising multiple clients.
    """
    ceiling = min(policy.backoff_max_seconds, policy.backoff_base_seconds * (2**attempt))
    return float(ceiling / 2 + rng.uniform(0, ceiling / 2))


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    return max(0.0, seconds)


class RetryingHTTP:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        policy: RetryPolicy | None = None,
        observer: RequestObserver | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._client = client
        self._policy = policy or RetryPolicy()
        self._observer = observer
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter only, not security-relevant

    def _observe(self, endpoint: str, code: str) -> None:
        if self._observer is not None:
            self._observer(endpoint, code)

    async def request(
        self, method: str, url: str, *, endpoint: str, **kwargs: Any
    ) -> httpx.Response:
        """Send a request, retrying transient failures.

        Raises ``NetworkError``, ``ServerError`` or ``RateLimitedError`` once retries are
        exhausted. Any other response is returned to the caller.
        """
        policy = self._policy
        last_error: CFError | None = None

        for attempt in range(policy.max_retries + 1):
            delay = backoff_delay(policy, attempt, self._rng)
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                # Only the exception class is reported: messages can embed URLs.
                self._observe(endpoint, "error")
                last_error = NetworkError(f"{type(exc).__name__} calling {endpoint}")
            else:
                status = response.status_code
                self._observe(endpoint, str(status))
                if status == 429:
                    retry_after = parse_retry_after(response.headers.get("retry-after"))
                    if retry_after is not None and retry_after > policy.max_retry_after_seconds:
                        # Waiting would exceed our budget; surface it and try next cycle.
                        raise RateLimitedError(
                            f"Rate limited calling {endpoint}; Retry-After={retry_after:.0f}s "
                            "exceeds the configured maximum wait",
                            retry_after=retry_after,
                        )
                    last_error = RateLimitedError(
                        f"Rate limited calling {endpoint}", retry_after=retry_after
                    )
                    if retry_after is not None:
                        delay = retry_after
                elif status >= 500:
                    last_error = ServerError(
                        f"HTTP {status} calling {endpoint}", status_code=status
                    )
                    retry_after = parse_retry_after(response.headers.get("retry-after"))
                    if retry_after is not None:
                        delay = min(retry_after, policy.max_retry_after_seconds)
                else:
                    return response

            if attempt < policy.max_retries:
                await self._sleep(delay)

        assert last_error is not None
        raise last_error
