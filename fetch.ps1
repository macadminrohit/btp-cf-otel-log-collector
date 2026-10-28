# Download a pinned otelcol-contrib (linux/amd64: that is what Cloud Foundry runs),
# verify its published sha256, and place the binary next to config.yaml for cf push.
# Windows PowerShell 5.1 or PowerShell 7. Needs `tar` (built in to Windows 10 1803+).
#
#   .\fetch.ps1
#   .\fetch.ps1 -Version 0.164.0
param(
    [string]$Version = $(if ($env:OTELCOL_VERSION) { $env:OTELCOL_VERSION } else { '0.164.0' })
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # the progress bar makes 5.1 downloads very slow
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dl   = Join-Path $Here '.dl'
New-Item -ItemType Directory -Force -Path $Dl | Out-Null

$Base = "https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v$Version"
$File = "otelcol-contrib_${Version}_linux_amd64.tar.gz"
$Tar  = Join-Path $Dl $File

if (-not (Test-Path $Tar)) {
    Write-Host "Downloading $File ..."
    Invoke-WebRequest -UseBasicParsing -Uri "$Base/$File" -OutFile $Tar
}

$ShaFile = "$Tar.sha256"
Invoke-WebRequest -UseBasicParsing -Uri "$Base/$File.sha256" -OutFile $ShaFile
$want = ((Get-Content $ShaFile -Raw).Trim() -split '\s+')[0].ToLower()
$got  = (Get-FileHash -Algorithm SHA256 -Path $Tar).Hash.ToLower()
if ($want -ne $got) {
    Remove-Item $Tar -Force
    throw "sha256 mismatch for $File (expected $want, got $got). The download was deleted; retry."
}

# Run tar from the app folder with relative paths: avoids GNU tar treating 'C:' as a host.
Push-Location $Here
try {
    tar -xzf ".dl\$File" otelcol-contrib
    if ($LASTEXITCODE -ne 0) { throw "tar failed (exit $LASTEXITCODE)" }
} finally { Pop-Location }

Write-Host "ok  $File  ->  otelcol-contrib ($([math]::Round((Get-Item (Join-Path $Here 'otelcol-contrib')).Length / 1MB)) MB, sha256 verified)"
