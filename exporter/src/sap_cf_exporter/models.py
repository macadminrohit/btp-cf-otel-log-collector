"""Domain model: discovery targets, per-instance samples, the snapshot store and health state."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProcessTarget:
    """One Cloud Foundry process whose ``/v3/processes/{guid}/stats`` we poll."""

    app_guid: str
    app_name: str
    org_name: str
    space_name: str
    process_guid: str
    process_type: str


@dataclass(frozen=True, slots=True)
class InstanceSample:
    """Raw values of one process instance, exactly as reported by Cloud Foundry.

    Fields are ``None`` when the API omitted them (e.g. ``usage`` is empty for instances
    that are STARTING or CRASHED). Nothing is derived or rescaled here.
    """

    target: ProcessTarget
    process_type: str
    instance_index: int
    instance_guid: str
    state: str
    uptime_seconds: float | None = None
    cpu_ratio: float | None = None
    cpu_entitlement_ratio: float | None = None
    memory_usage_bytes: float | None = None
    memory_limit_bytes: float | None = None
    disk_usage_bytes: float | None = None
    disk_limit_bytes: float | None = None
    log_rate_bytes_per_second: float | None = None
    usage_timestamp_seconds: float | None = None

    @property
    def running(self) -> bool:
        return self.state.upper() == "RUNNING"

    @property
    def label_values(self) -> tuple[str, ...]:
        """Label values in the order of ``metrics.CONTAINER_LABELS``."""
        t = self.target
        return (
            t.org_name,
            t.space_name,
            t.app_name,
            t.app_guid,
            self.process_type,
            str(self.instance_index),
            self.instance_guid,
        )


@dataclass(slots=True)
class DiscoveryResult:
    targets: list[ProcessTarget]
    app_count: int
    errors: list[tuple[str, str]] = field(default_factory=list)
    """Non-fatal ``(stage, reason)`` problems, e.g. one app returned 403."""


def _number(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` if it is absent or not numeric."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def parse_stats_resource(resource: Mapping[str, Any], target: ProcessTarget) -> InstanceSample:
    """Parse one element of ``resources`` from ``GET /v3/processes/{guid}/stats``.

    Raises ``ValueError`` if the instance index is missing (the instance cannot be
    identified). Every other field is optional.
    """
    index = resource.get("index")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("stats resource has no valid 'index'")

    usage = resource.get("usage")
    if not isinstance(usage, Mapping):
        usage = {}

    process_type = resource.get("type")
    instance_guid = resource.get("instance_guid")
    state = resource.get("state")

    # The CF docs show instances that are not RUNNING (e.g. STARTING) reporting usage as
    # zeros. Those are placeholders, not measurements: publishing them would drag fleet-wide
    # averages down, so usage-derived values are only taken from RUNNING instances.
    if not (isinstance(state, str) and state.upper() == "RUNNING"):
        usage = {}

    return InstanceSample(
        target=target,
        process_type=process_type
        if isinstance(process_type, str) and process_type
        else target.process_type,
        instance_index=index,
        instance_guid=instance_guid
        if isinstance(instance_guid, str) and instance_guid
        else UNKNOWN,
        state=state if isinstance(state, str) and state else UNKNOWN,
        uptime_seconds=_number(resource.get("uptime")),
        cpu_ratio=_number(usage.get("cpu")),
        cpu_entitlement_ratio=_number(usage.get("cpu_entitlement")),
        memory_usage_bytes=_number(usage.get("mem")),
        memory_limit_bytes=_number(resource.get("mem_quota")),
        disk_usage_bytes=_number(usage.get("disk")),
        disk_limit_bytes=_number(resource.get("disk_quota")),
        log_rate_bytes_per_second=_number(usage.get("log_rate")),
        usage_timestamp_seconds=_timestamp(usage.get("time")),
    )


def parse_stats_resources(
    resources: Iterable[Any], target: ProcessTarget
) -> tuple[list[InstanceSample], int]:
    """Parse all instances; returns ``(samples, number_of_unparseable_resources)``."""
    samples: list[InstanceSample] = []
    skipped = 0
    for resource in resources:
        if not isinstance(resource, Mapping):
            skipped += 1
            continue
        try:
            samples.append(parse_stats_resource(resource, target))
        except ValueError:
            skipped += 1
    return samples, skipped


@dataclass(slots=True)
class _TargetEntry:
    fetched_at: float
    samples: tuple[InstanceSample, ...]


class SnapshotStore:
    """Latest successful samples per process, each with the time it was fetched.

    Entries are replaced atomically per process on every successful fetch, so instances that
    disappeared (scale-down, restart with a new GUID) vanish immediately. When a fetch fails
    the previous entry is retained, but ``fresh_samples`` stops returning it once it is older
    than the staleness limit, so stale data is never published indefinitely.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _TargetEntry] = {}

    def update(
        self, process_guid: str, samples: Iterable[InstanceSample], fetched_at: float
    ) -> None:
        self._entries[process_guid] = _TargetEntry(fetched_at, tuple(samples))

    def remove(self, process_guid: str) -> None:
        self._entries.pop(process_guid, None)

    def retain_only(self, process_guids: Iterable[str]) -> None:
        keep = set(process_guids)
        for guid in [g for g in self._entries if g not in keep]:
            del self._entries[guid]

    def fresh_samples(self, now: float, stale_after: float) -> list[InstanceSample]:
        samples: list[InstanceSample] = []
        for entry in list(self._entries.values()):
            if now - entry.fetched_at <= stale_after:
                samples.extend(entry.samples)
        return samples

    def __len__(self) -> int:
        return len(self._entries)


@dataclass(slots=True)
class ExporterState:
    """Mutable health/status shared between the poller, the metrics and the HTTP endpoints."""

    source: str = "live"
    initial_collection_complete: bool = False
    last_success_epoch: float | None = None
    last_attempt_epoch: float | None = None
    last_cycle_duration_seconds: float | None = None
    discovered_apps: int | None = None
    last_cycle_failed_targets: int = 0
    auth_ok: bool | None = None
    """``None`` = not applicable / not yet known (e.g. fixture mode)."""
    last_error: str | None = None

    def is_stale(self, now: float, stale_after: float) -> bool:
        return self.last_success_epoch is None or now - self.last_success_epoch > stale_after
