# Instrumenting Node.js apps on SAP BTP Cloud Foundry via service binding

Audience: a Cloud Foundry space developer. No platform admin rights are needed
at any step.

Outcome: traces, application logs and Secure Application vulnerability reports
from a Node.js app, with **no OpenTelemetry dependency added to the app**, no
change to `package.json`, and no start command rewrite. Credentials live in one
shared service instance rather than in each app's environment.

Everything below was executed end to end on a BTP trial on 12 Nov 2026 against
Splunk OTel JS Cloud Foundry buildpack **v4.13.0**. Figures quoted are from that
run.

---

## What is in this folder

Two files go into **your** application; the rest are reference material.

| File | Purpose |
|---|---|
| `.profile` | **Copy to your app root.** Cloud Foundry sources it at launch and evaluates the exports produced by the next file. |
| `splunk-binding.js` | **Copy to your app root.** Reads `VCAP_SERVICES` and maps the bound instance onto the variables the agent reads. |
| `manifest.yml` | A complete working manifest. Merge its `buildpacks`, `env` and `services` blocks into your own. |
| `check-splunk-binding.py` | Reports whether an existing instance has everything a Node.js app needs. Prints key names and lengths only, never a value. |
| `server.js`, `package.json` | A small reference app, described below. |

The folder is also directly pushable as a sample: change `name` and
`OTEL_SERVICE_NAME` in `manifest.yml`, then `cf push` from here.

> **The sample app pins known-vulnerable dependencies on purpose.**
> `lodash 4.17.15` and `minimist 1.2.0` carry published advisories, and both are
> actually `require`d at startup, because Secure Application defaults to
> `SPLUNK_SECUREAPP_RUNTIME_PACKAGES_ONLY=true` and reports only packages that
> were really loaded. Without a genuine finding there is nothing to see in the
> Secure Application UI. Expect your dependency scanner to flag this folder.
> **Do not copy these pins into a real application** — take `.profile`,
> `splunk-binding.js` and the manifest blocks, and leave the dependencies alone.

---

## 1. Before you start

| Requirement | Why | How to check |
|---|---|---|
| Space developer on the target space | Everything here is space-scoped | `cf target` |
| `cflinuxfs5` available | The buildpack requires it; `cflinuxfs4` is DEPRECATED | `cf stacks` |
| Diego cells can reach `github.com` | The buildpack is referenced by release URL and fetched at staging | Try one push; see §7 if blocked |
| A collector reachable from the app | Secure Application reports and logs go through it, not direct to ingest | `curl` its `/v1/logs` from anywhere |
| The collector has the Secure Application routing connector | Without it, vulnerability reports land in your log index instead of the Secure Application UI | See §6 |
| An Observability Cloud access token with ingest scope | Traces | Your Splunk admin |

The buildpack is a **supply** buildpack. It does not replace the Node.js
buildpack; it runs before it.

---

## 2. The shared service instance — reuse one if it exists

**Skip this section if your space already has a `splunk-o11y` instance.** Run
`cf services` first. A user-provided service is nothing more than a named bag of
JSON credentials; Cloud Foundry does not type it by language, so an instance
created for your Java apps serves Node.js apps unchanged. In testing, the
instance was created for a Java application and later bound to a Node.js
application with no edits at all.

An instance carrying only the Java-style dotted keys (`splunk.realm`,
`splunk.access.token`, `otel.exporter.otlp.logs.*`) is read correctly by the
shim in §3 — this is tested. You do not need to add the generic keys below to an
existing instance.

### Check an existing instance before you bind anything

```sh
python3 check-splunk-binding.py            # or: python3 check-splunk-binding.py <instance-name>
```

It lists the bound applications, reports every credential key with its value
**length only** — never a value, so the output is safe to paste into a ticket —
and states which of the five keys a Node.js app needs are present. It also flags
the two mistakes that cost us time on this engagement: a logs header missing the
`%20`, and an access token long enough to suggest the collector token was pasted
in by mistake.

