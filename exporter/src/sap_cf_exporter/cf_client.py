"""Thin Cloud Controller V3 API client.

Responsibilities: bearer-token injection, 401 -> refresh -> retry-once, mapping of HTTP status
codes to typed errors, and pagination. Transient failures (429/5xx/network) are retried by
``RetryingHTTP`` underneath.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from sap_cf_exporter.auth import TokenProvider
from sap_cf_exporter.errors import (
    APIError,
    AuthenticationError,
    ForbiddenError,
    NotFoundError,
)
from sap_cf_exporter.retry import RetryingHTTP

PER_PAGE = 200
MAX_PAGES = 1000  # safety net against a pagination loop


def chunked(items: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class CFClient:
    def __init__(self, http: RetryingHTTP, tokens: TokenProvider, base_url: str) -> None:
        self._http = http
        self._tokens = tokens
        self._base_url = base_url.rstrip("/")
        self._host = urlparse(self._base_url).netloc

    # ------------------------------------------------------------------ plumbing
    async def _send(
        self, url: str, endpoint: str, params: Mapping[str, str] | None
    ) -> httpx.Response:
        token = await self._tokens.get_token()
        return await self._http.request(
            "GET",
            url,
            endpoint=endpoint,
            params=params,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )

    async def _get_json(
        self, url: str, endpoint: str, params: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        response = await self._send(url, endpoint, params)
        if response.status_code == 401:
            # The token may have been revoked or expired early: refresh once and retry.
            self._tokens.invalidate()
            response = await self._send(url, endpoint, params)

        status = response.status_code
        if status == 401:
            raise AuthenticationError(
                f"Cloud Foundry rejected the access token for {endpoint} (HTTP 401)",
                status_code=401,
            )
        if status == 403:
            raise ForbiddenError(
                f"Not authorized to call {endpoint} (HTTP 403): the principal lacks the "
                "required org/space role",
                status_code=403,
            )
        if status == 404:
            raise NotFoundError(f"{endpoint}: resource not found (HTTP 404)", status_code=404)
        if status != 200:
            raise APIError(f"Unexpected HTTP {status} from {endpoint}", status_code=status)
        try:
            body = response.json()
        except ValueError as exc:
            raise APIError(f"{endpoint}: response was not valid JSON") from exc
        if not isinstance(body, dict):
            raise APIError(f"{endpoint}: unexpected response shape")
        return body

    async def _pages(
        self, path: str, endpoint: str, params: Mapping[str, str] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield each page of a paginated V3 collection."""
        url: str = f"{self._base_url}{path}"
        query: Mapping[str, str] | None = {"per_page": str(PER_PAGE), **(params or {})}
        for _ in range(MAX_PAGES):
            page = await self._get_json(url, endpoint, query)
            yield page
            next_link = (page.get("pagination") or {}).get("next")
            href = next_link.get("href") if isinstance(next_link, dict) else None
            if not href:
                return
            # Never send the bearer token to a host other than the configured CF API.
            if urlparse(href).netloc != self._host:
                raise APIError(f"{endpoint}: pagination link points to a different host")
            url, query = href, None  # the href already carries the full query string
        raise APIError(f"{endpoint}: exceeded {MAX_PAGES} pages")

    # ------------------------------------------------------------------ resources
    async def list_organizations(self, names: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for chunk in chunked(names, 25) if names else [[]]:
            params = {"names": ",".join(chunk)} if chunk else {}
            async for page in self._pages("/v3/organizations", "organizations", params):
                results.extend(page.get("resources", []))
        return results

    async def list_spaces(
        self, names: list[str], organization_guids: list[str] | None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for org_chunk in chunked(organization_guids, 50) if organization_guids else [[]]:
            for name_chunk in chunked(names, 25) if names else [[]]:
                params: dict[str, str] = {}
                if name_chunk:
                    params["names"] = ",".join(name_chunk)
                if org_chunk:
                    params["organization_guids"] = ",".join(org_chunk)
                async for page in self._pages("/v3/spaces", "spaces", params):
                    results.extend(page.get("resources", []))
        return results

    async def list_apps(
        self,
        *,
        names: list[str] | None = None,
        guids: list[str] | None = None,
        space_guids: list[str] | None = None,
        organization_guids: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """List apps with their space and organization via ``include=space.organization``.

        Returns ``(apps, spaces_by_guid, orgs_by_guid)``. Large filter lists are chunked
        to keep URLs short.
        """
        apps: list[dict[str, Any]] = []
        spaces: dict[str, dict[str, Any]] = {}
        orgs: dict[str, dict[str, Any]] = {}

        # Chunk the longest filter; the remaining filters apply to every request.
        filters = {
            key: values
            for key, values in {
                "guids": guids,
                "space_guids": space_guids,
                "organization_guids": organization_guids,
                "names": names,
            }.items()
            if values
        }
        longest = max(filters, key=lambda key: len(filters[key]), default=None)
        if longest is not None and len(filters[longest]) > 25:
            size = 50 if longest.endswith("guids") else 25
            batches = [{**filters, longest: part} for part in chunked(filters[longest], size)]
        else:
            batches = [filters]

        for batch in batches:
            params = {"include": "space.organization"}
            params.update({key: ",".join(values) for key, values in batch.items()})
            async for page in self._pages("/v3/apps", "apps", params):
                apps.extend(page.get("resources", []))
                included = page.get("included") or {}
                for space in included.get("spaces", []):
                    spaces[space["guid"]] = space
                for org in included.get("organizations", []):
                    orgs[org["guid"]] = org
        return apps, spaces, orgs

    async def list_app_processes(self, app_guid: str) -> list[dict[str, Any]]:
        processes: list[dict[str, Any]] = []
        async for page in self._pages(f"/v3/apps/{app_guid}/processes", "app_processes"):
            processes.extend(page.get("resources", []))
        return processes

    async def get_process_stats(self, process_guid: str) -> list[dict[str, Any]]:
        """``GET /v3/processes/{guid}/stats`` -> list of per-instance resources."""
        body = await self._get_json(
            f"{self._base_url}/v3/processes/{process_guid}/stats", "process_stats"
        )
        resources = body.get("resources")
        if not isinstance(resources, list):
            raise APIError("process_stats: response had no 'resources' list")
        return resources
