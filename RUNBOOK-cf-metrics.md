# Runbook: add Cloud Foundry container metrics to the running `otel-log-collector`

Adds a `metrics` pipeline (receiver `cloud_foundry` -> Splunk Observability Cloud) to the collector
that already forwards logs to Splunk Cloud over HEC. The logs pipeline is not modified.

Status: the config validates (`otelcol-contrib validate`). It has NOT yet been run against a real
UAA client. First live run = the verification in step 5.

## Before you start

| Need | Notes |
| --- | --- |
| UAA client with authority `logs.admin`, grant type `client_credentials` | From the customer / SAP. Client ID + secret. |
| RLP gateway URL | `https://log-stream.cf.<region>.hana.ondemand.com` for the landscape |
| UAA URL | `https://uaa.cf.<region>.hana.ondemand.com` |
| Splunk Observability Cloud realm + org access token | Token needs ingest scope. |
| Collector app name | Same name used for the logs deployment |

Never paste the client secret or token into chat, tickets or files. Use hidden prompts as below.

## 1. Get the file

Download `config.cf-metrics.yaml` into the same folder as `config.yaml` and `manifest.yml`
(`btp-otel-poc/logs/collector-app/`). It is the only new file.

## 2. Set the credentials (hidden input)

bash:
```bash
app=<app_name>
cf set-env $app CF_RLP_GATEWAY_ENDPOINT 'https://log-stream.cf.<region>.hana.ondemand.com'
cf set-env $app CF_UAA_ENDPOINT         'https://uaa.cf.<region>.hana.ondemand.com'
cf set-env $app SPLUNK_O11Y_REALM       '<realm, e.g. us1>'
read -rsp 'UAA client id: '     cid;  echo; cf set-env $app CF_UAA_CLIENT_ID     "$cid"
read -rsp 'UAA client secret: ' csec; echo; cf set-env $app CF_UAA_CLIENT_SECRET "$csec"
read -rsp 'O11y access token: ' tok;  echo; cf set-env $app SPLUNK_O11Y_ACCESS_TOKEN "$tok"
unset cid csec tok
```

PowerShell:
```powershell
$app = '<app_name>'
cf set-env $app CF_RLP_GATEWAY_ENDPOINT 'https://log-stream.cf.<region>.hana.ondemand.com'
cf set-env $app CF_UAA_ENDPOINT         'https://uaa.cf.<region>.hana.ondemand.com'
cf set-env $app SPLUNK_O11Y_REALM       '<realm, e.g. us1>'
foreach ($n in 'CF_UAA_CLIENT_ID','CF_UAA_CLIENT_SECRET','SPLUNK_O11Y_ACCESS_TOKEN') {
  $s = Read-Host $n -AsSecureString
  cf set-env $app $n ([Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)))
}
```

Optional: `cf set-env $app CF_RLP_SHARD_ID '<unique-id>'` (default `btp-otel-metrics`).

## 3. Switch the overlay on

In `manifest.yml`, change the start command from

```
command: chmod +x ./otelcol-contrib && exec ./otelcol-contrib --config=config.yaml
```
to
```
command: chmod +x ./otelcol-contrib && exec ./otelcol-contrib --config=config.yaml --config=config.cf-metrics.yaml
```

## 4. Push

```bash
cf push -f manifest.yml --vars-file vars-<target>.yml --no-start
cf start $app
```

`--no-start` on a running app stops it; start it again. Env vars set in step 2 survive the push.
Logs are interrupted only for the restart (about a minute).

## 5. Verify (do both)

1. Startup: `cf logs $app --recent` must show `Everything is ready` and no `Asynchronous error received`.
   Logs forwarding must still work (send a test log, confirm in Splunk Cloud).
2. Metrics in Splunk Observability Cloud: metric finder, search `cpu`, `memory`, `disk`; filter by
   `deployment.environment.name`. First datapoints appear within 1-2 minutes.

## Failure messages

| In `cf logs` | Cause | Fix |
| --- | --- | --- |
| `failed to fetch initial token from UAA ... 401 Unauthorized` | Wrong client id/secret, or client not `client_credentials` | Re-run step 2, `cf restart $app` |
| `... 403` / stream error after login | Client lacks `logs.admin` | Ask SAP/customer to fix the client authorities |
| `connection refused` / `no such host` | Wrong `CF_UAA_ENDPOINT` / `CF_RLP_GATEWAY_ENDPOINT` | Fix the URL for this landscape |
| Receiver healthy, nothing in Splunk Observability Cloud | Wrong realm/token, or org new-dimension-key limit | `Exporting failed` in logs: fix realm/token. Otherwise check `sf.org.numPropertyLimitedMetricTimeSeriesCreateCalls` |
| App OOM / restarting | 512 MB too small for the metric volume | Raise `memory:` in `manifest.yml` |

A login failure stops the whole collector, including log forwarding. Roll back immediately.

## Roll back (restores logs-only)

1. In `manifest.yml` set the command back to `--config=config.yaml` only.
2. `cf push -f manifest.yml --vars-file vars-<target>.yml --no-start && cf start $app`

Optionally remove the credentials: `cf unset-env $app CF_UAA_CLIENT_SECRET` (and the other five).

## Notes

- Instances: 2 is fine. Instances share `shard_id` and split the stream (no duplicates).
- Any other collector reading the same landscape needs a different `shard_id`.
- Splunk Observability Cloud limits new dimension key names per org per week (default 40). The
  receiver's labels may hit it; metrics then arrive with labels missing. Tell me and I will add a
  trim.
