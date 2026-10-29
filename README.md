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

## What `wire-app` does

`wire-app.ps1` (Windows) and `wire-app.sh` (macOS/Linux) switch on log export for a Java
app **without changing the app or its code**. They only edit one Cloud Foundry
user-provided service and restage the app.

**How it works.** The Java apps are instrumented by the Cloud Foundry Java buildpack's
Splunk OpenTelemetry framework. That framework reads the credentials of a bound
user-provided service (for example `splunk-o11y`) and passes every key that starts with
`otel.` or `splunk.` to the JVM as a system property. Adding keys to that service
therefore reconfigures the Splunk Java agent at the next staging.

**Steps, in order:**

1. Checks you are logged in to Cloud Foundry (`cf target`) and that the named service
   exists in the targeted space.
2. Gets the collector's ingest token from `$env:OTLP_INGEST_TOKEN` if set, otherwise from
   the collector app's environment (`cf env otel-log-collector`). The token is held in
   memory only and is never printed.
3. Reads the service's current credentials.
4. **Merges** four keys into them and leaves every other key untouched (including the
   Observability Cloud access token and realm):

   | Key | Value | Purpose |
   |-----|-------|---------|
   | `otel.logs.exporter` | `otlp` | Turns on log export (it was `none`/unset) |
   | `otel.exporter.otlp.logs.protocol` | `http/protobuf` | Wire format the collector accepts |
   | `otel.exporter.otlp.logs.endpoint` | `<collector URL>/v1/logs` | Where the agent sends logs |
   | `otel.exporter.otlp.logs.headers` | `Authorization=Bearer%20<token>` | Authenticates to the collector (`%20`, not a space, because the buildpack passes it on a JVM argument line) |
5. Writes the merged credentials to a temporary file, runs
   `cf update-user-provided-service <name> -p <file>`, and deletes the file. The update
   replaces all credentials, which is why step 4 merges instead of overwriting.
6. Lists the key **names** now in the service (never the values).
7. For each app named in `-Apps` / `--apps`, runs `cf restage <app> --strategy rolling`
   so the new settings take effect with no downtime. Without `-Apps` it only reminds
   you to restage.

**What it does not do.**

- It does not change application code, the app's manifest, or any route.
- It does not touch the collector, Splunk Cloud, or Observability Cloud settings.
- It does not restage apps you did not name. If several apps are bound to the same
  service, each picks up the change at its next restage, so wire one app first.
- It does not create the service or the HEC token.

**What the app then does.** The Splunk Java agent captures the app's Logback/Log4j2 log
records, attaches the active trace context (`trace_id`, `span_id`) to each one, and sends
them over HTTPS to the collector, which forwards them to Splunk Cloud over HEC. Apps that
log with `System.out` are not captured.

**Safety.** No secret is written to disk beyond the short-lived temp file in your own
temp folder. No secret is printed. The change is reversible: run the same command with
`-Disable` (`--disable`) to set `otel.logs.exporter=none` and remove the other three keys,
then restage.

**Usage.**

```powershell
# Windows PowerShell. Keep it on ONE line (a backtick only continues a line if it is the last character).
.\wire-app.ps1 -Ups <service> -CollectorUrl https://<collector-route> -Apps <java-app>

# Roll back
.\wire-app.ps1 -Ups <service> -CollectorUrl https://<collector-route> -Apps <java-app> -Disable
```

`-CollectorUrl` is the collector's route **without** `/v1/logs`. Use `-CollectorApp <name>`
if the collector app is not called `otel-log-collector`. If PowerShell warns that the
script came from the internet, run `Get-ChildItem *.ps1 | Unblock-File` once.

## Status

Worked example, not a supported product. Proven on an SAP BTP trial Cloud Foundry
space with a Java Spring Boot app. The PowerShell scripts target Windows PowerShell
5.1 / 7 and have been used for the deploy, smoke-test and wire-app steps on a real
Cloud Foundry foundation. Review everything, and run the smoke test before relying on it.

No credentials belong in this repository. HEC tokens and the ingest token are set with
`cf set-env` and are never written to files. `.gitignore` excludes the collector binary.
