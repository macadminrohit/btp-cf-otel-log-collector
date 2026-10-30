<#
.SYNOPSIS
  Create or update the `sap-cf-exporter-secrets` user-provided service without ever printing a secret.
  No Python needed. Works in Windows PowerShell 5.1 and PowerShell 7. Needs the `cf` CLI, already logged in.

.EXAMPLE
  .\set-exporter-secrets.ps1                      # SSO user: uses the refresh token of your `cf login --sso`
  .\set-exporter-secrets.ps1 -Mode password       # technical user: prompts for username and password
  .\set-exporter-secrets.ps1 -DryRun              # check inputs and list the keys, change nothing

.NOTES
  Both modes prompt (hidden) for the collector's OTLP ingest token, i.e. the value of the
  OTLP_INGEST_TOKEN env var of the otel-log-collector app.
  Secrets go through a temporary file in your own temp folder that is deleted straight away.
  Nothing is echoed, logged or passed on a command line.
#>
[CmdletBinding()]
param(
    [ValidateSet("refresh_token", "password")]
    [string]$Mode = "refresh_token",
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
$secrets["otlp_push_token"] = Read-Secret "Collector OTLP_INGEST_TOKEN"

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
