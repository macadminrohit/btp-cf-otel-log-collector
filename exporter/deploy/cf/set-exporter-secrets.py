"""Create or update the `sap-cf-exporter-secrets` user-provided service without ever printing a secret.

Works on macOS, Linux and Windows (needs Python 3 and the `cf` CLI, already logged in).

  python set-exporter-secrets.py                  # SSO user: uses the refresh token of your `cf login --sso`
  python set-exporter-secrets.py --mode password  # technical user: prompts for username and password
  python set-exporter-secrets.py --dry-run        # show which keys would be stored, change nothing

The collector's OTLP ingest token (the `OTLP_INGEST_TOKEN` env var of the otel-log-collector app) is
read directly from `cf env` and never shown, so nothing is typed or pasted. Use `--collector-app NAME`
for a differently named app, or `--prompt-token` to type it (hidden) instead.

Secrets are written to a temporary owner-only file that is deleted straight away. Nothing is
echoed, logged or passed on a command line.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SERVICE = "sap-cf-exporter-secrets"


def cf_config_path() -> Path:
    base = Path(os.environ.get("CF_HOME") or Path.home())
    return base / ".cf" / "config.json"


def read_refresh_token() -> str:
    path = cf_config_path()
    try:
        token = json.loads(path.read_text(encoding="utf-8-sig")).get("RefreshToken", "")
    except (OSError, ValueError):
        token = ""
    if not token:
        sys.exit(f"No refresh token found in {path}. Run `cf login --sso` first, then retry.")
    return str(token)


def read_collector_token(app: str) -> str | None:
    result = subprocess.run(["cf", "env", app], capture_output=True, text=True, check=False)
    match = re.search(r"^OTLP_INGEST_TOKEN:\s*(\S+)", result.stdout, re.MULTILINE)
    return match.group(1) if match else None


def build_secrets(mode: str, collector_app: str, prompt_token: bool) -> dict[str, str]:
    secrets: dict[str, str] = {}
    if mode == "refresh_token":
        secrets["cf_refresh_token"] = read_refresh_token()
    else:
        secrets["cf_username"] = input("CF technical user name: ").strip()
        secrets["cf_password"] = getpass.getpass("CF technical user password: ")
    ingest = None if prompt_token else read_collector_token(collector_app)
    if ingest:
        print(f"Using OTLP_INGEST_TOKEN read from app '{collector_app}' ({len(ingest)} characters).")
    else:
        if not prompt_token:
            print(f"Could not read OTLP_INGEST_TOKEN from app '{collector_app}'; asking for it instead.")
        ingest = getpass.getpass("Collector OTLP_INGEST_TOKEN: ").strip()
    secrets["otlp_push_token"] = ingest
    empty = [key for key, value in secrets.items() if not value]
    if empty:
        sys.exit(f"Missing value for: {', '.join(empty)}")
    return secrets


def run_cf(*args: str) -> int:
    return subprocess.run(["cf", *args], capture_output=True, text=True, check=False).returncode


def store(secrets: dict[str, str]) -> None:
    fd, name = tempfile.mkstemp(suffix=".json")  # created owner-only (0600) on POSIX
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(secrets, handle)
        updated = run_cf("update-user-provided-service", SERVICE, "-p", name) == 0
        if not updated and run_cf("create-user-provided-service", SERVICE, "-p", name) != 0:
            sys.exit("cf could not create or update the service. Are you logged in (`cf target`)?")
    finally:
        os.remove(name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--mode", choices=["refresh_token", "password"], default="refresh_token")
    parser.add_argument("--collector-app", default="otel-log-collector")
    parser.add_argument("--prompt-token", action="store_true", help="type the ingest token instead")
    parser.add_argument("--dry-run", action="store_true", help="validate and list keys only")
    args = parser.parse_args()

    secrets = build_secrets(args.mode, args.collector_app, args.prompt_token)
    if args.dry_run:
        print(f"Dry run. Would store keys in {SERVICE}: {', '.join(sorted(secrets))}")
        return
    store(secrets)
    print(f"Stored keys in {SERVICE}: {', '.join(sorted(secrets))}")
    print(f"Set CF_AUTH_MODE: {args.mode} in exporter/deploy/cf/manifest.push.yml, then push.")


if __name__ == "__main__":
    main()
