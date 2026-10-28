#!/usr/bin/env bash
# Send one synthetic OTLP log record through the collector route and print the
# trace_id to search for in Splunk. Also proves the route rejects bad tokens.
#
#   ./smoke-test.sh <collector-route-url> [collector-app-name]
set -euo pipefail

URL="${1:?collector route url, e.g. https://otel-log-collector-x.cfapps.region.hana.ondemand.com}"
APP="${2:-otel-log-collector}"
URL="${URL%/}"

TOKEN="${OTLP_INGEST_TOKEN:-$(cf env "$APP" | awk -F': ' '/^OTLP_INGEST_TOKEN:/{print $2}')}"
[ -n "$TOKEN" ] || { echo "No ingest token." >&2; exit 1; }

TRACE_ID="$(openssl rand -hex 16)"
SPAN_ID="$(openssl rand -hex 8)"
NOW="$(date +%s)000000000"
BODY='{"resourceLogs":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"collector-smoke-test"}}]},"scopeLogs":[{"logRecords":[{"timeUnixNano":"'"$NOW"'","severityNumber":9,"severityText":"INFO","body":{"stringValue":"collector smoke test"},"traceId":"'"$TRACE_ID"'","spanId":"'"$SPAN_ID"'"}]}]}]}'

code() { curl -s -o /dev/null -m 20 -w '%{http_code}' -X POST "$URL/v1/logs" -H 'Content-Type: application/json' "$@" -d "$BODY"; }

echo "no token      -> $(code)            (expect 401)"
echo "wrong token   -> $(code -H 'Authorization: Bearer wrong')   (expect 401)"
echo "correct token -> $(code -H "Authorization: Bearer $TOKEN")   (expect 200)"
echo
echo "Search Splunk (last 15 min):"
echo "  index=<your-index> trace_id=$TRACE_ID"
