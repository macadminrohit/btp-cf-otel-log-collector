"""FastAPI application: ``/metrics``, ``/health``, ``/ready`` and the polling task lifecycle."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from sap_cf_exporter import __version__
from sap_cf_exporter.auth import build_token_provider
from sap_cf_exporter.cf_client import CFClient
from sap_cf_exporter.collector import PollingCollector
from sap_cf_exporter.config import Settings
from sap_cf_exporter.discovery import CloudFoundrySource, DiscoveryScope, StatsSource
from sap_cf_exporter.fixture import FixtureSource
from sap_cf_exporter.log_cache import LogCacheClient, LogCacheSource
from sap_cf_exporter.metrics import ExporterMetrics
from sap_cf_exporter.models import ExporterState, SnapshotStore
from sap_cf_exporter.otlp_push import OTLPPusher
from sap_cf_exporter.retry import RetryingHTTP, RetryPolicy

log = logging.getLogger("sap_cf_exporter")


def build_live_source(
    settings: Settings, http: httpx.AsyncClient, metrics: ExporterMetrics
) -> StatsSource:
    rhttp = RetryingHTTP(
        http,
        policy=RetryPolicy(
            max_retries=settings.max_retries,
            backoff_base_seconds=settings.retry_backoff_base_seconds,
            backoff_max_seconds=settings.retry_backoff_max_seconds,
            max_retry_after_seconds=settings.retry_after_max_seconds,
        ),
        observer=metrics.observe_request,
    )
    tokens = build_token_provider(settings, rhttp)
    client = CFClient(rhttp, tokens, settings.cf_api_url)
    scope = DiscoveryScope(
        org_names=settings.cf_org_names,
        space_names=settings.cf_space_names,
        app_names=settings.cf_app_names,
        app_guids=settings.cf_app_guids,
        shard_count=settings.shard_count,
        shard_index=settings.shard_index,
    )
    discovery = CloudFoundrySource(client, scope, concurrency=settings.max_concurrency)
    if settings.metrics_backend == "log_cache":
        return LogCacheSource(
            discovery,
            LogCacheClient(rhttp, tokens, settings.effective_log_cache_url),
            max_age_seconds=settings.log_cache_max_age_seconds,
        )
    return discovery


def create_app(settings: Settings | None = None, *, source: StatsSource | None = None) -> FastAPI:
    """Application factory. ``source`` may be injected (used by tests)."""
    settings = settings or Settings()
    stale_after = settings.effective_stale_after_seconds
    store = SnapshotStore()
    state = ExporterState(source=settings.exporter_source)
    metrics = ExporterMetrics(store, state, stale_after_seconds=stale_after)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": f"sap-cf-metrics-exporter/{__version__}"},
            follow_redirects=False,
        ) as http:
            if source is not None:
                active_source = source
            elif settings.exporter_source == "fixture":
                active_source = FixtureSource.from_settings(settings)
            else:
                active_source = build_live_source(settings, http, metrics)

            poller = PollingCollector(
                active_source,
                store,
                state,
                metrics,
                poll_interval_seconds=settings.poll_interval_seconds,
                discovery_interval_seconds=settings.discovery_interval_seconds,
                concurrency=settings.max_concurrency,
            )
            stop = asyncio.Event()
            app.state.poller = poller
            app.state.poll_task = asyncio.create_task(poller.run(stop), name="cf-poller")
            push_task: asyncio.Task[None] | None = None
            if settings.otlp_push_endpoint:
                pusher = OTLPPusher(
                    http,
                    settings.otlp_push_endpoint,
                    settings.otlp_push_token,
                    store,
                    metrics,
                    stale_after_seconds=stale_after,
                    environment=settings.deployment_environment,
                    interval_seconds=settings.otlp_push_interval_seconds
                    or settings.poll_interval_seconds,
                )
                push_task = asyncio.create_task(pusher.run(stop), name="otlp-pusher")
            app.state.push_task = push_task
            try:
                yield
            finally:
                stop.set()
                for task in (app.state.poll_task, push_task):
                    if task is None:
                        continue
                    try:
                        await asyncio.wait_for(task, timeout=10)
                    except (TimeoutError, asyncio.CancelledError):
                        task.cancel()

    app = FastAPI(
        title="SAP BTP Cloud Foundry Metrics Exporter",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.exporter_state = state
    app.state.metrics = metrics
    app.state.poll_task = None
    app.state.push_task = None

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        # Renders the cached snapshot only; no Cloud Foundry API call happens here.
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health")
    async def health() -> JSONResponse:
        """Liveness: the process is up and the polling task has not died."""
        task: asyncio.Task[Any] | None = app.state.poll_task
        poller_alive = task is not None and not task.done()
        body = {"status": "ok" if poller_alive else "unhealthy", "poller_running": poller_alive}
        return JSONResponse(body, status_code=200 if poller_alive else 503)

    @app.get("/ready")
    async def ready() -> JSONResponse:
        """Readiness: config valid (we are running), auth valid, first collection done, fresh."""
        now = time.time()
        stale = state.is_stale(now, stale_after)
        auth = "not_applicable" if state.auth_ok is None else ("ok" if state.auth_ok else "failed")
        is_ready = state.initial_collection_complete and state.auth_ok is not False and not stale
        body = {
            "ready": is_ready,
            "config_valid": True,
            "source": state.source,
            "authentication": auth,
            "initial_collection_complete": state.initial_collection_complete,
            "data_stale": stale,
            "last_success_age_seconds": None
            if state.last_success_epoch is None
            else round(now - state.last_success_epoch, 1),
            "stale_after_seconds": stale_after,
            "last_error": state.last_error,
        }
        return JSONResponse(body, status_code=200 if is_ready else 503)

    return app


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # httpx logs full request URLs at INFO; keep those out of the logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main() -> None:
    try:
        settings = Settings()
    except ValidationError as exc:
        # include_input=False: never echo configuration values (they may be secrets).
        print("Invalid configuration:", file=sys.stderr)
        for error in exc.errors(include_input=False, include_url=False):
            location = ".".join(str(part) for part in error["loc"]) or "settings"
            print(f"  - {location}: {error['msg']}", file=sys.stderr)
        raise SystemExit(2) from None

    _configure_logging(settings.log_level)
    log.info(
        "Starting sap-cf-metrics-exporter %s (source=%s, backend=%s, auth_mode=%s, poll=%ss, "
        "otlp_push=%s)",
        __version__,
        settings.exporter_source,
        settings.metrics_backend if settings.exporter_source == "live" else "n/a",
        settings.cf_auth_mode if settings.exporter_source == "live" else "n/a",
        settings.poll_interval_seconds,
        "on" if settings.otlp_push_endpoint else "off",
    )
    uvicorn.run(
        create_app(settings),
        host=settings.exporter_host,
        port=settings.exporter_port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
