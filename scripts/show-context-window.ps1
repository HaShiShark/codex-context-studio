$ErrorActionPreference = "Stop"

$controlPort = $env:CODEX_CONTEXT_STUDIO_CONTROL_PORT
if (-not $controlPort) {
  $controlPort = "8790"
}
$loopbackHost = if ($env:CODEX_CONTEXT_STUDIO_HOST) { $env:CODEX_CONTEXT_STUDIO_HOST } else { "localhost" }

$url = "http://${loopbackHost}:$controlPort/show"
try {
  Invoke-WebRequest -Uri $url -Method Post -UseBasicParsing -TimeoutSec 2 | Out-Null
  Write-Host "[codex-context-studio] context workbench opened"
} catch {
  Write-Host "[codex-context-studio] context workbench is not running: $($_.Exception.Message)" -ForegroundColor Red
  exit 1
}
