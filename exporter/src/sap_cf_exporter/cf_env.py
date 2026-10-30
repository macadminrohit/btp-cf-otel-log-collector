"""Expose Cloud Foundry service-binding credentials to the exporter and Collector.

On Cloud Foundry, secrets belong in a *user-provided service* that is bound to the
app, not in the manifest or in ``cf set-env``.  CF delivers the credentials to the
container as JSON in ``VCAP_SERVICES``.  Both processes in the container (the
exporter and the Splunk OTel Collector sidecar) read plain environment variables,
so the launch scripts run::

    eval "$(python -m sap_cf_exporter.cf_env)"

This module prints ``export NAME='value'`` lines for a fixed allow-list of keys.
Only that allow-list is ever exported, so a service cannot inject arbitrary
variables, and values are shell-quoted.  Nothing is written to stderr except the
*names* of problems; credential values are never logged.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Mapping, Sequence
from typing import Any

DEFAULT_SERVICE_NAME = "sap-cf-exporter-secrets"
SERVICE_NAME_ENV = "CF_EXPORTER_SECRETS_SERVICE"

# credential key in the user-provided service -> environment variable name
CREDENTIAL_ENV_MAP: dict[str, str] = {
    "splunk_access_token": "SPLUNK_ACCESS_TOKEN",
    "cf_bearer_token": "CF_BEARER_TOKEN",
    "cf_username": "CF_USERNAME",
    "cf_password": "CF_PASSWORD",
    "cf_client_id": "CF_CLIENT_ID",
    "cf_client_secret": "CF_CLIENT_SECRET",
    "cf_refresh_token": "CF_REFRESH_TOKEN",
    "otlp_push_token": "OTLP_PUSH_TOKEN",
}


class SecretsServiceError(Exception):
    """The bound service is missing or malformed. The message never contains values."""


def find_credentials(vcap_services: str | None, service_name: str) -> Mapping[str, Any]:
    """Return the ``credentials`` object of the named user-provided service."""
    if not vcap_services:
        raise SecretsServiceError("VCAP_SERVICES is not set; is the app running on Cloud Foundry?")
    try:
        document = json.loads(vcap_services)
    except json.JSONDecodeError as exc:
        raise SecretsServiceError("VCAP_SERVICES is not valid JSON") from exc
    if not isinstance(document, dict):
        raise SecretsServiceError("VCAP_SERVICES has an unexpected shape")

    for instances in document.values():
        if not isinstance(instances, list):
            continue
        for instance in instances:
            if isinstance(instance, dict) and instance.get("name") == service_name:
                credentials = instance.get("credentials")
                if isinstance(credentials, dict):
                    return credentials
                raise SecretsServiceError(f"service '{service_name}' has no credentials object")
    raise SecretsServiceError(f"service '{service_name}' is not bound to this app")


def shell_exports(credentials: Mapping[str, Any]) -> str:
    """Render allow-listed, shell-quoted ``export`` lines. Unknown/empty keys are skipped."""
    lines: list[str] = []
    for key, env_name in CREDENTIAL_ENV_MAP.items():
        value = credentials.get(key)
        if isinstance(value, str) and value:
            lines.append(f"export {env_name}={shlex.quote(value)}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    del argv  # no command-line options; configuration is via environment
    service_name = os.environ.get(SERVICE_NAME_ENV) or DEFAULT_SERVICE_NAME
    try:
        credentials = find_credentials(os.environ.get("VCAP_SERVICES"), service_name)
    except SecretsServiceError as exc:
        print(f"cf_env: {exc}", file=sys.stderr)
        return 1
    exports = shell_exports(credentials)
    if exports:
        print(exports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
