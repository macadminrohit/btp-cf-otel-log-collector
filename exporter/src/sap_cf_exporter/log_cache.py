"""Log Cache as the source of container numbers.

Cloud Foundry keeps the most recent Loggregator envelopes of every app in Log Cache. For each
app instance the platform emits gauges named ``cpu``, ``memory``, ``disk`` and so on, and Log
Cache serves them to any user who can read the app (space developer is enough).

``LogCacheSource`` plugs in where ``CloudFoundrySource`` does: discovery is delegated to it
unchanged, and ``fetch_stats`` returns the Log Cache gauges reshaped into the same per-instance
dicts as ``GET /v3/processes/{guid}/stats``, so parsing, storage, ``/metrics`` and the OTLP push
need no changes.

Unit handling is strict: ``cpu`` and ``cpu_entitlement`` arrive as percentages and are divided
by 100 to match the stats API ratios. A gauge in an unexpected unit is dropped, not guessed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from sap_cf_exporter.auth import TokenProvider
from sap_cf_exporter.discovery import CloudFoundrySource
from sap_cf_exporter.errors import APIError, AuthenticationError, ForbiddenError
from sap_cf_exporter.models import UNKNOWN, DiscoveryResult, ProcessTarget
from sap_cf_exporter.retry import RetryingHTTP

log = logging.getLogger(__name__)

NANOS = 1_000_000_000
READ_LIMIT = 500


class LogCacheClient:
    """Read-only client for ``GET /api/v1/read/{source_id}``."""

    def __init__(self, http: RetryingHTTP, tokens: TokenProvider, base_url: str) -> None:
        self._http = http
        self._tokens = tokens
        self._base_url = base_url.rstrip("/")

    async def _send(self, path: str, params: Mapping[str, str]) -> Any:
        token = await self._tokens.get_token()
        return await self._http.request(
            "GET",
            f"{self._base_url}{path}",
            endpoint="log_cache_read",
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    async def read_gauges(self, source_id: str, *, start_ns: int) -> list[dict[str, Any]]:
        params = {
            "envelope_types": "GAUGE",
            "start_time": str(start_ns),
            "limit": str(READ_LIMIT),
            "descending": "true",
        }
        path = f"/api/v1/read/{source_id}"
        response = await self._send(path, params)
        if response.status_code == 401:
            self._tokens.invalidate()
            response = await self._send(path, params)
        status = response.status_code
        if status == 401:
            raise AuthenticationError(
                "Log Cache rejected the access token (HTTP 401)", status_code=401
            )
        if status == 403:
            raise ForbiddenError(
                "Not authorized to read Log Cache (HTTP 403): the principal cannot read this app",
                status_code=403,
            )
        # A 404 is NOT mapped to NotFoundError: that would make the poller treat the process
        # as deleted and rediscover every cycle. A wrong LOG_CACHE_URL must surface as an error.
        if status != 200:
            raise APIError(f"Unexpected HTTP {status} from Log Cache", status_code=status)
        try:
            body = response.json()
        except ValueError as exc:
            raise APIError("Log Cache: response was not valid JSON") from exc
        # Protobuf-JSON omits empty fields, so "no envelopes" may arrive as {}, {"envelopes": {}}
        # or {"envelopes": {"batch": []}}. All three mean "nothing cached".
        if not isinstance(body, dict):
            raise APIError("Log Cache: unexpected response shape")
        envelopes = body.get("envelopes") or {}
        batch = envelopes.get("batch") if isinstance(envelopes, dict) else None
        if batch is None:
            if envelopes and not isinstance(envelopes, dict):
                raise APIError("Log Cache: unexpected response shape")
            return []
        if not isinstance(batch, list):
            raise APIError("Log Cache: unexpected response shape")
        return [e for e in batch if isinstance(e, dict)]


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _gauge(metrics: Mapping[str, Any], name: str, unit: str) -> float | None:
    entry = metrics.get(name)
    if not isinstance(entry, Mapping) or entry.get("unit") != unit:
        return None
    return _num(entry.get("value"))


def gauges_to_resources(
    envelopes: list[dict[str, Any]], target: ProcessTarget, *, now: float, max_age: float
) -> list[dict[str, Any]]:
    """Reshape Log Cache gauge envelopes into ``/v3/processes/{guid}/stats`` resources."""

    # Newest first, so the first value seen per instance and gauge is the current one.
    def stamp(envelope: dict[str, Any]) -> int:
        try:
            return int(envelope.get("timestamp", 0))
        except (TypeError, ValueError):
            return 0

    ordered = sorted(envelopes, key=stamp, reverse=True)
    oldest_ok = (now - max_age) * NANOS
    per_instance: dict[int, dict[str, Any]] = {}

    for envelope in ordered:
        raw_tags = envelope.get("tags")
        tags: Mapping[str, Any] = raw_tags if isinstance(raw_tags, Mapping) else {}
        process_id = tags.get("process_id")
        process_type = tags.get("process_type")
        # One app's source_id carries all of its processes; keep only this target's.
        if process_id and process_id != target.process_guid:
            continue
        if not process_id and process_type and process_type != target.process_type:
            continue
        try:
            index = int(envelope.get("instance_id"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if index < 0 or stamp(envelope) < oldest_ok:
            continue
        metrics = (envelope.get("gauge") or {}).get("metrics")
        if not isinstance(metrics, Mapping):
            continue

        entry = per_instance.setdefault(
            index,
            {
                "newest": stamp(envelope),
                "type": process_type if isinstance(process_type, str) and process_type else None,
                "guid": tags.get("process_instance_id"),
                "gauges": {},
            },
        )
        for name, value in metrics.items():
            entry["gauges"].setdefault(name, value)  # first seen = newest

    resources: list[dict[str, Any]] = []
    for index, entry in sorted(per_instance.items()):
        merged: dict[str, Any] = entry["gauges"]

        cpu = _gauge(merged, "cpu", "percentage")
        cpu_entitlement = _gauge(merged, "cpu_entitlement", "percentage")
        age_ns = _gauge(merged, "container_age", "nanoseconds")
        usage: dict[str, Any] = {
            "time": datetime.fromtimestamp(entry["newest"] / NANOS, UTC).isoformat(),
        }
        for key, value in (
            ("cpu", None if cpu is None else cpu / 100.0),
            ("cpu_entitlement", None if cpu_entitlement is None else cpu_entitlement / 100.0),
            ("mem", _gauge(merged, "memory", "bytes")),
            ("disk", _gauge(merged, "disk", "bytes")),
            ("log_rate", _gauge(merged, "log_rate", "B/s")),
        ):
            if value is not None:
                usage[key] = value

        resource: dict[str, Any] = {
            "index": index,
            "type": entry["type"] or target.process_type,
            "instance_guid": entry["guid"] or UNKNOWN,
            "state": "RUNNING",  # Log Cache only holds gauges of containers that are running
            "usage": usage,
        }
        for key, value in (
            ("uptime", None if age_ns is None else age_ns / NANOS),
            ("mem_quota", _gauge(merged, "memory_quota", "bytes")),
            ("disk_quota", _gauge(merged, "disk_quota", "bytes")),
        ):
            if value is not None:
                resource[key] = value
        resources.append(resource)
    return resources


class LogCacheSource:
    """``StatsSource`` backed by Log Cache; discovery still goes through the CF API."""

    def __init__(
        self,
        discovery: CloudFoundrySource,
        client: LogCacheClient,
        *,
        max_age_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._discovery = discovery
        self._client = client
        self._max_age = max_age_seconds
        self._clock = clock

    async def discover(self) -> DiscoveryResult:
        return await self._discovery.discover()

    async def fetch_stats(self, target: ProcessTarget) -> list[dict[str, Any]]:
        now = self._clock()
        start_ns = int((now - self._max_age) * NANOS)
        envelopes = await self._client.read_gauges(target.app_guid, start_ns=start_ns)
        return gauges_to_resources(envelopes, target, now=now, max_age=self._max_age)
