# Runbook: one HANA Cloud database through `otel-log-collector`

The collector you already run is `otelcol-contrib` 0.164.0. That binary includes
the SQL Query receiver and the SAP HANA `hdb` driver. This runbook points that
app at **one** HANA Cloud database in your own subaccount. It does not create
the database.

Logs keep going to Splunk Cloud. HANA metrics go to Splunk Observability Cloud
on the same exporter as the Cloud Foundry container metrics.

## What you need first

One SAP HANA Cloud instance whose SQL endpoint this Cloud Foundry space can
reach on port 443. On the Cisco trial that is a HANA Cloud instance in
`f42cd1cctrial`. Copy from the instance overview:

| Env var | Where it comes from |
|---|---|
| `HANA_HOST` | SQL endpoint hostname only. No `https://`, no `:443` |
| `HANA_DATABASE` | Database name on that instance |
| `HANA_USER` | A database user. On your own trial, the instance `DBADMIN` is enough to prove the path. Do not reuse that pattern on a J&J database |
| `HANA_PASSWORD` | That user's password |

Also the Observability Cloud values the metrics overlay already uses, if they
are not set: `SPLUNK_O11Y_REALM`, `SPLUNK_O11Y_ACCESS_TOKEN`.

The app must stay at **one instance**. The receiver has no leader election.

## Deploy

From `logs/collector-app`, after `./fetch.sh` if `otelcol-contrib` is not already
next to the manifest:

```bash
cf target -o f42cd1cctrial -s dev
cf push -f manifest.yml --vars-file vars-trial.yml --no-start

app=otel-log-collector
cf set-env $app OTEL_EXTRA_CONFIG config.hana.yaml
cf set-env $app HANA_HOST '<sql-endpoint-hostname>'
cf set-env $app HANA_DATABASE '<database-name>'
cf set-env $app HANA_USER '<user>'
read -rsp 'HANA password: ' pw; echo
cf set-env $app HANA_PASSWORD "$pw"
unset pw

# Skip the next two if the metrics overlay is already configured.
cf set-env $app SPLUNK_O11Y_REALM 'us1'
read -rsp 'O11y access token: ' tok; echo
cf set-env $app SPLUNK_O11Y_ACCESS_TOKEN "$tok"
unset tok

cf start $app
```

`config.hana.yaml` replaces `config.o11y-metrics.yaml` as the single extra
config. It still forwards OTLP metrics, and adds the HANA pipeline.

Roll back, which leaves logs running and drops HANA collection:

```bash
cf unset-env otel-log-collector OTEL_EXTRA_CONFIG
cf restart otel-log-collector
```

Unset `HANA_PASSWORD` as well when you are finished with the trial:

```bash
cf unset-env otel-log-collector HANA_PASSWORD
```

## Check

```bash
cf logs otel-log-collector --recent | grep -E "Everything is ready|sql_query|hdb|error"
```

A refused login or a security group that blocks port 443 shows up there as a
connection error. An empty `M_DISKS` on HANA Cloud does not fail the other
queries. In Observability Cloud, metric names start with `hana.` and the
service name is `sap-hana`.

The password is only in the app environment (`cf set-env`). It is not in the
repo. `cf env` shows it to anyone who can see this space, same as the HEC token.
