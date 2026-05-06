$ErrorActionPreference = "Stop"

$controlPort = $env:HASH_CONTEXT_CONTROL_PORT
if (-not $controlPort) {
  $controlPort = "8790"
}

$url = "http://127.0.0.1:$controlPort/show"
try {
  Invoke-WebRequest -Uri $url -Method Post -UseBasicParsing -TimeoutSec 2 | Out-Null
  Write-Host "[hash-context] context workbench opened"
} catch {
  Write-Host "[hash-context] context workbench is not running: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
