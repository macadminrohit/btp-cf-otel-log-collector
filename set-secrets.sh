#!/usr/bin/env bash
# Set the collector's configuration on the CURRENTLY TARGETED space.
# Prompts for everything; nothing is written to disk or echoed.
#
#   ./set-secrets.sh [app-name]            # keeps an existing ingest token
#   ./set-secrets.sh [app-name] --rotate   # generates a new ingest token
#
# After changing anything: cf restart <app>  (cf restage is not required).
set -euo pipefail

APP="${1:-otel-log-collector}"
ROTATE="${2:-}"

command -v cf >/dev/null || { echo "cf CLI not found" >&2; exit 1; }
cf target >/dev/null || { echo "Not logged in. Run: cf login -a <api> --sso" >&2; exit 1; }
cf app "$APP" >/dev/null || { echo "App '$APP' not found in the targeted space." >&2; exit 1; }

set_env() { cf set-env "$APP" "$1" "$2" >/dev/null && echo "  set $1"; }

read -r  -p "Splunk HEC URL (https://<host>/services/collector): " HEC_URL
case "$HEC_URL" in https://*/services/collector) ;; *) echo "URL must be https and end in /services/collector" >&2; exit 1;; esac
read -r  -p "Splunk index (must exist and be allowed on the HEC token): " HEC_INDEX
read -rs -p "Splunk HEC token (input hidden): " HEC_TOKEN; echo
[ -n "$HEC_TOKEN" ] || { echo "empty token" >&2; exit 1; }

set_env SPLUNK_HEC_URL "$HEC_URL"
set_env SPLUNK_HEC_INDEX "$HEC_INDEX"
set_env SPLUNK_HEC_TOKEN "$HEC_TOKEN"
unset HEC_TOKEN

# Customer data may be in log bodies. Keep the debug exporter quiet by default.
set_env DEBUG_VERBOSITY basic

EXISTING="$(cf env "$APP" | awk -F': ' '/^OTLP_INGEST_TOKEN:/{print $2}')"
if [ -z "$EXISTING" ] || [ "$ROTATE" = "--rotate" ]; then
  set_env OTLP_INGEST_TOKEN "$(openssl rand -hex 32)"
  echo "  (new OTLP ingest token generated - re-run wire-app.sh for every app)"
else
  echo "  OTLP_INGEST_TOKEN unchanged"
fi
echo "Done. Apply with: cf restart $APP"
