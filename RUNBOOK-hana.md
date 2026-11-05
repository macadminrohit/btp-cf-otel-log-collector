# Runbook: one HANA Cloud database through `otel-log-collector`

The collector you already run is `otelcol-contrib` 0.164.0. That binary includes
the SQL Query receiver and the SAP HANA `hdb` driver. This runbook points that
app at **one** HANA Cloud database in your own subaccount. It does not create
the database.

Logs keep going to Splunk Cloud. HANA metrics go to Splunk Observability Cloud
on the same exporter as the Cloud Foundry container metrics.

## What you need first

One SAP HANA Cloud instance whose SQL endpoint this Cloud Foundry space can
reach on port 443. Copy from the instance overview:

| Env var | Where it comes from |
|---|---|
| `HANA_HOST` | SQL endpoint hostname only. No `https://`, no `:443` |
| `HANA_DATABASE` | Database name on that instance |
| `HANA_USER` | A database user that can read the system views. `DBADMIN` is only acceptable on a personal trial, not on a J&J database |
| `HANA_PASSWORD` | That user's password |

Also the Observability Cloud values the metrics overlay already uses, if they
are not set: `SPLUNK_O11Y_REALM`, `SPLUNK_O11Y_ACCESS_TOKEN`.

The app must stay at **one instance**. The receiver has no leader election.

## Deploy

`vars-customer-dev.yml` is the local vars file for this space. It supplies
`app_name` and `route` to the manifest and is not in this repo. `cf` should
already be targeted at that org and space. Fetch the collector binary first if
it is not next to the manifest: `./fetch.sh` on Linux or macOS, `.\fetch.ps1`
on Windows. `app` below is the `app_name` from `vars-customer-dev.yml`.

The password and the Observability token are typed at a hidden prompt and passed
only to `cf set-env`. They are not written to a file.

### bash

```bash
cf push -f manifest.yml --vars-file vars-customer-dev.yml --no-start

app=<app_name>
cf set-env "$app" OTEL_EXTRA_CONFIG config.hana.yaml
cf set-env "$app" HANA_HOST '<sql-endpoint-hostname>'
cf set-env "$app" HANA_DATABASE '<database-name>'
cf set-env "$app" HANA_USER '<user>'
read -rsp 'HANA password: ' pw; echo
cf set-env "$app" HANA_PASSWORD "$pw"
unset pw

# Skip the realm and token lines if the metrics overlay is already configured.
cf set-env "$app" SPLUNK_O11Y_REALM '<realm>'
read -rsp 'O11y access token: ' tok; echo
cf set-env "$app" SPLUNK_O11Y_ACCESS_TOKEN "$tok"
unset tok

cf start "$app"
```

### PowerShell

`cf` commands are the same. The hidden prompt uses `Read-Host -AsSecureString`,
which works on Windows PowerShell 5.1 and PowerShell 7.

```powershell
cf push -f manifest.yml --vars-file vars-customer-dev.yml --no-start

$app = '<app_name>'
cf set-env $app OTEL_EXTRA_CONFIG config.hana.yaml
cf set-env $app HANA_HOST '<sql-endpoint-hostname>'
cf set-env $app HANA_DATABASE '<database-name>'
cf set-env $app HANA_USER '<user>'
$pw = Read-Host 'HANA password' -AsSecureString
cf set-env $app HANA_PASSWORD ([Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw)))
Remove-Variable pw

# Skip the next three lines if the metrics overlay is already configured.
cf set-env $app SPLUNK_O11Y_REALM '<realm>'
$tok = Read-Host 'O11y access token' -AsSecureString
cf set-env $app SPLUNK_O11Y_ACCESS_TOKEN ([Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($tok)))
Remove-Variable tok

cf start $app
```

`config.hana.yaml` replaces `config.o11y-metrics.yaml` as the single extra
config. It still forwards OTLP metrics, and adds the HANA pipeline.

Roll back to the metrics overlay without HANA. Logs and container metrics keep flowing.

```bash
cf set-env "$app" OTEL_EXTRA_CONFIG config.o11y-metrics.yaml
cf restart "$app"
```

```powershell
cf set-env $app OTEL_EXTRA_CONFIG config.o11y-metrics.yaml
cf restart $app
```

Unset `HANA_PASSWORD` as well when you remove HANA collection.

```bash
cf unset-env "$app" HANA_PASSWORD
```

```powershell
cf unset-env $app HANA_PASSWORD
```

## Check

```bash
cf logs "$app" --recent | grep -E "Everything is ready|sql_query|hdb|error"
```

```powershell
cf logs $app --recent | Select-String -Pattern 'Everything is ready|sql_query|hdb|error'
```

A refused login or a security group that blocks port 443 shows up there as a
connection error. An empty `M_DISKS` on HANA Cloud does not fail the other
queries. In Observability Cloud, metric names start with `hana.` and the
service name is `sap-hana`.

The password is only in the app environment (`cf set-env`). It is not in the
repo. `cf env` shows it to anyone who can see this space, same as the HEC token.
