# Runbook: SAP BTP Cloud Foundry app logs → Splunk Cloud (HEC), with trace correlation

One shared OpenTelemetry Collector, run as an ordinary Cloud Foundry app. Java apps
export OTLP logs to it; it forwards to Splunk Cloud over HEC. No sidecar, no code
change, no certificate to manage. Proven on the BTP trial (us10-001) on 28 Oct 2026.

```
 Java app (Splunk OTel agent)                 otel-log-collector (CF app)             Splunk Cloud
 logs carry trace_id / span_id  --OTLP/HTTP-->  bearer-token auth, batch   --HEC-->   index, trace_id field
        (binding credentials)    over the CF route                                          |
                                                                                            v
 Splunk Observability Cloud  <---- Log Observer Connect (joined on trace_id) ------------- search
```

Traces and metrics are unaffected: they still go from each app straight to
Observability Cloud. Observability Cloud does not ingest logs.

---

## 0. What you need from the customer BEFORE the change window

Collect these first. Every one has caused a failure at least once.

| # | Item | Why / how to check |
|---|------|--------------------|
| 1 | Splunk Cloud **HEC URL** | `https://http-inputs-<stack>.splunkcloud.com/services/collector` (paid stacks, port 443). Trial and some stacks differ (`inputs.<stack>…:8088`). Test `…/services/collector/health` returns `HEC is healthy`. |
| 2 | A **dedicated HEC token** | **Indexer acknowledgement must be OFF.** With ack on, Splunk answers `400 "Data channel is missing" (code 28)` and the exporter drops everything: it sends no channel header and never polls acks. Verified in the exporter source and live. |
| 3 | A target **index** that exists | Token's "Selected indexes" must include it, else `400 Incorrect index (code 7)`. |
| 4 | Splunk Cloud **HEC IP allow list** | If the stack restricts HEC by source IP, the collector's **egress IP** must be allowed. Get it from the foundation's NAT/egress address. Not self-serve. |
| 5 | CF **space** for the collector + **Space Developer** role | A shared "platform tools" space is typical. |
| 6 | **Quota** | 512 MB memory (observed ~36 MB idle at trial load; 256 MB is likely enough, measure first) and 1 GB disk, per instance. |
| 7 | **Buildpack**: `binary_buildpack` visible in `cf buildpacks` | If customer policy only allows approved buildpacks, get it approved or see "Alternatives". |
| 8 | **Package size limit** | The package is ~112 MB compressed, ~400 MB on disk. Confirm the foundation allows it. |
| 9 | **Application security group (egress)** | The collector space must reach Splunk Cloud on 443: `cf security-groups`. |
| 10 | How each Java app is instrumented | The `splunk-o11y` binding model below depends on the **community `java_buildpack`**'s Splunk OTel framework. If apps use `sap_java_buildpack` or set the agent manually, use the env-var variant in section 6. |
| 11 | **O11y realm** | Confirm per org. Realms differ per org (for example `us0`, `us1`); do not assume. The realm does not affect this log path, but it does affect Log Observer Connect (section 8). |
| 12 | **Data approval** | Application logs leave the foundation for Splunk Cloud. Confirm classification (PII, GxP) and that the log content is cleared for that index. |

---

## 1. Prepare the files (on your laptop, once per target)

All files live in `btp-otel-poc/logs/collector-app/`:

| File | Purpose | Secret? |
|------|---------|---------|
| `config.yaml` | Collector pipeline (OTLP/HTTP in, `splunk_hec` out) | No, env references only |
| `manifest.yml` | CF app definition, parameterised | No |
| `vars.example.yml` → `vars-<target>.yml` | App name and route for the target | No |
| `fetch.sh` / `fetch.ps1` | Downloads pinned `otelcol-contrib`, verifies sha256 | No |
| `set-secrets.sh` / `.ps1` | Prompts for HEC URL/token/index, generates ingest token, sets CF env | Handles secrets, writes none to disk |
| `wire-app.sh` / `.ps1` | Merges OTLP-log keys into an app's binding | Handles secrets; a temp file holds the credentials JSON for the duration of one `cf` call and is deleted |
| `smoke-test.sh` / `.ps1` | Sends a test log through the route, checks auth | No |
| `.cfignore`, `.gitignore` | Keep the binary and `.dl/` out of git and out of the push | No |

