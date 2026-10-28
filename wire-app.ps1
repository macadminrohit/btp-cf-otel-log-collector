# Point Java apps' OTLP logs at the collector by MERGING four keys into an existing
# user-provided service's credentials. Existing keys (including the Observability
# access token) are preserved and never printed.
#
#   .\wire-app.ps1 -Ups splunk-o11y -CollectorUrl https://<route> [-Apps app1,app2] [-Disable]
#
#   -Ups           the service the buildpack reads, e.g. splunk-o11y
#   -CollectorUrl  https://<route>   (no path)
#   -Apps          after updating, rolling-restage these apps
#   -Disable       rollback: otel.logs.exporter=none and drop the other keys
#
# Ingest token: taken from $env:OTLP_INGEST_TOKEN if set, otherwise read from the
# collector app named by -CollectorApp (default otel-log-collector) in the
# currently targeted space.
param(
    [Parameter(Mandatory = $true)][string]$Ups,
    [Parameter(Mandatory = $true)][string]$CollectorUrl,
    [string[]]$Apps = @(),
    [switch]$Disable,
    [string]$CollectorApp = 'otel-log-collector'
)
$ErrorActionPreference = 'Stop'

function Get-UpsCredentials([string]$Guid) {
    $raw = (& cf curl "/v3/service_instances/$Guid/credentials") | Out-String
    if ($LASTEXITCODE -ne 0) { throw 'cf curl failed' }
    $raw | ConvertFrom-Json
}
function Set-Prop($Obj, [string]$Name, [string]$Value) {
    $p = $Obj.PSObject.Properties[$Name]
    if ($p) { $p.Value = $Value }
    else { Add-Member -InputObject $Obj -NotePropertyName $Name -NotePropertyValue $Value }
}

& cf target | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Not logged in. Run: cf login -a <api> --sso' }
$guid = ((& cf service $Ups --guid) | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrEmpty($guid)) { throw "Service '$Ups' not found in the targeted space." }

$token = $env:OTLP_INGEST_TOKEN
if (-not $Disable -and [string]::IsNullOrEmpty($token)) {
    $token = (& cf env $CollectorApp) | Select-String -Pattern '^OTLP_INGEST_TOKEN:\s*(\S+)' |
        ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -First 1
}
if (-not $Disable -and [string]::IsNullOrEmpty($token)) { throw 'No ingest token found.' }

$creds = Get-UpsCredentials $guid
$dropKeys = 'otel.exporter.otlp.logs.protocol', 'otel.exporter.otlp.logs.endpoint', 'otel.exporter.otlp.logs.headers'

if ($Disable) {
    Set-Prop $creds 'otel.logs.exporter' 'none'
    foreach ($k in $dropKeys) { [void]$creds.PSObject.Properties.Remove($k) }
} else {
    Set-Prop $creds 'otel.logs.exporter' 'otlp'
    Set-Prop $creds 'otel.exporter.otlp.logs.protocol' 'http/protobuf'
    Set-Prop $creds 'otel.exporter.otlp.logs.endpoint' ($CollectorUrl.TrimEnd('/') + '/v1/logs')
    # %20, not a space: the buildpack passes this through a JVM argument line.
    Set-Prop $creds 'otel.exporter.otlp.logs.headers' ('Authorization=Bearer%20' + $token)
}
$token = $null

# Temp file in the user's own temp folder; deleted in the finally block.
$tmp = [IO.Path]::GetTempFileName()
try {
    $json = $creds | ConvertTo-Json -Depth 10 -Compress
    [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding($false)))   # UTF-8 without BOM
    $json = $null
    & cf update-user-provided-service $Ups -p $tmp | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'cf update-user-provided-service failed' }
} finally {
    Remove-Item -Force -ErrorAction SilentlyContinue $tmp
}

Write-Host "Updated '$Ups'. Keys now present:"
(Get-UpsCredentials $guid).PSObject.Properties.Name | Sort-Object | ForEach-Object { Write-Host "   $_" }

if ($Apps.Count -gt 0) {
    foreach ($a in $Apps) {
        Write-Host "Rolling restage: $a"
        & cf restage $a --strategy rolling
        if ($LASTEXITCODE -ne 0) { throw "restage of $a failed" }
    }
} else {
    Write-Host 'Not restarted. Credentials are read at staging: cf restage <app> --strategy rolling'
}
