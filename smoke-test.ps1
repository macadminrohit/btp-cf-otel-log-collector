# Send one synthetic OTLP log record through the collector route and print the
# trace_id to search for in Splunk. Also proves the route rejects bad tokens.
#
#   .\smoke-test.ps1 -Url https://<route> [-App otel-log-collector]
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [string]$App = 'otel-log-collector'
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Url = $Url.TrimEnd('/')

$token = $env:OTLP_INGEST_TOKEN
if ([string]::IsNullOrEmpty($token)) {
    $token = (& cf env $App) | Select-String -Pattern '^OTLP_INGEST_TOKEN:\s*(\S+)' |
        ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -First 1
}
if ([string]::IsNullOrEmpty($token)) { throw 'No ingest token.' }

function New-Hex([int]$Bytes) {
    $b = New-Object byte[] $Bytes
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
    -join ($b | ForEach-Object { $_.ToString('x2') })
}

$traceId = New-Hex 16
$spanId  = New-Hex 8
$nanos   = "$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())000000000"
$body = '{"resourceLogs":[{"resource":{"attributes":[{"key":"service.name","value":{"stringValue":"collector-smoke-test"}}]},"scopeLogs":[{"logRecords":[{"timeUnixNano":"' + $nanos + '","severityNumber":9,"severityText":"INFO","body":{"stringValue":"collector smoke test"},"traceId":"' + $traceId + '","spanId":"' + $spanId + '"}]}]}]}'

function Send-Log([hashtable]$Headers) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Method Post -Uri "$Url/v1/logs" `
            -ContentType 'application/json' -Headers $Headers -Body $body -TimeoutSec 20
        [int]$r.StatusCode
    } catch {
        if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { throw }
    }
}

Write-Host ("no token      -> {0}   (expect 401)" -f (Send-Log @{}))
Write-Host ("wrong token   -> {0}   (expect 401)" -f (Send-Log @{ Authorization = 'Bearer wrong' }))
Write-Host ("correct token -> {0}   (expect 200)" -f (Send-Log @{ Authorization = "Bearer $token" }))
$token = $null
Write-Host ''
Write-Host 'Search Splunk (last 15 min):'
Write-Host "  index=<your-index> trace_id=$traceId"
