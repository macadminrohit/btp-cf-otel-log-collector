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
| `otel-log-collector` already running with logs working | Its `OTLP_INGEST_TOKEN` is reused by the exporter. You need that value (it was set with `set-secrets`) |
| Nothing extra on Windows | `exporter/deploy/cf/set-exporter-secrets.ps1` (PowerShell) stores the secrets without showing them. macOS/Linux can use `set-exporter-secrets.py` (needs Python 3) instead. The exporter app itself runs on Cloud Foundry, so Python is never needed on your machine for it |
| Room in the space for one more app | 256 MB at runtime, about 1 GB free org memory while staging |
| Egress from the space to the CF API, UAA, Log Cache and the collector route | Default CF security groups allow this. If your landscape restricts egress, allow those hosts |
| Security sign-off for `refresh_token` mode | The refresh token is the deploying user's session credential. Ask your security team before storing it in a CF service. If they say no, use a technical user (`password` mode) |
| The `exporter/` folder of this repo | Runtime files only. Needs the Python buildpack (available by default) |

Never paste tokens or passwords into chat, tickets or files. The commands below read them from
hidden prompts or from the CF service.

## Part 0: pre-flight check (2 minutes, nothing is deployed)

Prove that your SSO login can read Log Cache. This is exactly what the exporter will do.

```bash
cf login --sso                      # customer API, org and space
LOG_CACHE=https://log-cache.cf.us10.hana.ondemand.com    # same as the CF API host with api. -> log-cache.
GUID=$(cf app <one-of-your-apps> --guid)
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: $(cf oauth-token)" \
  "$LOG_CACHE/api/v1/read/$GUID?envelope_types=GAUGE&limit=1"
```

PowerShell (use `curl.exe`, not `curl`: in Windows PowerShell `curl` is an alias of `Invoke-WebRequest`):

