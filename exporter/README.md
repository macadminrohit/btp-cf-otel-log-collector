# sap-cf-metrics-exporter (runtime files)

Small Cloud Foundry app that reads container metrics (CPU, memory, disk, uptime, log rate) of your
apps from **Log Cache** and pushes them to the OpenTelemetry Collector already running in your
space, which forwards them to Splunk Observability Cloud.

- Needs only an ordinary user login (Space Developer / Auditor). **No UAA client with `logs.admin`**:
  SAP does not issue that on SAP BTP, so the Collector's `cloud_foundry` receiver is not used.
- No Collector inside this app: it pushes OTLP/HTTP (JSON) to the collector's `/v1/metrics` route with
  the same bearer token the apps use for logs.
- Runs as a 256 MB app with no route.

Deployment steps: [`../RUNBOOK-cf-metrics.md`](../RUNBOOK-cf-metrics.md), Part 2.

## Layout

| Path | Purpose |
| --- | --- |
| `src/sap_cf_exporter/` | The exporter (Python 3.12, FastAPI, httpx) |
| `deploy/cf/manifest.push.yml` | CF manifest. Replace every `<placeholder>` before pushing |
| `deploy/cf/run-exporter.sh` | Start script: reads the bound secrets service, then starts the exporter |
| `requirements.txt`, `runtime.txt` | Pinned dependencies and Python version for the Python buildpack |

`runtime.txt` must name a Python version your landscape's Python buildpack still offers. If staging
fails with `no match found for python-x.y.z`, run `cf buildpacks` and pick an available 3.12.x.

## Settings (environment variables)

| Variable | Meaning |
| --- | --- |
| `METRICS_BACKEND` | `log_cache` (used here) or `stats_api` |
| `CF_API_URL` | `https://api.cf.<region>.hana.ondemand.com`. Log Cache's URL is derived from it (`log-cache.cf.<region>...`); set `LOG_CACHE_URL` only if yours differs |
| `CF_ORG_NAMES`, `CF_SPACE_NAMES`, `CF_APP_NAMES` | Which apps to read. Empty = everything the user can see |
| `CF_AUTH_MODE` | `password` (technical user; the exporter fetches and refreshes its own tokens; recommended), `client_credentials`, or `static_token` (demo only: a `cf oauth-token` token lasts about 20 minutes) |
| `OTLP_PUSH_ENDPOINT` | `https://<collector-route>/v1/metrics` |
| `DEPLOYMENT_ENVIRONMENT` | Sent as `deployment.environment.name` |
| `POLL_INTERVAL_SECONDS` | Default 60 |

Secrets (`cf_username`/`cf_password` or `cf_bearer_token`, and `otlp_push_token`) come from the
user-provided service `sap-cf-exporter-secrets`, never from files or the manifest.

## What is sent

Gauges named `sap_cf_container_*` per app instance. Resource attributes: `service.name` (the CF app
name) and `deployment.environment.name`. Data point attribute: `cf_instance_index` (plus
`cf_process_type` for non-web processes). Kept small because Splunk Observability Cloud limits the
number of new dimension key names per org per week.

Status: verified on an SAP BTP trial (`us10-001`) with a short-lived (~20 min) token, so it only reported until that token expired. Not yet tested on other
landscapes, nor with `password` mode against a live landscape.
