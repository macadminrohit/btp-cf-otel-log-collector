<#
.SYNOPSIS
  Create or update the `sap-cf-exporter-secrets` user-provided service without ever printing a secret.
  No Python needed. Works in Windows PowerShell 5.1 and PowerShell 7. Needs the `cf` CLI, already logged in.

.EXAMPLE
  .\set-exporter-secrets.ps1                      # SSO user: uses the refresh token of your `cf login --sso`
  .\set-exporter-secrets.ps1 -Mode password       # technical user: prompts for username and password
  .\set-exporter-secrets.ps1 -DryRun              # check inputs and list the keys, change nothing

.NOTES
  The collector's OTLP ingest token (the OTLP_INGEST_TOKEN env var of the otel-log-collector app) is read
  directly from `cf env` and never shown. Use -CollectorApp <name> if your collector app is named
  differently, or -PromptForToken to type/paste it (hidden prompt) instead.
  Secrets go through a temporary file in your own temp folder that is deleted straight away.
  Nothing is echoed, logged or passed on a command line.
#>
[CmdletBinding()]
param(
    [ValidateSet("refresh_token", "password")]
    [string]$Mode = "refresh_token",
    # App whose OTLP_INGEST_TOKEN the exporter must present. Read straight from `cf env`, so nothing is
    # typed or pasted (a mistyped or truncated token causes "OTLP push rejected: HTTP 401").
    [string]$CollectorApp = "otel-log-collector",
    [switch]$PromptForToken,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Service = "sap-cf-exporter-secrets"

function Read-Secret([string]$Prompt) {
    $secure = Read-Host $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

# Run `cf` and return its exit code. Windows PowerShell 5.1 turns any stderr output of a native command
# into a terminating error when $ErrorActionPreference is "Stop" (NativeCommandError), and a failing
# `update` is expected on first use. So relax it just for the call and judge by the exit code.
function Invoke-Cf {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & cf @args 2>&1 | Out-Null
        return $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $previous }
}

function Get-CollectorToken([string]$App) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $lines = & cf env $App 2>&1 | ForEach-Object { "$_" } }
    finally { $ErrorActionPreference = $previous }
    $hit = $lines | Select-String -Pattern '^OTLP_INGEST_TOKEN:\s*(\S+)' | Select-Object -First 1
    if ($hit) { return $hit.Matches[0].Groups[1].Value }
    return $null
}

$secrets = [ordered]@{}

if ($Mode -eq "refresh_token") {
    $base = if ($env:CF_HOME) { $env:CF_HOME } else { $HOME }
    $configPath = Join-Path $base ".cf\config.json"
    if (-not (Test-Path $configPath)) {
        throw "No cf config at $configPath. Run 'cf login --sso' first, then retry."
    }
    $refresh = (Get-Content -Raw -Path $configPath | ConvertFrom-Json).RefreshToken
    if (-not $refresh) {
        throw "No refresh token in $configPath. Run 'cf login --sso' first, then retry."
    }
    $secrets["cf_refresh_token"] = [string]$refresh
}
else {
    $secrets["cf_username"] = (Read-Host "CF technical user name").Trim()
    $secrets["cf_password"] = Read-Secret "CF technical user password"
}
$ingest = $null
if (-not $PromptForToken) {
    $ingest = Get-CollectorToken $CollectorApp
    if ($ingest) {
        Write-Host "Using OTLP_INGEST_TOKEN read from app '$CollectorApp' ($($ingest.Length) characters)."
    }
    else {
        Write-Host "Could not read OTLP_INGEST_TOKEN from app '$CollectorApp'; asking for it instead."
    }
}
if (-not $ingest) { $ingest = (Read-Secret "Collector OTLP_INGEST_TOKEN").Trim() }
$secrets["otlp_push_token"] = $ingest

$empty = @($secrets.Keys | Where-Object { -not $secrets[$_] })
if ($empty.Count -gt 0) { throw "Missing value for: $($empty -join ', ')" }

$keys = ($secrets.Keys | Sort-Object) -join ", "
if ($DryRun) {
    Write-Host "Dry run. Would store keys in ${Service}: $keys"
    return
}

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("cf-secrets-" + [guid]::NewGuid().ToString("N") + ".json")
try {
    # UTF-8 without BOM (Set-Content -Encoding UTF8 would add one in Windows PowerShell 5.1).
    $json = $secrets | ConvertTo-Json -Compress
    [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding($false)))

    if ((Invoke-Cf update-user-provided-service $Service -p $tmp) -ne 0) {
        if ((Invoke-Cf create-user-provided-service $Service -p $tmp) -ne 0) {
            throw "cf could not create or update the service. Are you logged in ('cf target')?"
        }
    }
}
finally {
    if (Test-Path $tmp) { Remove-Item -Force $tmp }
}

Write-Host "Stored keys in ${Service}: $keys"
Write-Host "Set CF_AUTH_MODE: $Mode in exporter/deploy/cf/manifest.push.yml, then push."
