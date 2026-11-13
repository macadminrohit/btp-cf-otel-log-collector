#!/usr/bin/env python3
"""Report whether a splunk-o11y service instance is sufficient for a Node.js app.

Prints credential key NAMES and value LENGTHS only, never a value, so the output
is safe to paste into a ticket or a chat.

    python3 check-splunk-binding.py [instance-name]

Requires the cf CLI, logged in and targeted at the space holding the instance.
"""

import json
import shutil
import subprocess
import sys

# What the Node.js shim (app/splunk-binding.js) maps, in its order of preference.
REQUIRED = [
    ("realm", ["realm", "splunk.realm"]),
    ("access token", ["access_token", "splunk.access.token"]),
    # Either the global or the per-signal key is fine. Instances built for Java
    # usually carry the per-signal one.
    ("OTLP protocol", ["otel.exporter.otlp.protocol", "otel.exporter.otlp.logs.protocol"]),
    ("logs endpoint", ["otel.exporter.otlp.logs.endpoint"]),
    ("logs auth header", ["otel.exporter.otlp.logs.headers"]),
]


def cf_curl(path):
    out = subprocess.run(
        ["cf", "curl", path], capture_output=True, text=True, check=True
    ).stdout
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        sys.exit(f"cf curl {path} did not return JSON:\n{out.strip()[:400]}")


def main():
    instance = sys.argv[1] if len(sys.argv) > 1 else "splunk-o11y"

    if not shutil.which("cf"):
        sys.exit("cf CLI not found on PATH")

    found = cf_curl(f"/v3/service_instances?names={instance}").get("resources", [])
    if not found:
        sys.exit(
            f"no service instance named {instance!r} in the targeted space.\n"
            "Check with: cf services"
        )
    guid = found[0]["guid"]

    print(f"instance: {instance}")

    bindings = cf_curl(
        f"/v3/service_credential_bindings?service_instance_guids={guid}"
    ).get("resources", [])
    print(f"bindings: {len(bindings)}")
    for b in bindings:
        app_guid = b.get("relationships", {}).get("app", {}).get("data", {}).get("guid")
        name = b.get("name")
        if not name and app_guid:
            apps = cf_curl(f"/v3/apps/{app_guid}").get("name")
            name = apps or app_guid
        print(f"  - {name}")

    creds = cf_curl(f"/v3/service_instances/{guid}/credentials")

    print()
    print("credential keys (names and lengths only):")
    for key in sorted(creds):
        print(f"  {key}: {len(str(creds[key]))} chars")

    print()
    print("Node.js readiness:")
    missing = []
    for label, keys in REQUIRED:
        hit = next((k for k in keys if creds.get(k)), None)
        if hit:
            print(f"  OK       {label}  <- {hit}")
        else:
            print(f"  MISSING  {label}  (expected one of: {', '.join(keys)})")
            missing.append(label)

    print()
    if missing:
        print("Needs these added before Secure Application and logs will work:")
        for label in missing:
            print(f"  - {label}")
        print()
        print("Traces need only realm + access token. The logs endpoint and auth")
        print("header are what carry Secure Application reports and application")
        print("logs to your collector; without them those two signals go nowhere.")
    else:
        print("Ready. Bind a Node.js app to this instance and restage.")

    # The two mistakes that actually cost us time on this engagement.
    print()
    print("checks:")
    header = str(creds.get("otel.exporter.otlp.logs.headers", ""))
    if header:
        if "%20" not in header:
            print("  WARN  logs header has no %20 -- the space in Bearer <token>")
            print("        must be URL-encoded or header parsing breaks")
        else:
            print("  OK    logs header uses %20")
        if not header.lower().startswith("authorization="):
            print("  WARN  logs header does not start with Authorization=")

    endpoint = str(creds.get("otel.exporter.otlp.logs.endpoint", ""))
    if endpoint:
        if not endpoint.rstrip("/").endswith("/v1/logs"):
            print("  WARN  logs endpoint does not end in /v1/logs")
        else:
            print("  OK    logs endpoint path looks right")

    realm = str(creds.get("realm") or creds.get("splunk.realm") or "")
    token = str(creds.get("access_token") or creds.get("splunk.access.token") or "")
    if realm and token:
        print(f"  note  realm is {realm}; spans will only appear in the {realm} org")
    if token and len(token) > 40:
        print("  WARN  access token is unusually long for an ingest token -- check")
        print("        it is not the collector token by mistake")


if __name__ == "__main__":
    main()
