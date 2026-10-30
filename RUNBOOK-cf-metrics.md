# Runbook: Cloud Foundry container metrics through the existing `otel-log-collector`

Goal: CPU, memory, disk, uptime and log rate of every app instance, in Splunk Observability Cloud,
**without any special platform permission**. SAP does not give customers the UAA client that the
Collector's `cloud_foundry` receiver needs, so this route does not use it.

```
 Log Cache  <--read (user login)--  sap-cf-metrics-exporter  --OTLP/HTTP + bearer-->  otel-log-collector
 (CF platform)                      (small CF app, no Collector inside)                 --> Splunk Observability Cloud
                                                                                         (logs keep going to Splunk Cloud, HEC)
```

Status: verified end to end on the SAP BTP trial (Oct 29, 2026). The exporter read Log Cache with an
ordinary user login, pushed to `otel-log-collector`, and the metrics for 3 apps appeared in Splunk
Observability Cloud under their CF app names. Not yet tested on the customer landscape (`us10`), nor
in `password` mode. The first trial run used a copied `cf oauth-token` token (about 20 minutes, so it
reported only briefly); it now runs with `CF_AUTH_MODE=refresh_token` (SSO), which renews itself.

## Before you start

| Need | Notes |
| --- | --- |
| A CF user that can read the apps (Space Developer or Auditor) | Same access `cf app` / `cf logs` already use |
| A way for the exporter to log in again by itself | SSO user: the refresh token from `cf login --sso` (`refresh_token` mode). Production: a technical user (username + password). A copied `cf oauth-token` token lasts only about 20 minutes (demo only) |
| Splunk Observability Cloud realm + org access token (ingest scope) | |
| `otel-log-collector` already running with logs working | Its `OTLP_INGEST_TOKEN` is reused by the exporter |
| The `exporter/` folder of this repo | Runtime files only. Needs the Python buildpack (available by default) |

Never paste tokens or passwords into chat, tickets or files. The commands below read them from
hidden prompts or from the CF service.

## Part 1: turn on the metrics pipeline in the collector

Download `config.o11y-metrics.yaml` next to `config.yaml`, and use the `manifest.yml` from this
folder (its start command accepts an optional overlay). Then:

```bash
app=otel-log-collector
cf push -f manifest.yml --vars-file vars-<target>.yml --no-start
cf set-env $app SPLUNK_O11Y_REALM '<realm, e.g. us1>'
read -rsp 'O11y access token: ' tok; echo; cf set-env $app SPLUNK_O11Y_ACCESS_TOKEN "$tok"; unset tok
cf set-env $app OTEL_EXTRA_CONFIG config.o11y-metrics.yaml
cf start $app
cf logs $app --recent | grep -E "Everything is ready|error"
```

PowerShell: replace the `read` line with
`$s = Read-Host 'O11y token' -AsSecureString; cf set-env $app SPLUNK_O11Y_ACCESS_TOKEN ([Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)))`.

Logs pause only for the restart. **Roll back:** `cf unset-env $app OTEL_EXTRA_CONFIG && cf restart $app`
(logs only, exactly as before).

## Part 2: deploy the exporter

1. Create the secrets service. File `secrets.json` (delete it afterwards) holds:
   `{"cf_bearer_token": "<PoC token>", "otlp_push_token": "<value of the collector's OTLP_INGEST_TOKEN>"}`
   **SSO users (no password):** use `cf_refresh_token` instead of `cf_bearer_token` and set
   `CF_AUTH_MODE: refresh_token`. Run `cf login --sso`, then take the `RefreshToken` value from
   `~/.cf/config.json` (open the file locally; do not paste it into chat or tickets). The exporter trades it
   for a new access token every ~20 minutes. It is your session credential: store it only in this service.
   If the exporter reports "refresh token is expired or revoked", repeat the login and update the service.
   **Production:** a technical user is better: `cf_username` + `cf_password` and `CF_AUTH_MODE: password`.
   ```bash
   cf create-user-provided-service sap-cf-exporter-secrets -p secrets.json
   rm secrets.json
   ```
2. Edit `exporter/deploy/cf/manifest.push.yml` (replace every `<placeholder>`): `CF_API_URL`, `CF_ORG_NAMES`, `CF_SPACE_NAMES`,
   `CF_APP_NAMES`, `OTLP_PUSH_ENDPOINT` (the collector's route + `/v1/metrics`),
   `DEPLOYMENT_ENVIRONMENT`. Log Cache's URL is derived from `CF_API_URL`
   (`api.cf.us10...` becomes `log-cache.cf.us10...`); set `LOG_CACHE_URL` only if yours differs.
3. Push from inside `exporter/` (staging needs about 1 GB of free org memory, runtime 256 MB):
   ```bash
   cd exporter && cf push -f deploy/cf/manifest.push.yml
   ```
   If staging fails with `no match found for python-3.12.x`, run `cf buildpacks`, pick an available
   3.12.x and put it in `exporter/runtime.txt`.

## Verify (both)

1. Exporter: `cf logs sap-cf-metrics-exporter --recent` shows `Cycle ok: N/N processes fetched` and
   no `OTLP push rejected` lines.
2. Splunk Observability Cloud: Metric Finder, search `sap_cf_container_memory_usage_bytes`; filter
   by `service.name` (= CF app name) or `deployment.environment.name`. First data in 1-2 minutes.

Metrics sent (per instance, label `cf_instance_index`): `sap_cf_container_cpu_usage_ratio`,
`..._cpu_entitlement_ratio`, `..._memory_usage_bytes`, `..._memory_limit_bytes`,
`..._disk_usage_bytes`, `..._disk_limit_bytes`, `..._uptime_seconds`,
`..._log_rate_bytes_per_second`, `..._instance_running`, `..._usage_timestamp_seconds`.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `static bearer token has expired` | Demo token (~20 min) | Switch to `refresh_token` (SSO) or `password` mode |
| `refresh token is expired or revoked` | SSO session ended | `cf login --sso`, update `cf_refresh_token` in the service, `cf restart` |
| `OTLP push rejected: HTTP 401` | Wrong `otlp_push_token` | Must equal the collector's `OTLP_INGEST_TOKEN` |
| `OTLP push rejected: HTTP 404/405` | Wrong `OTLP_PUSH_ENDPOINT` | Use `https://<collector route>/v1/metrics` |
| `HTTP 404` from Log Cache | Wrong `LOG_CACHE_URL` (landscape differs) | Set `LOG_CACHE_URL` explicitly |
| `Cycle ok: 0/N` and an app shows no data | App stopped, or no gauges in the last 3 min | Start the app. (Trial: apps are stopped overnight.) |
| Pushes accepted, nothing in Splunk Observability Cloud | Wrong realm/token on the collector | Check `Exporting failed` in the collector logs |
| Some labels missing | Org limit on NEW dimension key names (default 40/week) | Only `service.name`, `deployment.environment.name`, `cf_instance_index` (+`cf_process_type` for non-web) are sent |

## Notes

- All OTLP metrics that other apps send to the collector are forwarded to Splunk Observability
  Cloud as well. Remove `OTEL_EXTRA_CONFIG` if that is not wanted.
- `service.name` = CF app name, so it lines up with APM when the app's own `service.name` is the
  same.
- Polling is every 60 s; Log Cache is read, so the CF API rate limit is hardly touched.
