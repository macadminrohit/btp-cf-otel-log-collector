// Translates a bound `splunk-o11y` user-provided service into the environment
// variables the Splunk OTel JS agent reads. Invoked by .profile at launch; the
// output is a list of `export` lines that .profile evals.
//
// Writes only to stdout, which .profile captures, so credentials never reach
// the Cloud Foundry log stream. Any failure is silent by design: a telemetry
// shim must never stop the application from starting.
//
// One binding serves both languages. The Java buildpack consumes the dotted
// keys (splunk.access.token, otel.exporter.otlp.traces.endpoint, ...) directly
// from VCAP_SERVICES; this file reads the same instance and maps whichever
// naming it finds.

// Matches the SAP Java buildpack convention: any bound service whose NAME
// CONTAINS this string, so `platform-splunk-o11y-prod` is found as readily as
// `splunk-o11y`. Exact-match would silently miss a site-specific name, and the
// failure mode is no telemetry with no error. Override with
// SPLUNK_BINDING_NAME when the instance is named differently again.
const INSTANCE_MATCH = process.env.SPLUNK_BINDING_NAME || 'splunk-o11y'

function findInstance(services) {
  return Object.values(services)
    .flat()
    .find(
      (s) =>
        s &&
        ((s.name || '').includes(INSTANCE_MATCH) ||
          (s.tags || []).some((t) => String(t).includes(INSTANCE_MATCH)))
    )
}

// Single-quote for POSIX sh: wrap in single quotes and replace any embedded
// single quote with '\'' so a credential containing shell metacharacters
// cannot be interpreted by the eval in .profile.
function shellQuote(value) {
  return `'${String(value).replace(/'/g, "'\\''")}'`
}

try {
  const instance = findInstance(JSON.parse(process.env.VCAP_SERVICES || '{}'))
  if (!instance) process.exit(0)

  const c = instance.credentials || {}
  const accessToken = c['access_token'] ?? c['splunk.access.token']
  const mapping = {
    SPLUNK_REALM: c['realm'] ?? c['splunk.realm'],
    SPLUNK_ACCESS_TOKEN: accessToken,
    OTEL_EXPORTER_OTLP_LOGS_ENDPOINT: c['otel.exporter.otlp.logs.endpoint'],
    OTEL_EXPORTER_OTLP_LOGS_HEADERS: c['otel.exporter.otlp.logs.headers'],
    OTEL_LOGS_EXPORTER: c['otel.logs.exporter'],
    // Instances built for Java commonly carry the per-signal logs protocol
    // rather than the global one, so accept either and emit the matching
    // variable. Missing this leaves the exporter on its default protocol.
    OTEL_EXPORTER_OTLP_PROTOCOL: c['otel.exporter.otlp.protocol'],
    OTEL_EXPORTER_OTLP_LOGS_PROTOCOL: c['otel.exporter.otlp.logs.protocol'],
  }

  // The agent throws 'Splunk realm is set, but access token is unset' and the
  // app never boots. Emitting the realm alone would therefore turn a binding
  // whose token sits under an unrecognised key into a crash loop, so withhold
  // it unless a token is actually available. No telemetry is the correct
  // failure for a shim; a dead application is not.
  if (!accessToken && !process.env.SPLUNK_ACCESS_TOKEN) delete mapping.SPLUNK_REALM

  for (const [key, value] of Object.entries(mapping)) {
    // An explicitly set app variable wins, so a per-app override does not have
    // to be removed from the shared binding.
    if (value === undefined || value === null || value === '') continue
    if (process.env[key]) continue
    process.stdout.write(`export ${key}=${shellQuote(value)}\n`)
  }

  // Resource attributes are MERGED rather than overwritten. The binding
  // typically carries estate-wide values such as deployment.environment, while
  // the app sets its own service.version and service.namespace. Treating this
  // like the variables above would silently drop one set or the other -- the
  // app would report with no environment, or the whole estate with one
  // namespace. On a key collision the app's value wins, consistent with the
  // rule above.
  const attributes = new Map()
  for (const source of [c['otel.resource.attributes'], process.env.OTEL_RESOURCE_ATTRIBUTES]) {
    for (const pair of String(source || '').split(',')) {
      const at = pair.indexOf('=')
      if (at < 1) continue
      attributes.set(pair.slice(0, at).trim(), pair.slice(at + 1).trim())
    }
  }
  // A bare service.namespace key is a convention on some instances; the app's
  // own OTEL_RESOURCE_ATTRIBUTES still takes precedence over it.
  if (c['service.namespace'] && !attributes.has('service.namespace')) {
    attributes.set('service.namespace', String(c['service.namespace']))
  }
  if (attributes.size) {
    const merged = [...attributes].map(([k, v]) => `${k}=${v}`).join(',')
    process.stdout.write(`export OTEL_RESOURCE_ATTRIBUTES=${shellQuote(merged)}\n`)
  }
} catch {
  process.exit(0)
}