```powershell
cf login --sso
$LogCache = "https://log-cache.cf.us10.hana.ondemand.com"   # CF API host with api. -> log-cache.
$Guid = cf app <one-of-your-apps> --guid
curl.exe -s -o NUL -w "%{http_code}`n" -H "Authorization: $(cf oauth-token)" "$LogCache/api/v1/read/${Guid}?envelope_types=GAUGE&limit=1"
```

The token is sent but not printed.

| Result | Meaning |
| --- | --- |
| `200` | Good. Continue. |
| `401` | Login expired: `cf login --sso` again |
| `403` | Your role cannot read this app: you need Space Developer or Space Auditor |
| `404` / other | Log Cache host differs on this landscape: use it as `LOG_CACHE_URL` in the manifest |

## Part 1: turn on the metrics pipeline in the collector

`git pull` this repo, so `config.o11y-metrics.yaml` sits next to `config.yaml` and `manifest.yml` is the
current one (its start command accepts an optional overlay). Run `./fetch.sh` (or `fetch.ps1`) if the
`otelcol-contrib` binary is not in this folder, and use the same `vars-<target>.yml` as your first install.
The existing `OTLP_INGEST_TOKEN` and HEC settings stay on the app. Then:

Bash:

```bash
app=otel-log-collector
cf push -f manifest.yml --vars-file vars-<target>.yml --no-start
cf set-env $app SPLUNK_O11Y_REALM '<realm, e.g. us1>'
read -rsp 'O11y access token: ' tok; echo; cf set-env $app SPLUNK_O11Y_ACCESS_TOKEN "$tok"; unset tok
cf set-env $app OTEL_EXTRA_CONFIG config.o11y-metrics.yaml
cf start $app
cf logs $app --recent | grep -E "Everything is ready|error"
```

PowerShell:

```powershell
$app = "otel-log-collector"
cf push -f manifest.yml --vars-file vars-<target>.yml --no-start
cf set-env $app SPLUNK_O11Y_REALM "<realm, e.g. us1>"
$s = Read-Host "O11y access token" -AsSecureString
$b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
try { cf set-env $app SPLUNK_O11Y_ACCESS_TOKEN ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($b)) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b); Remove-Variable s, b }
cf set-env $app OTEL_EXTRA_CONFIG config.o11y-metrics.yaml
cf start $app
cf logs $app --recent | Select-String "Everything is ready|error"
```

Logs pause only for the restart. **Roll back** (logs only, exactly as before):
bash `cf unset-env $app OTEL_EXTRA_CONFIG && cf restart $app`,
PowerShell `cf unset-env $app OTEL_EXTRA_CONFIG; cf restart $app`.

## Part 2: deploy the exporter

1. Create the secrets service with the helper (`.ps1` for PowerShell, `.py` for bash). It prompts (hidden) for the collector's `OTLP_INGEST_TOKEN`,
   stores everything in the `sap-cf-exporter-secrets` service through a temporary owner-only file that is
   deleted at once, and prints only the key names. Nothing lands in shell history or on screen.
   ```bash
   cf login --sso        # the same user and target the exporter will read with
   python exporter/deploy/cf/set-exporter-secrets.py                    # SSO user: no password needed
   # production, technical user instead:
   python exporter/deploy/cf/set-exporter-secrets.py --mode password
   ```
   PowerShell (no Python needed). Add `-DryRun` first to check inputs without changing anything:
   ```powershell
   cf login --sso
   .\exporter\deploy\cf\set-exporter-secrets.ps1
   # technical user instead:
   .\exporter\deploy\cf\set-exporter-secrets.ps1 -Mode password
   ```
   If PowerShell says scripts are disabled, run it as
   `powershell -ExecutionPolicy Bypass -File .\exporter\deploy\cf\set-exporter-secrets.ps1` (affects only that run).
   - **SSO (`refresh_token`)**: the script reads the refresh token that `cf login --sso` saved in
     `~/.cf/config.json` (Windows: `%USERPROFILE%\.cf\config.json`). The exporter trades it for a new ~20 min
     access token each time, so it runs unattended. It is your session credential: store it only here.
     If the exporter reports "refresh token is expired or revoked", run `cf login --sso`, run the script again,
     then `cf restart sap-cf-metrics-exporter`.
   - **Technical user (`password`)**: preferred for production. Set `CF_AUTH_MODE: password` in the manifest.
   - Add `--dry-run` (bash/Python) or `-DryRun` (PowerShell) to check inputs and see which keys would be stored, without changing anything.
2. Edit `exporter/deploy/cf/manifest.push.yml` (replace every `<placeholder>`): `CF_API_URL`, `CF_ORG_NAMES`, `CF_SPACE_NAMES`,
   `CF_APP_NAMES`, `OTLP_PUSH_ENDPOINT` (the collector's route + `/v1/metrics`),
   `DEPLOYMENT_ENVIRONMENT`, and `CF_AUTH_MODE` (`refresh_token` for SSO, `password` for a technical user). Log Cache's URL is derived from `CF_API_URL`
   (`api.cf.us10...` becomes `log-cache.cf.us10...`); set `LOG_CACHE_URL` only if yours differs.
3. Push from inside `exporter/` (staging needs about 1 GB of free org memory, runtime 256 MB):
   ```bash
   cd exporter && cf push -f deploy/cf/manifest.push.yml
   ```
   PowerShell: `cd exporter; cf push -f deploy/cf/manifest.push.yml`
   If staging fails with `no match found for python-3.12.x`, run `cf buildpacks`, pick an available
   3.12.x and put it in `exporter/runtime.txt`.

## Verify (both)

1. Exporter: `cf logs sap-cf-metrics-exporter --recent` shows `Cycle ok: N/N processes fetched` and
   no `OTLP push rejected` lines. To filter: bash `| grep -E "Cycle|rejected|ERROR"`,
   PowerShell `| Select-String "Cycle|rejected|ERROR"`.
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
| Timeouts / connection errors to the CF API, UAA or Log Cache | Egress blocked for the space | Allow those hosts in the space's application security group |
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