Expect an instance built for Java to be missing the two logs keys. Traces will
work without them; Secure Application reports and application logs will not
leave the application, because both travel over the logs pipeline to your
collector. Add them with `cf update-user-provided-service`, then `cf restage`
the bound apps.

Service instances are space-scoped, so each space needs its own unless your
operator has enabled service instance sharing.

If your Java applications are already instrumented by the Java buildpack's
binding detection, that instance is the one to bind — the buildpack requires a
bound service whose name contains `splunk-o11y` in order to add `-javaagent` at
staging, so it exists by definition. Bind it to the Node.js applications as
well; it needs no changes.

### The alternative, for completeness

Setting the same variables directly on each application with `cf set-env` works
identically and needs none of §3. It is the better choice for one or two
applications. Binding is what makes a token rotation a single
`cf update-user-provided-service` rather than an edit to every application, and
is the right model once you have more than a handful.

### Creating one, if you have none

One instance per space. Every instrumented app in the space binds to it, so a
token rotation is a single update rather than an edit to every app.

```sh
cf create-user-provided-service splunk-o11y -p '{
  "realm": "us1",
  "access_token": "<OBSERVABILITY_CLOUD_INGEST_TOKEN>",
  "otel.exporter.otlp.protocol": "http/protobuf",
  "otel.exporter.otlp.logs.endpoint": "https://<your-collector-host>/v1/logs",
  "otel.exporter.otlp.logs.headers": "Authorization=Bearer%20<COLLECTOR_INGEST_TOKEN>"
}'
```

Two different tokens appear here and they are easy to swap by mistake:

- `access_token` is the **Observability Cloud** ingest token (22 characters in
  our org). It authenticates traces to Splunk.
- The token inside `otel.exporter.otlp.logs.headers` is your **collector's**
  ingest token (64 characters in our setup). It authenticates the app to the
  collector.

Getting these the wrong way round produces a clean `401` from the collector and
nothing else — no error in the app log. We lost time to exactly this.

`%20` is required: it is the URL-encoded space in `Bearer <token>`. The OTel SDK
URL-decodes header values, so a literal space breaks parsing.

The same instance also serves **Java** apps. The Java buildpack reads
`VCAP_SERVICES` natively and understands the dotted keys, so add these if you
are creating a new instance that both languages will share. Java additionally
needs the traces endpoint stated explicitly or every span returns HTTP 404
while metrics succeed; Node.js derives it from the realm and does not need it.

```json
  "splunk.realm": "us1",
  "splunk.access.token": "<OBSERVABILITY_CLOUD_INGEST_TOKEN>",
  "otel.exporter.otlp.traces.endpoint": "https://ingest.us1.observability.splunkcloud.com/v2/trace/otlp"
```

To rotate a token later:

```sh
cf update-user-provided-service splunk-o11y -p '{ ... }'
cf restage <app>     # restart is not enough for a credential change to be re-read
```

---

## 3. Add two small files to the app

**Only needed if you are using a service binding.** If you set the variables
directly with `cf set-env`, skip to §4 — there is nothing to add to the app.

The Node.js buildpack **does not read `VCAP_SERVICES`**. This is the one place
where it differs materially from the Java buildpack: there is no reference to
`VCAP_SERVICES` anywhere in the buildpack scripts or the bundled agent. These
two files do that translation at launch. Cloud Foundry runs `.profile`
automatically, after the buildpack's own scripts and before your start command.

`.profile` — in the application root:

```sh
#!/bin/sh
if [ -n "${VCAP_SERVICES:-}" ] && [ -f "${HOME:-/home/vcap/app}/splunk-binding.js" ]; then
  eval "$(node "${HOME:-/home/vcap/app}/splunk-binding.js")"
fi
```

`splunk-binding.js` — in the application root. Copy it from this folder. It
reads the bound instance, maps the credentials onto the variables the agent
expects, and writes `export` lines to stdout for `.profile` to evaluate.

**How it finds the instance.** It matches any bound service whose name
*contains* `splunk-o11y`, which is the same rule the SAP Java buildpack uses, so
`platform-splunk-o11y-prod` is found as readily as `splunk-o11y`. If your instance is
named differently again, set `SPLUNK_BINDING_NAME` to a distinguishing substring.
Exact-match would miss a site-specific name, and that failure mode is no
telemetry and no error message.

