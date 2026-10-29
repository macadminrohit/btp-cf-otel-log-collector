"""Optional push of the container metrics to an OpenTelemetry Collector over OTLP/HTTP (JSON).

Use this when a Collector is already running in the same Cloud Foundry space and its OTLP
receiver is reachable through a route: the exporter then needs no Collector of its own. The
Prometheus ``/metrics`` endpoint keeps working in parallel.

Shape of what is sent (kept deliberately small, because Splunk Observability Cloud limits the
number of NEW dimension key names an org may create per week):

* resource attributes: ``service.name`` (the CF app name) and ``deployment.environment.name``
* data point attributes: ``cf_instance_index``, plus ``cf_process_type`` for non-``web`` processes
* metric names: the same ``sap_cf_container_*`` gauges as ``/metrics``

The bearer token is only ever sent in the ``Authorization`` header and never logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from sap_cf_exporter import __version__
from sap_cf_exporter.metrics import CONTAINER_METRICS, ExporterMetrics
from sap_cf_exporter.models import InstanceSample, SnapshotStore

log = logging.getLogger(__name__)

NANOS = 1_000_000_000
DEFAULT_PATH = "/v1/metrics"


def _attr(key: str, value: str) -> dict[str, Any]:
    return {"key": key, "value": {"stringValue": value}}


def build_payload(
    samples: list[InstanceSample], *, now: float, environment: str
) -> dict[str, Any] | None:
    """OTLP ``ExportMetricsServiceRequest`` as JSON, or ``None`` when there is nothing to send."""
    by_app: dict[str, list[InstanceSample]] = defaultdict(list)
    for sample in samples:
        by_app[sample.target.app_name].append(sample)

    time_ns = str(int(now * NANOS))
    resource_metrics: list[dict[str, Any]] = []
    for app_name, app_samples in sorted(by_app.items()):
        metrics: list[dict[str, Any]] = []
        for spec in CONTAINER_METRICS:
            points: list[dict[str, Any]] = []
            for sample in app_samples:
                value = spec.value(sample)
                if value is None:
                    continue
                attributes = [_attr("cf_instance_index", str(sample.instance_index))]
                if sample.process_type != "web":
                    attributes.append(_attr("cf_process_type", sample.process_type))
                points.append(
                    {"attributes": attributes, "timeUnixNano": time_ns, "asDouble": value}
                )
            if points:
                metrics.append(
                    {
                        "name": spec.name,
                        "description": spec.documentation,
                        "gauge": {"dataPoints": points},
                    }
                )
        if not metrics:
            continue
        resource_metrics.append(
            {
                "resource": {
                    "attributes": [
                        _attr("service.name", app_name),
                        _attr("deployment.environment.name", environment),
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "sap-cf-metrics-exporter", "version": __version__},
                        "metrics": metrics,
                    }
                ],
            }
        )
    return {"resourceMetrics": resource_metrics} if resource_metrics else None


def normalise_endpoint(endpoint: str) -> str:
    """Append the standard OTLP path when the configured URL has none."""
    return endpoint if urlparse(endpoint).path.strip("/") else endpoint.rstrip("/") + DEFAULT_PATH


class OTLPPusher:
    def __init__(
        self,
        http: httpx.AsyncClient,
        endpoint: str,
        token: SecretStr | None,
        store: SnapshotStore,
        metrics: ExporterMetrics,
        *,
        stale_after_seconds: float,
        environment: str,
        interval_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = http
        self._endpoint = normalise_endpoint(endpoint)
        self._token = token
        self._store = store
        self._metrics = metrics
        self._stale_after = stale_after_seconds
        self._environment = environment
        self._interval = interval_seconds
        self._clock = clock

    async def push_once(self) -> bool:
        """Send the current snapshot. Returns True when it was accepted (or empty)."""
        now = self._clock()
        payload = build_payload(
            self._store.fresh_samples(now, self._stale_after),
            now=now,
            environment=self._environment,
        )
        if payload is None:
            return True  # nothing fresh to send; staleness is visible on /ready
        headers = {"Content-Type": "application/json"}
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.get_secret_value()}"
        try:
            response = await self._http.post(self._endpoint, json=payload, headers=headers)
        except asyncio.CancelledError:
            raise
        except httpx.HTTPError as exc:
            self._metrics.record_error("otlp_push", "network")
            log.warning("OTLP push failed: %s", type(exc).__name__)
            return False
        if response.status_code // 100 != 2:
            self._metrics.record_error("otlp_push", f"http_{response.status_code}")
            log.warning("OTLP push rejected: HTTP %d", response.status_code)
            return False
        return True

    async def run(self, stop: asyncio.Event) -> None:
        # Let the first poll finish before the first push.
        try:
            await asyncio.wait_for(stop.wait(), timeout=min(self._interval, 15.0))
        except TimeoutError:
            pass
        while not stop.is_set():
            try:
                await self.push_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("Unexpected error in OTLP push")
                self._metrics.record_error("otlp_push", "unexpected")
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                pass
