"""Fixture source: replays a saved ``/v3/processes/{guid}/stats`` response (Milestone 1).

The file is re-read on every poll, so it can be edited while the exporter runs to simulate
instances appearing, restarting or crashing.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from sap_cf_exporter.config import Settings
from sap_cf_exporter.errors import APIError
from sap_cf_exporter.models import DiscoveryResult, ProcessTarget


class FixtureSource:
    def __init__(self, path: Path, target: ProcessTarget) -> None:
        self._path = path
        self._target = target

    @classmethod
    def from_settings(cls, settings: Settings) -> FixtureSource:
        return cls(
            settings.fixture_stats_file,
            ProcessTarget(
                app_guid=settings.fixture_app_guid,
                app_name=settings.fixture_app_name,
                org_name=settings.fixture_org_name,
                space_name=settings.fixture_space_name,
                process_guid=settings.fixture_process_guid,
                process_type=settings.fixture_process_type,
            ),
        )

    async def discover(self) -> DiscoveryResult:
        return DiscoveryResult(targets=[self._target], app_count=1)

    async def fetch_stats(self, target: ProcessTarget) -> list[dict[str, Any]]:
        try:
            raw = await asyncio.to_thread(self._path.read_text, encoding="utf-8")
            body = json.loads(raw)
            resources = body["resources"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise APIError(f"Fixture {self._path} is unreadable or malformed") from exc
        if not isinstance(resources, list):
            raise APIError(f"Fixture {self._path} has no 'resources' list")
        return resources