```bash
cd "<repo>/btp-otel-poc/logs/collector-app"
cp vars.example.yml vars-customer-dev.yml
# edit: app_name, route (must be unique on the customer's shared domain)
./fetch.sh          # needs outbound GitHub access from the laptop
```

### Windows desktop (PowerShell)

Every `.sh` script has a `.ps1` equivalent. They run on Windows PowerShell 5.1 (built in)
or PowerShell 7, need only the `cf` CLI (v8+) and `tar` (built in since Windows 10 1803),
and use no Python, bash or curl. **The collector binary pushed is still the Linux one**
(`linux_amd64`): that is what Cloud Foundry runs, whatever the laptop is.

| Linux / macOS | Windows PowerShell |
|---|---|
| `./fetch.sh` | `.\fetch.ps1` |
| `./set-secrets.sh <app> [--rotate]` | `.\set-secrets.ps1 -App <app> [-Rotate]` |
| `./wire-app.sh <ups> <url> --apps "a b"` | `.\wire-app.ps1 -Ups <ups> -CollectorUrl <url> -Apps a,b` |
| `./wire-app.sh <ups> <url> --disable` | `.\wire-app.ps1 -Ups <ups> -CollectorUrl <url> -Disable` |
| `./smoke-test.sh <url> <app>` | `.\smoke-test.ps1 -Url <url> -App <app>` |
| `cp vars.example.yml vars-x.yml` | `Copy-Item vars.example.yml vars-x.yml` |

All other commands in this runbook (`cf push`, `cf start`, `cf logs`, …) are identical.
Replace `grep` with `Select-String`, e.g. `cf env <app> | Select-String SPLUNK_HEC`.

Windows-specific things that will bite:

- **Execution policy / "not digitally signed".** If the zip came from email or a browser, run
  `Get-ChildItem .\*.ps1 | Unblock-File` first. If policy still blocks scripts:
  `powershell -ExecutionPolicy Bypass -File .\fetch.ps1` (per process, no system change).
  If AppLocker or the security team forbids that, use the manual commands below.
- **Antivirus / EDR may quarantine the 400 MB Go binary** after `fetch.ps1` extracts it
  (it is unsigned and large). The sha256 is verified against the project's published value
  before extraction. Ask security to allow the folder, or fetch on an unmanaged host and copy.
- **GitHub must be reachable** from the laptop for `fetch.ps1`. If it is blocked, download
  `otelcol-contrib_0.164.0_linux_amd64.tar.gz` and its `.sha256` in a browser from
  `github.com/open-telemetry/opentelemetry-collector-releases/releases/tag/v0.164.0`, put both in `.dl\`,
  and re-run `.\fetch.ps1` (it skips the download and still verifies the hash).
- **File permissions.** A push from Windows does not carry Unix mode bits. The manifest start
  command therefore runs `chmod +x` before starting the binary. If the app crashes with
  "permission denied" anyway, that line is the first thing to check.
- **Corporate proxy.** `cf` honours `HTTPS_PROXY`; `fetch.ps1` uses the system proxy settings.
- **Windows scripts have not been run on Windows by the author.** They were written against
  the documented behaviour of PowerShell 5.1 and the cf CLI and were not executed. Run the smoke
  test (section 5) before trusting them, and tell me what breaks.

**Manual fallback, no scripts at all** (works anywhere `cf` works):

```powershell
cf push -f manifest.yml --vars-file vars-customer-dev.yml --no-start

