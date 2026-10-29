"""Authentication against Cloud Foundry's UAA.

Supported modes (``CF_AUTH_MODE``):

* ``client_credentials`` - a dedicated OAuth client (recommended where SAP permits one).
* ``password``           - a technical user via the public ``cf`` client (what ``cf auth`` uses).
                           Refreshed with the refresh token when UAA returns one.
* ``static_token``       - a manually supplied bearer token. Proof-of-concept only: it cannot
                           be refreshed and expires quickly (about 20 minutes on the SAP BTP trial: see the token's own `exp` claim).

Tokens live in memory only and are never logged. ``get_token`` refreshes proactively
``refresh_skew_seconds`` before expiry and is safe under concurrent callers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from sap_cf_exporter.config import Settings, validate_cf_url
from sap_cf_exporter.errors import APIError, AuthenticationError, ConfigError, ForbiddenError
from sap_cf_exporter.retry import RetryingHTTP

Clock = Callable[[], float]


class TokenProvider(ABC):
    @abstractmethod
    async def get_token(self) -> str:
        """Return a currently valid access token."""

    @abstractmethod
    def invalidate(self) -> None:
        """Discard the cached token (called after the API answered 401)."""


# --------------------------------------------------------------------------------------
# Static token (proof of concept)
# --------------------------------------------------------------------------------------
def jwt_expiry(token: str) -> float | None:
    """Return the ``exp`` claim (epoch seconds) of a JWT, WITHOUT verifying the signature.

    Used only to give a clear "token expired" message early. It is never used to make a
    trust decision; Cloud Foundry validates the token on every request.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        exp = claims.get("exp")
    except (ValueError, AttributeError):
        return None
    return float(exp) if isinstance(exp, int | float) and not isinstance(exp, bool) else None


class StaticTokenProvider(TokenProvider):
    def __init__(self, token: str, *, wall_clock: Clock = time.time) -> None:
        self._token = token.removeprefix("bearer ").removeprefix("Bearer ").strip()
        self._wall_clock = wall_clock
        self._expires_at = jwt_expiry(self._token)

    async def get_token(self) -> str:
        if self._expires_at is not None and self._wall_clock() >= self._expires_at:
            raise AuthenticationError(
                "The static bearer token has expired. Supply a fresh CF_BEARER_TOKEN or "
                "switch to CF_AUTH_MODE=client_credentials/password."
            )
        return self._token

    def invalidate(self) -> None:
        # A static token cannot be refreshed; a 401 will surface as AuthenticationError.
        return None


# --------------------------------------------------------------------------------------
# UAA grants
# --------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ClientCredentialsGrant:
    client_id: str
    client_secret: str

    def form(self) -> dict[str, str]:
        return {"grant_type": "client_credentials"}

    @property
    def client_auth(self) -> tuple[str, str]:
        return (self.client_id, self.client_secret)


@dataclass(frozen=True, slots=True)
class PasswordGrant:
    username: str
    password: str
    client_id: str = "cf"
    client_secret: str = ""

    def form(self) -> dict[str, str]:
        return {"grant_type": "password", "username": self.username, "password": self.password}

    @property
    def client_auth(self) -> tuple[str, str]:
        return (self.client_id, self.client_secret)


Grant = ClientCredentialsGrant | PasswordGrant


@dataclass(slots=True)
class _CachedToken:
    access_token: str
    expires_at: float  # monotonic clock
    refresh_token: str | None