**Two mappings worth knowing about**, both driven by what real Java-built
instances actually contain:

- **Protocol.** An instance built for Java usually carries the *per-signal*
  `otel.exporter.otlp.logs.protocol` rather than the global
  `otel.exporter.otlp.protocol`. The shim accepts either and emits the matching
  variable. Reading only the global key leaves the exporter on its default
  protocol.
- **Resource attributes are merged, not replaced.** The binding typically
  carries estate-wide values such as `deployment.environment`, while the app
  sets its own `service.version` and `service.namespace`. Treating this like
  the other variables would silently drop one set or the other — the app would
  report with no environment, or the whole estate under one namespace. On a key
  collision the app's value wins, consistent with the rule below. A bare
  `service.namespace` credential key is honoured the same way.

**Windows: `.profile` must keep Unix (LF) line endings.** It runs on a Linux
container. Saved with Windows CRLF endings it either fails to parse or quietly
does nothing, and there is no error in `cf logs` either way — it looks exactly
like a binding that was never read. `.gitattributes` in this repo pins it to LF,
so a normal `git clone` is safe. The risk is copy-pasting into Notepad. If you
do that, use an editor that can save Unix endings (VS Code shows `CRLF`/`LF` in
the status bar and lets you click to switch), and check it landed:

```powershell
# Prints True if the file still has Windows endings, which is wrong
[IO.File]::ReadAllText(".profile") -match "`r`n"
```

Three properties of that file matter for review:

- **Nothing is echoed.** The output is consumed by `eval`, so no credential
  reaches the Cloud Foundry log stream.
- **Values are shell-quoted.** A credential containing `'` or `;` is passed
  through literally and cannot be executed. This is tested.
- **It never blocks startup.** Any parse failure or missing binding exits 0
  silently. A telemetry shim must not be able to take the app down.
- **An explicitly set variable wins.** Per-app overrides work without removing
  anything from the shared binding.

---

## 4. Manifest

```yaml
applications:
  - name: <your-app>
    stack: cflinuxfs5

    # Order matters. The Splunk buildpack supplies; the Node.js buildpack
    # finalizes. Listing it last fails the build with an explanatory message.
    buildpacks:
      - https://github.com/signalfx/splunk-otel-js/releases/download/v4.13.0/splunk-otel-js-cloudfoundry-4.13.0.zip
      - nodejs_buildpack

    services:
      - splunk-o11y

    env:
      OTEL_SERVICE_NAME: <your-service-name>
      OTEL_RESOURCE_ATTRIBUTES: deployment.environment=<env>,service.namespace=<namespace>,service.version=<version>

      # Secure Application. The buildpack bundles the agent but does NOT enable
      # it -- see §5.
      SPLUNK_SECUREAPP_AGENT_ENABLED: "true"

      # Application logs. A SEPARATE switch from the above -- see §5.
      SPLUNK_AUTOMATIC_LOG_COLLECTION: "true"

      # Must be false. true overwrites NODE_OPTIONS and silently removes the
      # agent preload.
      OPTIMIZE_MEMORY: "false"
```

Do **not** add a `command:` override. The buildpack installs a `profile.d`
script that prepends `--require=".../splunk-otel/instrument.js"` to
`NODE_OPTIONS`, which leaves your existing `npm start` untouched. This is what
avoids the `MODULE_NOT_FOUND` failures that hand-written start commands produce.

Then:

```sh
cf push -f manifest.yml
```

---

## 5. Four behaviours that will cost you time

These are not configuration mistakes. They are how the agent behaves, and each
one looks like something it is not.

### A realm without an access token stops the application booting

This is the only failure in this runbook that takes the application down rather
than just losing telemetry. If `SPLUNK_REALM` is set and `SPLUNK_ACCESS_TOKEN`
is not, the agent throws during the preload:

```
Error: Splunk realm is set, but access token is unset. To send traces to the
Observability Cloud, both need to be set
```

