$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

$loopbackHost = if ($env:HASH_CONTEXT_HOST) { $env:HASH_CONTEXT_HOST } else { "localhost" }
$backendPort = if ($env:HASH_WEB_PORT) { $env:HASH_WEB_PORT } else { "8765" }
$frontendPort = if ($env:HASH_CONTEXT_FRONTEND_PORT) { $env:HASH_CONTEXT_FRONTEND_PORT } else { "5174" }
$proxyPort = if ($env:HASH_CONTEXT_PROXY_PORT) { $env:HASH_CONTEXT_PROXY_PORT } else { "8787" }

$python = if (Test-Path ".venv\Scripts\python.exe") {
  ".venv\Scripts\python.exe"
} else {
  "python"
}

function Start-NamedJob {
  param(
    [string]$Name,
    [scriptblock]$Command,
    [object[]]$Arguments
  )

  Start-Job -Name $Name -ScriptBlock $Command -ArgumentList $Arguments
}

$backend = Start-NamedJob -Name "backend" -Arguments @($root.Path, $python, $loopbackHost, $backendPort) -Command {
  param($cwd, $pythonExe, $hostName, $port)
  Set-Location $cwd
  $env:HASH_WEB_HOST = $hostName
  $env:HASH_WEB_PORT = $port
  & $pythonExe -m backend.web_server 2>&1
}

$proxy = Start-NamedJob -Name "proxy" -Arguments @($root.Path, $python, $loopbackHost, $proxyPort) -Command {
  param($cwd, $pythonExe, $hostName, $port)
  Set-Location $cwd
  $env:HASH_CONTEXT_PROXY_HOST = $hostName
  $env:HASH_CONTEXT_PROXY_PORT = $port
  & $pythonExe -m backend.proxy_fastapi 2>&1
}

$frontend = Start-NamedJob -Name "frontend" -Arguments @($root.Path, $loopbackHost, $frontendPort) -Command {
  param($cwd, $hostName, $port)
  Set-Location $cwd
  & npm run dev -- --host $hostName --port $port --strictPort 2>&1
}

Write-Host ""
Write-Host "Hash Context Codex Lab is starting..."
Write-Host "Backend:  http://${loopbackHost}:$backendPort"
Write-Host "Proxy:    http://${loopbackHost}:$proxyPort"
Write-Host "Frontend: http://${loopbackHost}:$frontendPort/"
Write-Host "Press Ctrl+C to stop local services."
Write-Host ""

$jobs = @($backend, $proxy, $frontend)

try {
  while ($true) {
    foreach ($job in $jobs) {
      Receive-Job -Job $job | ForEach-Object {
        Write-Host "[$($job.Name)] $_"
      }

      if ($job.State -in @("Completed", "Failed", "Stopped")) {
        Receive-Job -Job $job | ForEach-Object {
          Write-Host "[$($job.Name)] $_"
        }
        throw "$($job.Name) stopped with state $($job.State)."
      }
    }

    Start-Sleep -Milliseconds 250
  }
}
finally {
  Write-Host ""
  Write-Host "Stopping Hash Context Codex Lab..."
  $jobs | Where-Object { $_.State -eq "Running" } | Stop-Job
  $jobs | Remove-Job -Force
  Write-Host "Stopped."
}