class UAATokenProvider(TokenProvider):
    def __init__(
        self,
        http: RetryingHTTP,
        *,
        cf_api_url: str,
        grant: Grant,
        uaa_url: str | None = None,
        refresh_skew_seconds: float = 60.0,
        clock: Clock = time.monotonic,
    ) -> None:
        self._http = http
        self._cf_api_url = cf_api_url.rstrip("/")
        self._grant = grant
        self._uaa_url = uaa_url.rstrip("/") if uaa_url else None
        self._skew = refresh_skew_seconds
        self._clock = clock
        self._token: _CachedToken | None = None
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        if self._token is not None:
            # Keep the refresh token: it may still allow a cheap refresh.
            self._token.expires_at = float("-inf")

    async def get_token(self) -> str:
        async with self._lock:
            token = self._token
            if token is not None and self._clock() < token.expires_at - self._skew:
                return token.access_token
            self._token = await self._obtain(token.refresh_token if token else None)
            return self._token.access_token

    async def _resolve_uaa_url(self) -> str:
        if self._uaa_url:
            return self._uaa_url
        response = await self._http.request(
            "GET",
            f"{self._cf_api_url}/",
            endpoint="cf_root",
            headers={"Accept": "application/json"},
        )
        if response.status_code != 200:
            raise APIError(
                "Could not discover the UAA URL from the CF API root "
                f"(HTTP {response.status_code})",
                status_code=response.status_code,
            )
        try:
            href = response.json()["links"]["uaa"]["href"]
            self._uaa_url = validate_cf_url(href, name="discovered UAA URL")
        except (KeyError, TypeError, ValueError) as exc:
            raise APIError("CF API root did not advertise a usable UAA URL") from exc
        return self._uaa_url

    async def _obtain(self, refresh_token: str | None) -> _CachedToken:
        uaa_url = await self._resolve_uaa_url()
        if refresh_token:
            try:
                return await self._token_request(
                    uaa_url, {"grant_type": "refresh_token", "refresh_token": refresh_token}
                )
            except (AuthenticationError, APIError):
                pass  # refresh token expired/revoked -> fall back to the full grant
        return await self._token_request(uaa_url, self._grant.form())

    async def _token_request(self, uaa_url: str, form: dict[str, str]) -> _CachedToken:
        response = await self._http.request(
            "POST",
            f"{uaa_url}/oauth/token",
            endpoint="uaa_token",
            data=form,
            auth=self._grant.client_auth,
            headers={"Accept": "application/json"},
        )
        status = response.status_code
        if status in (400, 401):
            raise AuthenticationError(
                f"UAA rejected the credentials (HTTP {status}): {_uaa_error(response)}",
                status_code=status,
            )
        if status == 403:
            raise ForbiddenError(
                f"UAA refused the grant (HTTP 403): {_uaa_error(response)}", status_code=status
            )
        if status != 200:
            raise APIError(f"Unexpected UAA response HTTP {status}", status_code=status)

        try:
            body = response.json()
            access_token = body["access_token"]
            expires_in = float(body.get("expires_in", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise APIError("UAA token response was malformed") from exc
        if not isinstance(access_token, str) or not access_token:
            raise APIError("UAA token response contained no access_token")
        refresh = body.get("refresh_token")
        return _CachedToken(
            access_token=access_token,
            expires_at=self._clock() + expires_in,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
        )


def _uaa_error(response: httpx.Response) -> str:
    """A short, safe description of a UAA error (never echoes request data)."""
    try:
        body: Any = response.json()
        error = str(body.get("error", "unknown_error"))[:60]
        description = str(body.get("error_description", ""))[:200]
    except (ValueError, AttributeError):
        return "unparseable error response"
    return f"{error}: {description}" if description else error


def build_token_provider(settings: Settings, http: RetryingHTTP) -> TokenProvider:
    """Create the token provider selected by ``CF_AUTH_MODE``."""
    mode = settings.cf_auth_mode
    if mode == "static_token":
        assert settings.cf_bearer_token is not None
        return StaticTokenProvider(settings.cf_bearer_token.get_secret_value())

    grant: Grant
    if mode == "client_credentials":
        if not settings.cf_client_id or settings.cf_client_secret is None:
            raise ConfigError("client_credentials requires CF_CLIENT_ID and CF_CLIENT_SECRET")
        grant = ClientCredentialsGrant(
            settings.cf_client_id, settings.cf_client_secret.get_secret_value()
        )
    else:
        if not settings.cf_username or settings.cf_password is None:
            raise ConfigError("password mode requires CF_USERNAME and CF_PASSWORD")
        grant = PasswordGrant(
            username=settings.cf_username,
            password=settings.cf_password.get_secret_value(),
            client_id=settings.cf_client_id or "cf",
            client_secret=settings.cf_client_secret.get_secret_value()
            if settings.cf_client_secret
            else "",
        )
    return UAATokenProvider(
        http, cf_api_url=settings.cf_api_url, grant=grant, uaa_url=settings.cf_uaa_url
    )