$app = 'otel-log-collector'
cf set-env $app SPLUNK_HEC_URL   'https://http-inputs-<stack>.splunkcloud.com/services/collector'
cf set-env $app SPLUNK_HEC_INDEX '<index>'
cf set-env $app DEBUG_VERBOSITY 'basic'
$s = Read-Host 'HEC token' -AsSecureString
cf set-env $app SPLUNK_HEC_TOKEN ([Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)))
# ingest token: any long random value, e.g. 64 hex characters
$b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
cf set-env $app OTLP_INGEST_TOKEN (-join ($b | % { $_.ToString('x2') }))
cf start $app
```

Wiring an app by hand (instead of `wire-app.ps1`) means editing the service credentials
JSON, which **replaces all of it**; do not do that for a binding that holds the
Observability access token unless you re-enter every existing key. Prefer `wire-app.ps1`,
or use the per-app environment variables in section 6, variant B.

`fetch.sh` pins version `0.164.0`. Bump `OTELCOL_VERSION` deliberately, then re-run
section 3 (validate) and 5 (smoke test).

---

## 2. Log in to the customer foundation

```bash
cf login -a <customer-cf-api> --sso
cf target -o <org> -s <platform-tools-space>
cf buildpacks | grep binary_buildpack     # item 7
cf security-groups                         # item 9
```

---

## 3. (Optional, recommended) Validate the config locally

**Linux/macOS only. Windows: skip this step** (the binary fetched is for Linux; smoke-test in
section 5 is the real check, or use WSL). `fetch.sh` leaves a native binary under `.dl/`:

```bash
PORT=8080 OTLP_INGEST_TOKEN=x SPLUNK_HEC_URL=https://x.invalid/services/collector SPLUNK_HEC_TOKEN=x \
  .dl/native/otelcol-contrib validate --config=config.yaml
```

Caveat learned the hard way: in 0.164.0 **`validate` does not reject unknown keys** in the
`splunk_hec` block. A misspelt key is silently ignored and the defaults apply. After
any config edit, test with a real run, not `validate` alone.

---

## 4. Deploy the collector

```bash
cf push -f manifest.yml --vars-file vars-customer-dev.yml --no-start
./set-secrets.sh <app_name>        # prompts; token input is hidden
cf start <app_name>
cf app <app_name>                  # expect: instances 1/1 running
```

Notes:
- `cf push --no-start` on an app that is already running stops it. Restart it.
- Set `instances: 2` in the manifest for production so one cell restart does not stop log export.
- `DEBUG_VERBOSITY` is set to `basic` by `set-secrets.sh`. **Never leave it at `detailed` on a customer foundation**: it writes every log body into the collector's own log stream.
- After changing any env var: `cf restart <app_name>` (restage not needed).

---

## 5. Smoke test, before touching any application

```bash
./smoke-test.sh https://<route> <app_name>
```

Expect `401`, `401`, `200`. Then in Splunk Cloud search, over the last 15 minutes
(use the trace_id the script prints):

```
index=<index> trace_id=<printed id>
| table _time host source sourcetype service.name trace_id span_id _raw
```

You should get one event with `trace_id` and `span_id` as their own fields.
If it is missing, read `cf logs <app_name> --recent` for `Exporting failed`:

| Collector log says | Cause | Fix |
|--------------------|-------|-----|
| `400 Bad Request` | Usually ack enabled (`code 28`) or bad index (`code 7`). Confirm with a manual `curl` to HEC; see section 9. | Item 2 / item 3 |
| `401` / `403` from HEC | Token wrong, disabled, or HEC disabled | Re-run `set-secrets.sh` |
| connection timeout / refused | Egress blocked, or HEC IP allow list | Items 9 / 4 |
| nothing logged, event absent | Wrong index name or search window | Search `index=*` narrowly by trace_id |

---

## 6. Wire the first application (dev, one Java app)

This changes an application: **use the customer's change process** and start with a dev or test app.

Check the app's current setup first:

```bash
cf services                      # find the user-provided service the buildpack reads
cf env <app> | grep -i otel      # do not paste this output anywhere: it may contain tokens
```

**Variant A: apps using the `splunk-o11y` binding (community `java_buildpack`)**

```bash
./wire-app.sh <ups-name> https://<route> --apps "<app>"
```

This merges four keys into the service's credentials, keeping all existing ones
(including the Observability access token, which is never printed), then does a
**rolling restage** so there is no downtime:

```
otel.logs.exporter                = otlp          (was: none)
otel.exporter.otlp.logs.protocol  = http/protobuf
otel.exporter.otlp.logs.endpoint  = https://<route>/v1/logs
otel.exporter.otlp.logs.headers   = Authorization=Bearer%20<ingest token>
```

Two details that cost time on the trial: the header must use `%20` and not a space
(the buildpack puts it on a JVM argument line), and `otel.logs.exporter=none` must be
**replaced** with `otlp`. Changing it back is the rollback (`--disable`).

> **Environment comes from the app, not the collector.** The collector never invents an
> environment. The Java agent sends `deployment.environment` (from the binding's
> `otel.resource.attributes`); the collector copies that value into
> `deployment.environment.name` so both fields in Splunk Cloud equal the environment shown
> in Splunk APM. An app that sends no environment gets none. To change an app's environment,
> change its own `otel.resource.attributes`, not the collector.

> **Order matters.** Deploy and smoke-test the collector *first*. If an app exports
> OTLP logs to a collector that is not there, it floods its own log with
> "Failed to export logs … Connection refused", exactly the problem `otel.logs.exporter=none` was
> set to avoid.

**Variant B: apps instrumented some other way (env vars)**

```bash
cf set-env <app> OTEL_LOGS_EXPORTER otlp
cf set-env <app> OTEL_EXPORTER_OTLP_LOGS_PROTOCOL http/protobuf
cf set-env <app> OTEL_EXPORTER_OTLP_LOGS_ENDPOINT https://<route>/v1/logs
# token: cf set-env <app> OTEL_EXPORTER_OTLP_LOGS_HEADERS "Authorization=Bearer%20<token>"
#        (run it yourself; do not put the token in a file or a ticket)
cf restage <app> --strategy rolling
```

**Node apps are different.** This path does not give them trace correlation: neither the
Splunk Node distro nor `@cap-js/telemetry` puts trace context into logs. Each app must
read `trace.getActiveSpan()?.spanContext()` and attach `trace_id`/`span_id` itself.

---

## 7. Prove correlation for that app

1. Generate traffic against the app (any request that logs).
2. In the collector log you may see nothing at `basic`; use Splunk instead:
   ```
   index=<index> service.name=<app> | head 20
   ```
3. Pick one event, copy its `trace_id`, and open the same trace in Splunk APM
   (`<o11y-ui>/#/apm/traces/<trace_id>`) or:
   ```bash
   cup o11y apm trace-segments <trace_id>
   ```
   `cup o11y apm trace <id>` returned "segment not found" in testing; use the APM UI for span detail.
