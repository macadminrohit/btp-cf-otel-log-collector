# Set the collector's configuration on the CURRENTLY TARGETED Cloud Foundry space.
# Prompts for everything; nothing is written to disk or echoed.
#
#   .\set-secrets.ps1 [-App otel-log-collector] [-Rotate] [-WithO11y]
#
# -WithO11y also sets the Observability Cloud realm and access token, which the
# config.o11y-metrics.yaml and config.secureapp.yaml overlays require.
#
# After changing anything: cf restart <app>   (restage is not required)
#
# Adding an overlay FILE is different: a file cannot be added to an existing
# droplet, so that needs cf push. Setting OTEL_EXTRA_CONFIG to a file the
# droplet does not contain puts the collector into a crash loop.
param(
    [string]$App = 'otel-log-collector',
    [switch]$Rotate,
    [switch]$WithO11y
)
$ErrorActionPreference = 'Stop'

function Invoke-Cf {
    # Run cf, hide its output, fail on non-zero exit.
    & cf @args | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "cf $($args[0]) failed (exit $LASTEXITCODE)" }
}
function Read-Secret([string]$Prompt) {
    $s = Read-Host -Prompt $Prompt -AsSecureString
    $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
    try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
}
function Set-AppEnv([string]$Name, [string]$Value) {
    Invoke-Cf set-env $App $Name $Value
    Write-Host "  set $Name"
}

if (-not (Get-Command cf -ErrorAction SilentlyContinue)) { throw 'cf CLI not found on PATH' }
Invoke-Cf target
Invoke-Cf app $App

$hecUrl = Read-Host 'Splunk HEC URL (https://<host>/services/collector)'
if ($hecUrl -notmatch '^https://.+/services/collector$') { throw 'URL must be https and end in /services/collector' }
$hecIndex = Read-Host 'Splunk index (must exist and be allowed on the HEC token)'
$hecToken = Read-Secret 'Splunk HEC token (input hidden)'
if ([string]::IsNullOrEmpty($hecToken)) { throw 'empty token' }

Set-AppEnv SPLUNK_HEC_URL $hecUrl
Set-AppEnv SPLUNK_HEC_INDEX $hecIndex
Set-AppEnv SPLUNK_HEC_TOKEN $hecToken
$hecToken = $null

# Customer data may be in log bodies. Keep the debug exporter quiet by default.
Set-AppEnv DEBUG_VERBOSITY basic

if ($WithO11y) {
    # Required by config.o11y-metrics.yaml (container metrics) and by
    # config.secureapp.yaml (Secure Application vulnerability reports).
    $realm = Read-Host 'Splunk Observability Cloud realm (for example us0)'
    if ($realm -notmatch '^[a-z]{2}[0-9]+$') { throw 'realm looks wrong; expected something like us0' }
    $o11yToken = Read-Secret 'Observability Cloud access token, ingest scope (input hidden)'
    if ([string]::IsNullOrEmpty($o11yToken)) { throw 'empty token' }

    # Distinct from the collector's own OTLP_INGEST_TOKEN below. Pasting one in
    # place of the other produces a clean 401 and no other symptom.
    Set-AppEnv SPLUNK_O11Y_REALM $realm
    Set-AppEnv SPLUNK_O11Y_ACCESS_TOKEN $o11yToken
    $o11yToken = $null
}

$existing = (& cf env $App) | Select-String -Pattern '^OTLP_INGEST_TOKEN:\s*(\S+)' |
    ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -First 1

if ([string]::IsNullOrEmpty($existing) -or $Rotate) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $new = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    Set-AppEnv OTLP_INGEST_TOKEN $new
    $new = $null
    Write-Host '  (new OTLP ingest token generated - re-run wire-app.ps1 for every app)'
} else {
    Write-Host '  OTLP_INGEST_TOKEN unchanged'
}
Write-Host "Done. Apply with: cf restart $App"
