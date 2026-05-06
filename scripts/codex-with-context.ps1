$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")

$proxyPort = $env:HASH_CONTEXT_PROXY_PORT
if (-not $proxyPort) {
  $proxyPort = "8787"
}

$controlPort = $env:HASH_CONTEXT_CONTROL_PORT
if (-not $controlPort) {
  $controlPort = "8790"
}

function Stop-ProjectProcessOnPort {
  param(
    [int] $Port
  )
  $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  foreach ($connection in $connections) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($connection.OwningProcess)" -ErrorAction SilentlyContinue
    if (-not $process) {
      continue
    }
    $commandLine = [string] $process.CommandLine
    if ($commandLine -like "*hash-context-codex-lab*" -or
        $commandLine -like "*Codex Context Proxy*" -or
        $commandLine -like "*proxy_server.py*" -or
        $commandLine -like "*web_server.py*" -or
        $commandLine -like "*hash-proxy-server*" -or
        $commandLine -like "*hash-web-server*") {
      Write-Host "[hash-context] stopping stale local service on port $Port pid=$($process.ProcessId)" -ForegroundColor DarkYellow
      & taskkill /pid $process.ProcessId /t /f | Out-Null
    }
  }
}

function Get-PackagedWindowExe {
  $installRoot = [System.IO.Path]::GetFullPath((Join-Path $root.Path "..\.."))
  $candidates = @(
    (Join-Path $installRoot "Codex Context Proxy.exe"),
    (Join-Path $installRoot "hashcode.exe")
  )
  foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
      return $candidate
    }
  }
  return ""
}

function Start-ContextWindow {
  $packagedExe = Get-PackagedWindowExe
  if ($packagedExe) {
    return Start-Process -FilePath $packagedExe -WindowStyle Hidden -PassThru
  }

  return Start-Process -FilePath "npm.cmd" -ArgumentList @("run", "window") -WorkingDirectory $root.Path -WindowStyle Hidden -PassThru
}

$hookCommand = (Join-Path $root "scripts\codex-context-hook.cmd").Replace("\", "/")
$hookConfig = "hooks.UserPromptSubmit=[{matcher='*',hooks=[{type='command',command='$hookCommand',timeout=5,statusMessage='HashContext'}]}]"

$configArgs = @(
  "-c", "model_providers.hash-context.name=Hash Context",
  "-c", "model_providers.hash-context.base_url=http://127.0.0.1:$proxyPort/v1",
  "-c", "model_providers.hash-context.requires_openai_auth=true",
  "-c", "model_providers.hash-context.wire_api=responses",
  "-c", "model_providers.hash-context.supports_websockets=false",
  "-c", "model_provider=hash-context",
  "-c", "features.codex_hooks=true",
  "-c", $hookConfig
)

$autoCompactTokenLimit = $env:HASH_CONTEXT_AUTO_COMPACT_TOKEN_LIMIT
if ($autoCompactTokenLimit) {
  $autoCompactTokenLimit = $autoCompactTokenLimit.Trim()
  if ($autoCompactTokenLimit -notmatch '^\d+$') {
    throw "HASH_CONTEXT_AUTO_COMPACT_TOKEN_LIMIT must be an integer token count."
  }
  $configArgs += @("-c", "model_auto_compact_token_limit=$autoCompactTokenLimit")
}

function Test-HttpOk {
  param(
    [string] $Url
  )
  try {
    $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
    return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500)
  } catch {
    return $false
  }
}

function Wait-HttpOk {
  param(
    [string] $Name,
    [string] $Url,
    [int] $TimeoutSeconds = 30
  )
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-HttpOk -Url $Url) {
      Write-Host "[ok] $Name -> $Url" -ForegroundColor Green
      return
    }
    Start-Sleep -Milliseconds 500
  }
  throw "$Name did not become ready: $Url"
}