The throw happens before your code runs, so the app crash-loops. `splunk-binding.js`
withholds `SPLUNK_REALM` unless a token is also available, which turns this back
into the correct failure for a telemetry shim: no telemetry, running application.
That guard only matters if the instance carries the realm under a recognised key
and the token under one this file does not know, so keep the two mappings in step
if you ever extend them.

### The buildpack does not enable Secure Application

It ships `@splunk/secureapp-agent` inside the buildpack, but nothing in the
buildpack sets the flag, and the agent defaults it to `false`. If you were told
the buildpack turns Secure Application on, it does not —
`SPLUNK_SECUREAPP_AGENT_ENABLED: "true"` is yours to set.

### Secure Application and application logs are separate switches

Enabling Secure Application force-enables the logs *pipeline*, which is why the
vulnerability reports flow. But the bunyan / pino / winston bridge is gated on
`SPLUNK_AUTOMATIC_LOG_COLLECTION`, which Secure Application does not affect.
With only the Secure Application flag set, you get vulnerability reports and
**zero application logs**, through a pipeline that is demonstrably working.

Also: `console.log` is never collected, in any configuration. The agent
instruments logging libraries, not the console. Applications logging via
`console.log` will see nothing in Splunk and must move to a supported library.

### Successful trace exports print an error stack

Every successful trace export logs a stack trace ending in:

```
Error: Unknown wire type 3
    at deserializeExportTraceServiceResponse (...)
```

This is **not** a failure. Splunk's trace ingest replies with JSON while the SDK
tries to read it as protobuf, after the spans have already been accepted. The
proof is the buffer at the end of the stack:

```sh
cf logs <app> --recent | grep -o "<Buffer [0-9a-f ]*>"
```

Decoded, `7b 22 76 61 6c 69 64 22 3a 34 7d` is `{"valid":4}` — four spans
accepted. Our run totalled 292 spans accepted, zero rejected, with this stack
printed throughout.

Do not suppress these. They are currently the only visible evidence of trace
export in either direction.

---

## 6. Collector requirement for Secure Application

Secure Application reports travel as OTLP **log records** with instrumentation
scope `secureapp`. They must be split out and sent to the security event API;
if they go down your ordinary log pipeline they land in your log index as
unreadable gzip+base64 blobs and never reach the Secure Application UI.

The collector needs a routing connector and a dedicated exporter. This repo
ships exactly that as `../config.secureapp.yaml`; enable it on the collector by
adding the file to `OTEL_EXTRA_CONFIG` and running `cf push`, as described in
section 9a of `../RUNBOOK.md`. The essentials:

```yaml
connectors:
  routing/logs:
    default_pipelines: [logs/app]
    error_mode: ignore
    table:
      - context: log
        condition: instrumentation_scope.name == "secureapp"
        pipelines: [logs/secureapp]

exporters:
  otlp_http/secureapp:
    logs_endpoint: https://ingest.<realm>.signalfx.com/v3/event
    headers:
      X-SF-Token: <token>
      # Without this header the records are accepted and silently ignored.
      X-Splunk-Instrumentation-Library: secureapp
```

Give the secureapp branch its **own** debug exporter rather than sharing one
with the default branch. If both branches print through an exporter with the
same name, the output is identical whichever way a record went and a routing
mistake is invisible. We initially could not tell which branch records took for
exactly this reason.

---

## 7. If the Diego cells cannot reach github.com

Some environments block egress from the staging container. Two options, in
order of preference:

1. **Ask your CF operator to register the buildpack once**
   (`cf create-buildpack`). Apps then reference it by name. Requires admin.
2. **Host the ZIP inside the foundation.** Download the release ZIP, push it as
   a staticfile app in a space you control, and reference that app's URL in
   `buildpacks:` instead of the GitHub one. Verified working on BTP.

---

## 8. Verifying it worked

Run these in order. Each produces positive evidence rather than absence of
error.

**Was the agent injected?** Add a route handler that returns
`process.env.NODE_OPTIONS`, or check the staging log for
`Installing Splunk OpenTelemetry for Node.js 4.13.0`. In the running container
you should see:

