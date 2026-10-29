"""Independent polling loop.

* Runs sequentially, so cycles can never overlap.
* Re-discovers apps every ``discovery_interval`` (or early after a 404), polls stats every
  ``poll_interval``.
* Never raises: every failure is logged, counted and reflected in ``ExporterState``.
* Writes to ``SnapshotStore``; ``/metrics`` only ever reads the store.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from sap_cf_exporter.discovery import StatsSource
from sap_cf_exporter.errors import AuthenticationError, NotFoundError, error_reason
from sap_cf_exporter.metrics import ExporterMetrics
from sap_cf_exporter.models import (
    ExporterState,
    ProcessTarget,
    SnapshotStore,
    parse_stats_resources,
)

log = logging.getLogger(__name__)


class _Outcome(Enum):
    OK = "ok"
    GONE = "gone"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CycleResult:
    success: bool
    targets: int
    fetched: int
    failed: int
    duration_seconds: float


class PollingCollector:
    def __init__(
        self,
        source: StatsSource,
        store: SnapshotStore,
        state: ExporterState,
        metrics: ExporterMetrics,
        *,
        poll_interval_seconds: float = 60.0,
        discovery_interval_seconds: float = 300.0,
        concurrency: int = 5,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._store = store
        self._state = state
        self._metrics = metrics
        self._poll_interval = poll_interval_seconds
        self._discovery_interval = discovery_interval_seconds
        self._concurrency = concurrency
        self._clock = clock
        self._monotonic = monotonic

        self._targets: list[ProcessTarget] | None = None
        self._discovered_at = 0.0
        self._rediscover = False
        self._cycle_lock = asyncio.Lock()

    # ------------------------------------------------------------------ public
    async def run(self, stop: asyncio.Event) -> None:
        """Poll until ``stop`` is set."""
        while not stop.is_set():
            started = self._monotonic()
            await self.collect_once()
            delay = max(0.0, self._poll_interval - (self._monotonic() - started))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def collect_once(self) -> CycleResult:
        async with self._cycle_lock:  # defence in depth: no overlapping cycles
            return await self._cycle()

    # ------------------------------------------------------------------ cycle
    async def _cycle(self) -> CycleResult:
        started = self._monotonic()
        state = self._state
        state.last_attempt_epoch = self._clock()

        try:
            discovery_failed = await self._refresh_targets()
            targets = self._targets
            if targets is None:
                return self._finish(started, success=False, targets=0, fetched=0, failed=0)

            semaphore = asyncio.Semaphore(self._concurrency)

            async def fetch(target: ProcessTarget) -> _Outcome:
                async with semaphore:
                    return await self._fetch(target)

            outcomes = await asyncio.gather(*(fetch(t) for t in targets))
            self._store.retain_only(t.process_guid for t in targets)

            fetched = outcomes.count(_Outcome.OK)
            failed = outcomes.count(_Outcome.FAILED)
            if _Outcome.GONE in outcomes:
                self._rediscover = True

            # A cycle succeeds when at least one process was fetched, or when there is
            # genuinely nothing to fetch and discovery itself is healthy.
            success = fetched > 0 or (not targets and not discovery_failed)
            return self._finish(
                started, success=success, targets=len(targets), fetched=fetched, failed=failed
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive anything
            log.exception("Unexpected error in collection cycle")
            self._metrics.record_error("cycle", "unexpected")
            state.last_error = "unexpected error in collection cycle"
            return self._finish(started, success=False, targets=0, fetched=0, failed=0)

    def _finish(
        self, started: float, *, success: bool, targets: int, fetched: int, failed: int
    ) -> CycleResult:
        state = self._state
        duration = self._monotonic() - started
        state.last_cycle_duration_seconds = duration
        state.last_cycle_failed_targets = failed
        if success:
            state.last_success_epoch = self._clock()
            state.initial_collection_complete = True
            state.last_error = None
        log.info(
            "Cycle %s: %d/%d processes fetched, %d failed, %.2fs",
            "ok" if success else "FAILED",
            fetched,
            targets,
            failed,
            duration,
        )
        return CycleResult(success, targets, fetched, failed, duration)

    # ------------------------------------------------------------------ discovery
    async def _refresh_targets(self) -> bool:
        """Re-discover if due. Returns True if a discovery attempt failed this cycle."""
        due = (
            self._targets is None
            or self._rediscover
            or self._monotonic() - self._discovered_at >= self._discovery_interval
        )
        if not due:
            return False
        try:
            result = await self._source.discover()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._record_failure("discovery", exc)
            if self._targets is None:
                log.error("Initial discovery failed; will retry next cycle")
            else:
                log.warning("Discovery failed; continuing with the previous target list")
            return True

        self._targets = result.targets
        self._discovered_at = self._monotonic()
        self._rediscover = False
        self._state.discovered_apps = result.app_count
        self._mark_auth_ok()
        for stage, reason in result.errors:
            self._metrics.record_error(stage, reason)
        log.info("Discovered %d apps, %d processes to poll", result.app_count, len(result.targets))
        return False

    # ------------------------------------------------------------------ stats
    async def _fetch(self, target: ProcessTarget) -> _Outcome:
        try:
            resources = await self._source.fetch_stats(target)
        except asyncio.CancelledError:
            raise
        except NotFoundError:
            # Process deleted since discovery: drop its series now and rediscover early.
            self._store.remove(target.process_guid)
            log.info("Process %s of app %s no longer exists", target.process_guid, target.app_name)
            return _Outcome.GONE
        except Exception as exc:  # noqa: BLE001
            self._record_failure("stats", exc, target)
            return _Outcome.FAILED

        samples, skipped = parse_stats_resources(resources, target)
        for _ in range(skipped):
            self._metrics.record_error("stats", "parse")
        if skipped:
            log.warning("Skipped %d unparseable instance(s) of app %s", skipped, target.app_name)
        self._store.update(target.process_guid, samples, fetched_at=self._clock())
        self._mark_auth_ok()
        return _Outcome.OK

    # ------------------------------------------------------------------ helpers
    def _mark_auth_ok(self) -> None:
        # A successful authenticated API call proves the credentials work. Fixture mode makes
        # no authenticated calls, so auth stays "not applicable" (None).
        if self._state.source == "live":
            self._state.auth_ok = True

    def _record_failure(
        self, stage: str, exc: BaseException, target: ProcessTarget | None = None
    ) -> None:
        reason = error_reason(exc)
        self._metrics.record_error(stage, reason)
        self._state.last_error = f"{stage}: {reason}"
        if isinstance(exc, AuthenticationError):
            self._state.auth_ok = False
        where = f" for app {target.app_name} ({target.process_type})" if target else ""
        # str(exc) is safe: errors never embed tokens, credentials or request URLs.
        log.warning("%s failed%s: %s", stage, where, exc)
