#!/usr/bin/env bash
# Set the collector's configuration on the CURRENTLY TARGETED space.
# Prompts for everything; nothing is written to disk or echoed.
#
#   ./set-secrets.sh [app-name]              # keeps an existing ingest token
#   ./set-secrets.sh [app-name] --rotate     # generates a new ingest token
#   ./set-secrets.sh [app-name] --with-o11y  # also set the Observability Cloud
#                                            # realm and access token
#
# --with-o11y covers what the config.o11y-metrics.yaml and config.secureapp.yaml
# overlays require.
#
# After changing anything: cf restart <app>  (cf restage is not required).
#
# Adding an overlay FILE is different: a file cannot be added to an existing
# droplet, so that needs cf push. Setting OTEL_EXTRA_CONFIG to a file the
# droplet does not contain puts the collector into a crash loop.
set -euo pipefail

APP="${1:-otel-log-collector}"
MODE="${2:-}"
ROTATE="$MODE"

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

if [ "$MODE" = "--with-o11y" ]; then
    # Required by config.o11y-metrics.yaml (container metrics) and by
    # config.secureapp.yaml (Secure Application vulnerability reports).
    read -r  -p "Splunk Observability Cloud realm (for example us0): " O11Y_REALM
    case "$O11Y_REALM" in [a-z][a-z][0-9]*) ;; *) echo "realm looks wrong; expected something like us0" >&2; exit 1;; esac
    read -rs -p "Observability Cloud access token, ingest scope (input hidden): " O11Y_TOKEN; echo
    [ -n "$O11Y_TOKEN" ] || { echo "empty token" >&2; exit 1; }

    # Distinct from the collector's own OTLP_INGEST_TOKEN below. Pasting one in
    # place of the other produces a clean 401 and no other symptom.
    set_env SPLUNK_O11Y_REALM "$O11Y_REALM"
    set_env SPLUNK_O11Y_ACCESS_TOKEN "$O11Y_TOKEN"
    unset O11Y_TOKEN
fi

EXISTING="$(cf env "$APP" | awk -F': ' '/^OTLP_INGEST_TOKEN:/{print $2}')"
if [ -z "$EXISTING" ] || [ "$ROTATE" = "--rotate" ]; then
  set_env OTLP_INGEST_TOKEN "$(openssl rand -hex 32)"
  echo "  (new OTLP ingest token generated - re-run wire-app.sh for every app)"
else
  echo "  OTLP_INGEST_TOKEN unchanged"
fi
echo "Done. Apply with: cf restart $APP"