4. Capture both for the deliverable: the Splunk event (showing `trace_id`, `span_id`) and the APM trace.

What the event looks like in Splunk (HEC fields, indexed):

```json
{"event":"posted invoice INV-LOGS-3 for supplier null in system S4H",
 "fields":{"deployment.environment.name":"btp-trial","service.name":"s4-adapter",
           "otel.log.severity.text":"INFO","trace_id":"10d032e1…1b6f","span_id":"8039661f…0647"},
 "host":"…","source":"btp:cf:otlp","sourcetype":"btp:cf:app","index":"…"}
```

Note the body is the plain message text. The ECS JSON stays on stdout (still in `cf logs`).

---

## 8. Log Observer Connect (Observability Cloud side)

This is the only part that is **not self-serve** and **may be licensed**:

1. An Observability Cloud **admin** configures a **Log Observer Connect** connection from
   the O11y org to the customer's Splunk Cloud stack (Settings → Log Observer Connect,
   per Splunk documentation).
2. A Splunk Cloud **admin** must create/approve a service account/role for it with search
   access to the index above, and the customer's Splunk Cloud stack must meet the
   connection prerequisites (version, region, network/allow-list for the O11y realm).
3. Whether the entitlement is included depends on the customer's contract. **Ask the account team.**
4. Match the connection's realm (us0/us1/…) to the O11y org where the APM data lives.

I have not performed these steps on the lab tenant, and the exact menu names and
prerequisites must be checked against current Splunk docs before the meeting.
Until it is set up, correlation still works by hand: copy `trace_id` from APM and search
Splunk Cloud directly, as in section 7.

