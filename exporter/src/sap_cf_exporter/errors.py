"""Exception hierarchy.

Every ``CFError`` carries a short, low-cardinality ``reason`` string that is used as a
Prometheus label value on ``sap_cf_exporter_scrape_errors_total``.
"""

from __future__ import annotations


class ExporterError(Exception):
    """Base class for all exporter errors."""


class ConfigError(ExporterError):
    """Invalid or incomplete configuration."""


class CFError(ExporterError):
    """An error talking to the Cloud Foundry API or UAA."""

    reason: str = "api_error"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthenticationError(CFError):
    """Credentials rejected (HTTP 401 / UAA refused the grant)."""

    reason = "auth"


class ForbiddenError(CFError):
    """Authenticated but not authorized for the resource (HTTP 403)."""

    reason = "forbidden"


class NotFoundError(CFError):
    """Resource does not exist or is no longer visible (HTTP 404)."""

    reason = "not_found"


class RateLimitedError(CFError):
    """HTTP 429 persisted after retries (or Retry-After exceeded the allowed wait)."""

    reason = "rate_limited"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = 429,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after = retry_after


class ServerError(CFError):
    """HTTP 5xx persisted after retries."""

    reason = "server_error"


class NetworkError(CFError):
    """Connection / timeout failures persisted after retries."""

    reason = "network"


class APIError(CFError):
    """Any other unexpected API response (other 4xx, malformed payload, ...)."""

    reason = "api_error"


def error_reason(exc: BaseException) -> str:
    """Map an exception to a low-cardinality reason label."""
    if isinstance(exc, CFError):
        return exc.reason
    return "unexpected"