function ConvertTo-FullPath {
  param([string] $Path)
  if (-not $Path) {
    return ""
  }
  try {
    return [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
  } catch {
    return $Path.TrimEnd("\")
  }
}

function Test-PathUnder {
  param(
    [string] $Path,
    [string] $Parent
  )
  $fullPath = ConvertTo-FullPath $Path
  $fullParent = ConvertTo-FullPath $Parent
  if (-not $fullPath -or -not $fullParent) {
    return $false
  }
  return $fullPath.Equals($fullParent, [System.StringComparison]::OrdinalIgnoreCase) -or
    $fullPath.StartsWith("$fullParent\", [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-CommandPath {
  param([object] $CommandInfo)
  if ($CommandInfo.Source) {
    return [string] $CommandInfo.Source
  }
  if ($CommandInfo.Definition) {
    return [string] $CommandInfo.Definition
  }
  return ""
}

function Resolve-RealCodexCommand {
  if ($env:HASH_CONTEXT_REAL_CODEX -and (Test-Path $env:HASH_CONTEXT_REAL_CODEX)) {
    return (ConvertTo-FullPath $env:HASH_CONTEXT_REAL_CODEX)
  }

  $defaultShimDir = Join-Path $env:USERPROFILE ".hash-context-codex\bin"
  $shimDir = if ($env:HASH_CONTEXT_SHIM_DIR) { $env:HASH_CONTEXT_SHIM_DIR } else { $defaultShimDir }
  $commands = @(Get-Command codex -All -ErrorAction SilentlyContinue)
  foreach ($commandInfo in $commands) {
    $candidate = Get-CommandPath $commandInfo
    if (-not $candidate) {
      continue
    }
    if (Test-PathUnder -Path $candidate -Parent $shimDir) {
      continue
    }
    if (Test-Path $candidate) {
      return (ConvertTo-FullPath $candidate)
    }
  }

  return ""
}

$codexCommand = Resolve-RealCodexCommand
if (-not $codexCommand) {
  throw "codex command was not found in PATH. Please confirm that running 'codex' directly works in this terminal."
}

$localNoProxy = "127.0.0.1,localhost,::1"
$env:NO_PROXY = if ($env:NO_PROXY) { "$localNoProxy,$env:NO_PROXY" } else { $localNoProxy }
$env:no_proxy = if ($env:no_proxy) { "$localNoProxy,$env:no_proxy" } else { $localNoProxy }

Stop-ProjectProcessOnPort -Port ([int] $proxyPort)
Stop-ProjectProcessOnPort -Port 8765
Stop-ProjectProcessOnPort -Port 5174
Stop-ProjectProcessOnPort -Port ([int] $controlPort)

Write-Host "[hash-context] starting local services and hidden context window..." -ForegroundColor Cyan
$previousStartHidden = $env:HASH_CONTEXT_START_HIDDEN
$previousControlPort = $env:HASH_CONTEXT_CONTROL_PORT
$env:HASH_CONTEXT_START_HIDDEN = "1"
$env:HASH_CONTEXT_CONTROL_PORT = $controlPort
$usesPackagedWindow = [bool](Get-PackagedWindowExe)
$windowProcess = Start-ContextWindow
if ($null -eq $previousStartHidden) {
  Remove-Item Env:\HASH_CONTEXT_START_HIDDEN -ErrorAction SilentlyContinue
} else {
  $env:HASH_CONTEXT_START_HIDDEN = $previousStartHidden
}
if ($null -eq $previousControlPort) {
  Remove-Item Env:\HASH_CONTEXT_CONTROL_PORT -ErrorAction SilentlyContinue
} else {
  $env:HASH_CONTEXT_CONTROL_PORT = $previousControlPort
}
Write-Host "[hash-context] launcher pid: $($windowProcess.Id)"

Wait-HttpOk -Name "proxy" -Url "http://127.0.0.1:$proxyPort/api/proxy/sessions"
Wait-HttpOk -Name "backend" -Url "http://127.0.0.1:8765/api/init"
if ($usesPackagedWindow) {
  Wait-HttpOk -Name "frontend" -Url "http://127.0.0.1:8765/react/"
} else {
  Wait-HttpOk -Name "frontend" -Url "http://127.0.0.1:5174/"
}
Wait-HttpOk -Name "window-control" -Url "http://127.0.0.1:$controlPort/health"

Write-Host "[hash-context] starting Codex through local proxy..." -ForegroundColor Cyan
Write-Host "[hash-context] base_url=http://127.0.0.1:$proxyPort/v1"
Write-Host "[hash-context] type context or ctx inside Codex to open the workbench"
Write-Host "[hash-context] logs: $($root.Path)\logs\electron-window.log"
Write-Host ""

$codexExitCode = 0
try {
  & $codexCommand `
    @configArgs `
    @args
  $codexExitCode = $LASTEXITCODE
} finally {
  if ($windowProcess -and -not $windowProcess.HasExited) {
    & taskkill /pid $windowProcess.Id /t /f | Out-Null
  }
}

exit $codexExitCode