---

## 9. Troubleshooting commands

```bash
cf logs <app_name> --recent                  # collector's own log (look for "Exporting failed")
cf env <app_name> | grep -c SPLUNK_HEC       # expect 3, values not shown here on purpose
# Manual HEC test (run in YOUR terminal; the token never goes in a file):
read -rs "T?HEC token: "
curl -s -w ' [%{http_code}]\n' "<HEC_URL>" -H "Authorization: Splunk $T" \
  -d '{"event":"hec test","index":"<index>","sourcetype":"btp:cf:app"}'
unset T
```

PowerShell equivalent:

```powershell
$s = Read-Host 'HEC token' -AsSecureString
$t = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
try {
  (Invoke-WebRequest -UseBasicParsing -Method Post -Uri '<HEC_URL>' -Headers @{Authorization="Splunk $t"} `
    -Body '{"event":"hec test","index":"<index>","sourcetype":"btp:cf:app"}').Content
} catch { $_.ErrorDetails.Message }   # Splunk's JSON reply is in the error body on a 400
$t = $null
```

| Splunk replies | Meaning |
|----------------|---------|
| `Success` code 0 | Token, index OK |
| `Data channel is missing` code 28 | Indexer ack is on: use a token with it off |
| `Incorrect index` code 7 | Index missing or not allowed on token |
| `Invalid token` code 4 | Wrong or disabled token |

---

## 10. Rollback

Per app: `./wire-app.sh <ups-name> https://<route> --disable --apps "<app>"` sets
`otel.logs.exporter=none` again. Collector: `cf stop <app_name>`; remove with `cf delete`.
Nothing else was changed in the app.

## 11. Rotate the ingest token

```bash
./set-secrets.sh <app_name> --rotate    # re-enter HEC values; new ingest token generated
cf restart <app_name>
./wire-app.sh <ups-name> https://<route> --apps "<app1> <app2>"   # every wired app
```

Apps keep failing auth (logs dropped, nothing else breaks) until re-wired.

---

## Known limits and what to say plainly

- **The collector's route is public.** BTP disables container-to-container networking, so apps reach it through the gorouter. The bearer token is the only control. It cannot be IP-restricted at the route, and the token sits in each app's binding. Treat it as write-only ingest, rotate it, and say so.
- **Queue is in memory.** If the collector is down, the agent drops logs after a short buffer. Stdout (`cf logs`) is unaffected.
- **No platform logs.** Only what each app's agent exports. Router, staging and platform events need the Firehose Nozzle (`doppler.firehose`, platform admin) or the contrib `cloud_foundry` receiver (UAA client with `logs.admin`). Both need platform admin and are the **target state** for the estate.
- **The agent's log pipeline adds a little heap and CPU to each JVM.** Not measured; measure before claiming it is negligible.
- **Per-app wiring.** This is one binding change per app (or per shared binding). It scales to many apps but is not zero-touch like a nozzle.
- **Not demonstrable on a BTP trial:** the Firehose Nozzle, `cloud_foundry` receiver, Log Observer Connect (licensed, admin), IP allow-listing of the foundation's egress, and a multi-instance production-sized collector.
- **Binary size.** `otelcol-contrib` is ~400 MB because it contains every component. For production, build a custom collector with `ocb` containing only `otlp`, `splunk_hec`, `memory_limiter`, `batch`, `resource`, `bearertokenauth` and `debug`. It is smaller, faster to stage, and has a smaller attack surface. Not done yet.

## Alternatives

- **Syslog drains** (`syslog-tls://`) need no app change but need a TCP route and a CA-trusted certificate for a hostname the customer owns. The earlier `../otel-collector.yaml` was a first draft of that path; it is untested and has known errors (see its header).
- **Splunk Firehose Nozzle**: platform-level, zero per-app wiring. Preferred target state if the customer can grant `doppler.firehose`.
- **HEC appender inside the app** (`splunk-library-javalogging`): not on Maven Central; per-app token, dependency and egress. Last choice.