```
--require="/home/vcap/deps/0/splunk-otel/instrument.js"
```

**Set the diagnostic logger first, or you will see nothing.** The agent
registers no logger unless asked, so Secure Application messages and every
export failure are silent:

```sh
cf set-env <app> OTEL_LOG_LEVEL info
cf restart <app>
```

This is worth doing for the first deployment and removing afterwards.

**Is Secure Application running?**

```sh
cf logs <app> --recent | grep "packages identified"
# secureapp 157 packages identified
```

Note it reports only packages actually loaded at runtime
(`SPLUNK_SECUREAPP_RUNTIME_PACKAGES_ONLY` defaults to true). A declared but
unused vulnerable dependency will not appear. The first scan waits 60 seconds
by default; `SPLUNK_SECUREAPP_DEPENDENCY_INITIAL_DELAY` shortens it for testing.

**Are reports reaching the collector?** On the collector:

```sh
cf logs <collector> --recent | grep '"POST /v1/logs HTTP/1.1"'
```

`200` is success. `401` means a token mismatch — almost always the two tokens
from §2 swapped. `404` usually means the collector app is stopped: the CF router
returns 404 for a stopped app, which looks exactly like a wrong path.

**Are traces in APM?** Confirm the realm matches the org you are looking at —
spans sent to `us1` will never appear in a `us0` org no matter how long you
wait — and filter APM to the `deployment.environment` value from
`OTEL_RESOURCE_ATTRIBUTES`.

---

## 9. Node.js applications already instrumented in package.json

This is a common starting point, and for these applications **you do not need
the buildpack at all**.

An app that already carries `@splunk/otel` in `package.json` and initialises it
itself is already running the agent. What it is missing is configuration. Bind
the service instance, add the two files from §3, set the feature flags from §4,
and restage — the in-app agent reads exactly the same environment variables:

```sh
cf bind-service <app> <your-splunk-o11y-instance>
cf set-env <app> SPLUNK_SECUREAPP_AGENT_ENABLED true
cf set-env <app> SPLUNK_AUTOMATIC_LOG_COLLECTION true
cf restage <app>
```

Skip §4's `buildpacks:` block entirely. Leave the app's existing buildpack
configuration alone.

Two things to confirm first:

- **The bundled agent version.** Secure Application needs a recent
  `@splunk/otel`. Check with `cf logs <app> --recent | grep telemetry.distro`,
  or read `telemetry.distro.version` off a live trace. The customer app we
  inspected reports 4.12.1, which carries the Secure Application agent.
- **A supported logging library.** Application logs only flow from bunyan, pino
  or winston. An app logging through `console.log` will produce vulnerability
  reports but no logs, whatever you set.

**Do not add the buildpack on top of in-app instrumentation.** It would load the
SDK twice, and we have not tested that. If you want the buildpack on such an
app, remove `@splunk/otel` from `package.json` and the initialisation from the
source first — which is the change this buildpack exists to avoid needing.

---

## 10. Turning it off

| Goal | Action |
|---|---|
| Disable instrumentation, keep the buildpack | `cf set-env <app> SPLUNK_OTEL_NODEJS_ENABLED false` then restage |
| Disable Secure Application only | `cf unset-env <app> SPLUNK_SECUREAPP_AGENT_ENABLED` then restage |
| Remove entirely | Drop the Splunk buildpack from `buildpacks:`, `cf unbind-service <app> splunk-o11y`, delete `.profile` and `splunk-binding.js`, then push |

Always **restage**, not restart, after a buildpack or credential change. A
restart reuses the existing droplet and will appear to ignore your change.

---

## 11. Security notes

- `cf env <app>` and `cf curl /v3/service_instances/<guid>/credentials` both
  reveal bound credentials to anyone with space access. Binding centralises
  rotation; it does not hide the token from space developers.
- Prefer one token per space with ingest scope only, so a leak is contained and
  revocable without affecting other spaces.
- CI systems that run `cf env` will print these values into build logs.
- The collector token and the Observability Cloud token have different blast
  radii. Do not reuse one for both.
