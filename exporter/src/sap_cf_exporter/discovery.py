"""Application and process discovery.

``StatsSource`` is the seam between "what to poll" (discovery) and "how to poll it".
``CloudFoundrySource`` is the live implementation; ``fixture.FixtureSource`` replays a saved
response so the exporter can be exercised without credentials.

Discovery order (each step is paginated):

1. (optional) resolve configured org names  -> org GUIDs
2. (optional) resolve configured space names -> space GUIDs
3. list apps in scope, with space + org names via ``include=space.organization``
4. list each app's processes
5. keep processes that can have instances
"""

from __future__ import annotations

import asyncio
import logging
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sap_cf_exporter.cf_client import CFClient
from sap_cf_exporter.errors import ForbiddenError, NotFoundError, error_reason
from sap_cf_exporter.models import UNKNOWN, DiscoveryResult, ProcessTarget

log = logging.getLogger(__name__)


class StatsSource(Protocol):
    async def discover(self) -> DiscoveryResult:
        """Return the processes to poll."""
        ...

    async def fetch_stats(self, target: ProcessTarget) -> list[dict[str, Any]]:
        """Return the raw ``resources`` list of the process' stats."""
        ...


@dataclass(frozen=True, slots=True)
class DiscoveryScope:
    org_names: Sequence[str] = field(default_factory=tuple)
    space_names: Sequence[str] = field(default_factory=tuple)
    app_names: Sequence[str] = field(default_factory=tuple)
    app_guids: Sequence[str] = field(default_factory=tuple)
    shard_count: int = 1
    shard_index: int = 0

    def owns(self, app_guid: str) -> bool:
        """Static sharding: replicas with distinct ``shard_index`` poll disjoint app sets."""
        if self.shard_count <= 1:
            return True
        return zlib.crc32(app_guid.encode()) % self.shard_count == self.shard_index


@dataclass(frozen=True, slots=True)
class AppInfo:
    guid: str
    name: str
    state: str
    org_name: str
    space_name: str


def _guid_of(relationship: Any) -> str | None:
    if isinstance(relationship, Mapping):
        data = relationship.get("data")
        if isinstance(data, Mapping):
            guid = data.get("guid")
            if isinstance(guid, str):
                return guid
    return None


class CloudFoundrySource:
    def __init__(self, client: CFClient, scope: DiscoveryScope, *, concurrency: int = 5) -> None:
        self._client = client
        self._scope = scope
        self._concurrency = concurrency

    # ------------------------------------------------------------------ StatsSource
    async def fetch_stats(self, target: ProcessTarget) -> list[dict[str, Any]]:
        return await self._client.get_process_stats(target.process_guid)

    async def discover(self) -> DiscoveryResult:
        apps = [app for app in await self._find_apps() if self._scope.owns(app.guid)]
        errors: list[tuple[str, str]] = []
        semaphore = asyncio.Semaphore(self._concurrency)

        async def processes_of(app: AppInfo) -> list[ProcessTarget]:
            async with semaphore:
                try:
                    processes = await self._client.list_app_processes(app.guid)
                except (ForbiddenError, NotFoundError) as exc:
                    # Persistent (403) or race with deletion (404): skip this app only.
                    log.warning("Skipping app %s (%s): %s", app.name, error_reason(exc), exc)
                    errors.append(("discovery_processes", error_reason(exc)))
                    return []
            return self._targets_for(app, processes)

        # Any other failure (auth, network, 5xx after retries) propagates so the caller keeps
        # its previous target list instead of silently dropping apps.
        results = await asyncio.gather(*(processes_of(app) for app in apps))
        targets = [target for group in results for target in group]
        return DiscoveryResult(targets=targets, app_count=len(apps), errors=errors)

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _targets_for(app: AppInfo, processes: list[dict[str, Any]]) -> list[ProcessTarget]:
        if app.state.upper() == "STOPPED":
            return []
        targets: list[ProcessTarget] = []
        for process in processes:
            guid, ptype = process.get("guid"), process.get("type")
            if not isinstance(guid, str) or not isinstance(ptype, str):
                continue
            instances = process.get("instances")
            if isinstance(instances, int) and instances == 0:
                continue  # nothing can be running; picked up on a later discovery after scale-up
            targets.append(
                ProcessTarget(
                    app_guid=app.guid,
                    app_name=app.name,
                    org_name=app.org_name,
                    space_name=app.space_name,
                    process_guid=guid,
                    process_type=ptype,
                )
            )
        return targets

    async def _find_apps(self) -> list[AppInfo]:
        scope = self._scope
        client = self._client
        org_guids: list[str] | None = None
        space_guids: list[str] | None = None

        if scope.app_guids:
            raw_apps, spaces, orgs = await client.list_apps(
                guids=list(scope.app_guids), names=list(scope.app_names) or None
            )
            missing = set(scope.app_guids) - {a.get("guid") for a in raw_apps}
            if missing:
                log.warning(
                    "Configured app GUIDs not visible to this principal: %s", sorted(missing)
                )
        else:
            if scope.org_names:
                org_resources = await client.list_organizations(list(scope.org_names))
                org_guids = [o["guid"] for o in org_resources]
                unresolved = set(scope.org_names) - {o.get("name") for o in org_resources}
                if unresolved:
                    log.warning("Configured orgs not found/visible: %s", sorted(unresolved))
                if not org_guids:
                    return []
            if scope.space_names:
                space_resources = await client.list_spaces(list(scope.space_names), org_guids)
                space_guids = [s["guid"] for s in space_resources]
                if not space_guids:
                    log.warning("None of the configured spaces were found: %s", scope.space_names)
                    return []
            raw_apps, spaces, orgs = await client.list_apps(
                names=list(scope.app_names) or None,
                space_guids=space_guids,
                organization_guids=org_guids if space_guids is None else None,
            )

        apps: list[AppInfo] = []
        for raw in raw_apps:
            guid, name = raw.get("guid"), raw.get("name")
            if not isinstance(guid, str) or not isinstance(name, str):
                continue
            space = spaces.get(_guid_of((raw.get("relationships") or {}).get("space")) or "", {})
            org = orgs.get(_guid_of(space.get("relationships", {}).get("organization")) or "", {})
            apps.append(
                AppInfo(
                    guid=guid,
                    name=name,
                    state=str(raw.get("state") or UNKNOWN),
                    org_name=str(org.get("name") or UNKNOWN),
                    space_name=str(space.get("name") or UNKNOWN),
                )
            )
        return apps
