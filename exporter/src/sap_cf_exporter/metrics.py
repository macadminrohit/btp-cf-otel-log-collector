"""Prometheus exposition.

``SnapshotCollector`` is a custom collector: on every ``/metrics`` scrape it renders the cached
snapshot into gauges. Because series are rebuilt from the snapshot each time:

* a scrape never calls the Cloud Foundry API,
* instances that disappear leave no stale series behind (nothing to ``.remove()``), and
* a process whose last successful fetch is older than ``stale_after`` is simply not rendered.

Operational counters (API requests, errors) are ordinary ``prometheus_client`` counters.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, ProcessCollector
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector

from sap_cf_exporter import __version__
from sap_cf_exporter.models import ExporterState, InstanceSample, SnapshotStore

CONTAINER_LABELS: tuple[str, ...] = (
    "cf_org_name",
    "cf_space_name",
    "cf_app_name",
    "cf_app_guid",
    "cf_process_type",
    "cf_instance_index",
    "cf_instance_guid",
)


@dataclass(frozen=True, slots=True)
class ContainerMetric:
    name: str
    documentation: str
    value: Callable[[InstanceSample], float | None]


CONTAINER_METRICS: tuple[ContainerMetric, ...] = (
    ContainerMetric(
        "sap_cf_container_cpu_usage_ratio",
        "Raw usage.cpu from the CF process stats API: 'the current cpu usage of the "
        "instance'. The CF API documentation does not state the unit or denominator, so the "
        "value is passed through unchanged (no scaling).",
        lambda s: s.cpu_ratio,
    ),
    ContainerMetric(
        "sap_cf_container_cpu_entitlement_ratio",
        "Raw usage.cpu_entitlement from the CF process stats API: 'the current cpu usage "
        "relative to the instance's cpu entitlement'. It is a usage ratio, NOT the entitlement "
        "or a quota, and is passed through unchanged (no scaling or clamping).",
        lambda s: s.cpu_entitlement_ratio,
    ),
    ContainerMetric(
        "sap_cf_container_memory_usage_bytes",
        "Container memory in use (usage.mem), bytes.",
        lambda s: s.memory_usage_bytes,
    ),
    ContainerMetric(
        "sap_cf_container_memory_limit_bytes",
        "Container memory quota (mem_quota), bytes.",
        lambda s: s.memory_limit_bytes,
    ),
    ContainerMetric(
        "sap_cf_container_disk_usage_bytes",
        "Container disk in use (usage.disk), bytes.",
        lambda s: s.disk_usage_bytes,
    ),
    ContainerMetric(
        "sap_cf_container_disk_limit_bytes",
        "Container disk quota (disk_quota), bytes.",
        lambda s: s.disk_limit_bytes,
    ),
    ContainerMetric(
        "sap_cf_container_uptime_seconds",
        "Seconds since the instance started (uptime).",
        lambda s: s.uptime_seconds,
    ),
    ContainerMetric(
        "sap_cf_container_log_rate_bytes_per_second",
        "Log throughput of the instance (usage.log_rate), bytes per second.",
        lambda s: s.log_rate_bytes_per_second,
    ),
    ContainerMetric(
        "sap_cf_container_instance_running",
        "1 if the instance state is RUNNING, otherwise 0 (STARTING, CRASHED, DOWN, ...).",
        lambda s: 1.0 if s.running else 0.0,
    ),
    ContainerMetric(
        "sap_cf_container_usage_timestamp_seconds",
        "Unix time in usage.time ('the time when the usage was requested' per the CF API "
        "docs). Compare with scrape time to detect usage that Cloud Foundry has not refreshed.",
        lambda s: s.usage_timestamp_seconds,
    ),
)


class SnapshotCollector(Collector):
    def __init__(
        self,
        store: SnapshotStore,
        state: ExporterState,
        *,
        stale_after_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._state = state
        self._stale_after = stale_after_seconds
        self._clock = clock

    def collect(self) -> Iterable[Metric]:
        now = self._clock()
        samples = self._store.fresh_samples(now, self._stale_after)

        families = {
            spec.name: GaugeMetricFamily(spec.name, spec.documentation, labels=CONTAINER_LABELS)
            for spec in CONTAINER_METRICS
        }
        for sample in samples:
            labels = list(sample.label_values)
            for spec in CONTAINER_METRICS:
                value = spec.value(sample)
                if value is not None:
                    families[spec.name].add_metric(labels, value)
        yield from families.values()

        yield from self._exporter_gauges(now, len(samples))

    def _exporter_gauges(self, now: float, instance_count: int) -> Iterable[Metric]:
        state = self._state

        info = GaugeMetricFamily(
            "sap_cf_exporter_info",
            "Exporter build and data-source information.",
            labels=("version", "source"),
        )
        info.add_metric([__version__, state.source], 1)
        yield info

        if state.last_success_epoch is not None:
            last_success = GaugeMetricFamily(
                "sap_cf_exporter_last_success_timestamp_seconds",
                "Unix time of the last successful collection cycle.",
            )
            last_success.add_metric([], state.last_success_epoch)
            yield last_success

        stale = GaugeMetricFamily(
            "sap_cf_exporter_data_stale",
            "1 if there has been no successful collection within the staleness limit "
            "(container series are withheld for processes whose data is older than the limit).",
        )
        stale.add_metric([], 1.0 if state.is_stale(now, self._stale_after) else 0.0)
        yield stale

        if state.discovered_apps is not None:
            apps = GaugeMetricFamily(
                "sap_cf_exporter_discovered_apps", "Applications in scope at the last discovery."
            )
            apps.add_metric([], state.discovered_apps)
            yield apps

        instances = GaugeMetricFamily(
            "sap_cf_exporter_discovered_instances",
            "Process instances currently published on /metrics.",
        )
        instances.add_metric([], instance_count)
        yield instances

        if state.last_cycle_duration_seconds is not None:
            duration = GaugeMetricFamily(
                "sap_cf_exporter_scrape_duration_seconds",
                "Duration of the last collection cycle against the CF API.",
            )
            duration.add_metric([], state.last_cycle_duration_seconds)
            yield duration

        failed = GaugeMetricFamily(
            "sap_cf_exporter_last_cycle_failed_targets",
            "Processes whose stats could not be fetched in the last cycle.",
        )
        failed.add_metric([], state.last_cycle_failed_targets)
        yield failed


class ExporterMetrics:
    """Registry plus the operational counters; pass ``observe_request`` to the HTTP layer."""

    def __init__(
        self,
        store: SnapshotStore,
        state: ExporterState,
        *,
        stale_after_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.registry = CollectorRegistry()
        self.api_requests = Counter(
            "sap_cf_exporter_api_requests_total",
            "HTTP requests made to Cloud Foundry / UAA, by logical endpoint and status code.",
            ("endpoint", "code"),
            registry=self.registry,
        )
        self.scrape_errors = Counter(
            "sap_cf_exporter_scrape_errors_total",
            "Errors while collecting from Cloud Foundry, by stage and reason.",
            ("stage", "reason"),
            registry=self.registry,
        )
        ProcessCollector(registry=self.registry)
        self.registry.register(
            SnapshotCollector(store, state, stale_after_seconds=stale_after_seconds, clock=clock)
        )

    def observe_request(self, endpoint: str, code: str) -> None:
        self.api_requests.labels(endpoint=endpoint, code=code).inc()

    def record_error(self, stage: str, reason: str) -> None:
        self.scrape_errors.labels(stage=stage, reason=reason).inc()
