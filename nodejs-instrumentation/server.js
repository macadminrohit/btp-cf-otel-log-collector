// Test app for the Splunk OTel JS Cloud Foundry buildpack.
//
// The point of this app is what is MISSING from package.json: there is no
// @splunk/otel dependency and no `-r @splunk/otel/instrument` in the start
// script. The buildpack supplies the agent and injects it through NODE_OPTIONS
// from a profile.d script, so if spans appear, they came from outside the app.
//
// That also removes the failure we hit when instrumenting by hand, where a
// start command with a hardcoded entry file produced MODULE_NOT_FOUND.
//
// lodash and minimist are pinned to versions with published advisories, and the
// requires below are load-bearing: Secure Application defaults to
// SPLUNK_SECUREAPP_RUNTIME_PACKAGES_ONLY=true and reports only packages that
// were actually loaded.
//
// pino is here for a second reason: the agent's automatic log collection only
// instruments bunyan, pino and winston. The console.log calls below are kept
// deliberately as the contrast -- they reach the Cloud Foundry log stream and
// nothing else, while the pino records below travel to the collector.
const express = require('express')
const _ = require('lodash')
const minimist = require('minimist')
const pino = require('pino')

const log = pino({ name: 'node-buildpack-probe' })
const app = express()

app.get('/health', (_req, res) => {
  log.info('health check')
  res.json({ ok: true })
})

app.get('/', (_req, res) =>
  res.json({
    message: 'node-buildpack-probe',
    instance: process.env.CF_INSTANCE_INDEX ?? 'local',
    // Proves the buildpack injected the preload rather than the app doing it.
    nodeOptions: process.env.NODE_OPTIONS ?? '(none)',
  })
)

app.get('/work', (req, res) => {
  const args = minimist(['--n', String(req.query.n ?? 3)])
  const out = _.chunk(_.range(Number(args.n) * 4), 4)
  log.info({ chunks: out.length }, 'work completed')
  res.json({ chunks: out.length, sample: out[0] ?? [] })
})

// Logs at error level so severity mapping is visible in Splunk, without the
// process actually failing.
app.get('/boom', (_req, res) => {
  log.error({ reason: 'synthetic' }, 'deliberate error for log severity testing')
  res.status(500).json({ error: 'synthetic' })
})

const port = process.env.PORT || 3000
app.listen(port, () => {
  console.log(`node-buildpack-probe listening on ${port}`)
  console.log(`lodash ${require('lodash/package.json').version}, minimist ${require('minimist/package.json').version}`)
})
