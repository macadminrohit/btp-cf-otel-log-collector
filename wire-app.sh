#!/usr/bin/env bash
# Point Java apps' OTLP logs at the collector by MERGING four keys into an
# existing user-provided service's credentials. Existing keys (including the
# Observability access token) are preserved and never printed.
#
#   ./wire-app.sh <ups-name> <collector-route-url> [--apps "app1 app2"] [--disable]
#
#   <ups-name>            the service the buildpack reads, e.g. splunk-o11y
#   <collector-route-url> https://<route>   (no path)
#   --apps "a b"          after updating, rolling-restage these apps
#   --disable             rollback: set otel.logs.exporter=none, drop the rest
#
# Ingest token: taken from $OTLP_INGEST_TOKEN if set, otherwise read from the
# collector app named by $COLLECTOR_APP (default otel-log-collector) in the
# currently targeted space.
set -euo pipefail

UPS="${1:?usage: wire-app.sh <ups-name> <collector-route-url> [--apps \"a b\"] [--disable]}"
URL="${2:?collector route url, e.g. https://otel-log-collector-x.cfapps.region.hana.ondemand.com}"
shift 2
APPS=""; DISABLE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --apps) APPS="$2"; shift 2;;
    --disable) DISABLE=1; shift;;
    *) echo "unknown arg $1" >&2; exit 1;;
  esac
done

cf target >/dev/null || { echo "Not logged in." >&2; exit 1; }
GUID="$(cf service "$UPS" --guid)"

if [ "$DISABLE" = 0 ]; then
  if [ -z "${OTLP_INGEST_TOKEN:-}" ]; then
    OTLP_INGEST_TOKEN="$(cf env "${COLLECTOR_APP:-otel-log-collector}" | awk -F': ' '/^OTLP_INGEST_TOKEN:/{print $2}')"
  fi
  [ -n "$OTLP_INGEST_TOKEN" ] || { echo "No ingest token found." >&2; exit 1; }
  export OTLP_INGEST_TOKEN
fi
export COLLECTOR_URL="${URL%/}" DISABLE

umask 077
TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT

cf curl "/v3/service_instances/$GUID/credentials" | python3 -c '
import json, os, sys
d = json.load(sys.stdin)
keys = ["otel.exporter.otlp.logs.protocol", "otel.exporter.otlp.logs.endpoint", "otel.exporter.otlp.logs.headers"]
if os.environ["DISABLE"] == "1":
    d["otel.logs.exporter"] = "none"
    for k in keys: d.pop(k, None)
else:
    d["otel.logs.exporter"] = "otlp"
    d["otel.exporter.otlp.logs.protocol"] = "http/protobuf"
    d["otel.exporter.otlp.logs.endpoint"] = os.environ["COLLECTOR_URL"] + "/v1/logs"
    # %20 not a space: the buildpack passes this through a JVM argument line.
    d["otel.exporter.otlp.logs.headers"] = "Authorization=Bearer%20" + os.environ["OTLP_INGEST_TOKEN"]
json.dump(d, open(sys.argv[1], "w"))
' "$TMP"

cf update-user-provided-service "$UPS" -p "$TMP" >/dev/null
echo "Updated '$UPS'. Keys now present:"
cf curl "/v3/service_instances/$GUID/credentials" | python3 -c '
import json, sys
for k in sorted(json.load(sys.stdin)): print("  ", k)'

if [ -n "$APPS" ]; then
  for a in $APPS; do
    echo "Rolling restage: $a"
    cf restage "$a" --strategy rolling
  done
else
  echo "Not restarted. Credentials are read at staging: cf restage <app> --strategy rolling"
fi
