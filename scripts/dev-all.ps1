$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

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

$backend = Start-NamedJob -Name "backend" -Arguments @($root.Path, $python) -Command {
  param($cwd, $pythonExe)
  Set-Location $cwd
  & $pythonExe web_server.py 2>&1
}

$frontend = Start-NamedJob -Name "frontend" -Arguments @($root.Path) -Command {
  param($cwd)
  Set-Location $cwd
  & npm run dev -- --host 127.0.0.1 --strictPort 2>&1
}

Write-Host ""
Write-Host "Hash Context Codex Lab is starting..."
Write-Host "Backend:  http://127.0.0.1:8765"
Write-Host "Frontend: http://127.0.0.1:5174/"
Write-Host "Press Ctrl+C to stop both services."
Write-Host ""

$jobs = @($backend, $frontend)

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
