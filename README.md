# btp-cf-otel-log-collector

Ship application logs from SAP BTP Cloud Foundry to Splunk Cloud (HEC), carrying
`trace_id` and `span_id` as searchable fields so they join to Splunk APM traces.

One shared OpenTelemetry Collector runs as an ordinary Cloud Foundry app. Java apps
instrumented with the Splunk OpenTelemetry Java agent export OTLP logs to it over HTTPS
with a bearer token; it forwards to Splunk Cloud over HEC. No sidecar, no application
code change, no certificate to manage.

**Start with [RUNBOOK.md](RUNBOOK.md).**

```
Java app (Splunk OTel agent) --OTLP/HTTP + bearer--> otel-log-collector (CF app) --HEC--> Splunk Cloud
```

| File | Purpose |
|------|---------|
| `RUNBOOK.md` | Prerequisites, deploy, test, wire an app, rollback, troubleshooting |
| `config.yaml` | Collector pipeline. Secrets come from environment variables only |
| `manifest.yml`, `vars.example.yml` | Cloud Foundry app definition and per-target variables |
| `fetch.sh` / `fetch.ps1` | Download the pinned collector release and verify its sha256 |
| `set-secrets.sh` / `.ps1` | Prompt for HEC settings and set them on the app |
| `wire-app.sh` / `.ps1` | Add the OTLP log settings to an app's service binding |
| `smoke-test.sh` / `.ps1` | Send a test log and check authentication |

## Status

Worked example, not a supported product. Proven on an SAP BTP trial Cloud Foundry
space with a Java Spring Boot app. The PowerShell scripts were written for Windows
PowerShell 5.1 / 7 but have not been run on Windows by the author. Review everything,
and run the smoke test before relying on it.

No credentials belong in this repository. HEC tokens and the ingest token are set with
`cf set-env` and are never written to files. `.gitignore` excludes the collector binary.
