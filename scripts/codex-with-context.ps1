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
$loopbackHost = if ($env:HASH_CONTEXT_HOST) { $env:HASH_CONTEXT_HOST } else { "localhost" }
$serviceProbeHost = if ($loopbackHost -eq "localhost") { "127.0.0.1" } else { $loopbackHost }

$topBeginManaged = "# BEGIN HASH_CONTEXT_DESKTOP_TOP"
$topEndManaged = "# END HASH_CONTEXT_DESKTOP_TOP"
$providerBeginManaged = "# BEGIN HASH_CONTEXT_DESKTOP_PROVIDER"
$providerEndManaged = "# END HASH_CONTEXT_DESKTOP_PROVIDER"

function Get-CodexUpstreamInfo {
  $configPath = if ($env:HASH_CONTEXT_DESKTOP_CONFIG) { $env:HASH_CONTEXT_DESKTOP_CONFIG } else { Join-Path $env:USERPROFILE ".codex\config.toml" }

  if (-not (Test-Path $configPath)) {
    Write-Host "[hash-context] Codex config not found: $configPath" -ForegroundColor Red
    Write-Host "[hash-context] Please run 'codex' first to login (codex login) or configure a third-party API provider." -ForegroundColor Red
    throw "Codex config file not found."
  }

  $content = Get-Content -Raw -Path $configPath -ErrorAction Stop

  $escapedTopBegin = [regex]::Escape($topBeginManaged)
  $escapedTopEnd = [regex]::Escape($topEndManaged)
  $escapedProviderBegin = [regex]::Escape($providerBeginManaged)
  $escapedProviderEnd = [regex]::Escape($providerEndManaged)
  $content = [regex]::Replace($content, "(?ms)\r?\n?$escapedTopBegin\r?\n.*?\r?\n$escapedTopEnd\r?\n?", "`r`n")
  $content = [regex]::Replace($content, "(?ms)\r?\n?$escapedProviderBegin\r?\n.*?\r?\n$escapedProviderEnd\r?\n?", "`r`n")

  if (-not $content.Trim()) {
    Write-Host "[hash-context] Codex config is empty." -ForegroundColor Red
    Write-Host "[hash-context] Please run 'codex' first to login (codex login) or configure a third-party API provider." -ForegroundColor Red
    throw "Codex config file is empty."
  }

  $modelProvider = "openai"
  $mpMatch = [regex]::Match($content, '^\s*model_provider\s*=\s*"([^"]+)"', [System.Text.RegularExpressions.RegexOptions]::Multiline)
  if ($mpMatch.Success) { $modelProvider = $mpMatch.Groups[1].Value }

  $openaiBaseUrl = ""
  $obuMatch = [regex]::Match($content, '^\s*openai_base_url\s*=\s*"([^"]+)"', [System.Text.RegularExpressions.RegexOptions]::Multiline)
  if ($obuMatch.Success) { $openaiBaseUrl = $obuMatch.Groups[1].Value }

  $effectiveBaseUrl = ""
  $providerApiKey = ""
  $providerEnvKey = ""
  $providerBearerToken = ""

  if ($openaiBaseUrl) { $effectiveBaseUrl = $openaiBaseUrl }

  if ($modelProvider -ne "openai") {
    $escapedId = [regex]::Escape($modelProvider)
    $sectionPattern = "\[model_providers\.$escapedId\][\s\S]*?(?=\[\s*model_providers\.|\z)"
    $sectionMatch = [regex]::Match($content, $sectionPattern)
    if (-not $sectionMatch.Success) {
      throw "Provider section [model_providers.$modelProvider] not found."
    }
    $sectionText = $sectionMatch.Value
    if ($sectionText -match 'base_url\s*=\s*"([^"]+)"') { $effectiveBaseUrl = $Matches[1] }
    if ($sectionText -match 'api_key\s*=\s*"([^"]+)"') { $providerApiKey = $Matches[1] }
    if ($sectionText -match 'env_key\s*=\s*"([^"]+)"') { $providerEnvKey = $Matches[1] }
    if ($sectionText -match 'experimental_bearer_token\s*=\s*"([^"]+)"') { $providerBearerToken = $Matches[1] }
  }

  if (-not $effectiveBaseUrl) { $effectiveBaseUrl = "https://api.openai.com/v1" }
  $effectiveBaseUrl = $effectiveBaseUrl.TrimEnd("/")
  $isOfficialOpenAI = ($effectiveBaseUrl -match "api\.openai\.com/v1$")

  $upstreamKind = ""
  $effectiveApiKey = ""
  $errorMessage = ""

  if ($isOfficialOpenAI) {
    $authPath = Join-Path $env:USERPROFILE ".codex\auth.json"
    $hasSubscription = $false
    if (Test-Path $authPath) {
      try {
        $auth = Get-Content -Raw -Path $authPath | ConvertFrom-Json
        if ($auth -and $auth.tokens -and $auth.tokens.access_token -and $auth.tokens.account_id) { $hasSubscription = $true }
      } catch {}
    }

    $hasApiKey = $false
    if ($providerEnvKey) {
      $envValue = [Environment]::GetEnvironmentVariable($providerEnvKey, "User")
      if (-not $envValue) { $envValue = [Environment]::GetEnvironmentVariable($providerEnvKey, "Process") }
      if ($envValue) { $hasApiKey = $true; $effectiveApiKey = $envValue }
    }
    if (-not $hasApiKey -and $providerApiKey) { $hasApiKey = $true; $effectiveApiKey = $providerApiKey }
    if (-not $hasApiKey) {
      $openaiKey = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "User")
      if (-not $openaiKey) { $openaiKey = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "Process") }
      if ($openaiKey) { $hasApiKey = $true; $effectiveApiKey = $openaiKey }
    }
    if (-not $hasApiKey -and $auth -and $auth.OPENAI_API_KEY) {
      $hasApiKey = $true; $effectiveApiKey = [string] $auth.OPENAI_API_KEY
    }

    if ($hasSubscription) { $upstreamKind = "subscription" }
    elseif ($hasApiKey) { $upstreamKind = "api_key" }
    else { $errorMessage = "No authentication found. Please login via 'codex login' or set OPENAI_API_KEY." }
  } else {
    if ($providerApiKey) { $effectiveApiKey = $providerApiKey }
    elseif ($providerBearerToken) { $effectiveApiKey = $providerBearerToken }
    elseif ($providerEnvKey) {
      $envValue = [Environment]::GetEnvironmentVariable($providerEnvKey, "User")
      if (-not $envValue) { $envValue = [Environment]::GetEnvironmentVariable($providerEnvKey, "Process") }
      if ($envValue) { $effectiveApiKey = $envValue }
    }

    if (-not $effectiveApiKey) {
      $authPath = Join-Path $env:USERPROFILE ".codex\auth.json"
      if (Test-Path $authPath) {
        try {
          $auth = Get-Content -Raw -Path $authPath | ConvertFrom-Json
          if ($auth -and $auth.OPENAI_API_KEY) { $effectiveApiKey = [string] $auth.OPENAI_API_KEY }
        } catch {}
      }
    }
    if (-not $effectiveApiKey) {
      $openaiKey = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "User")
      if (-not $openaiKey) { $openaiKey = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "Process") }
      if ($openaiKey) { $effectiveApiKey = $openaiKey }
    }

    if (-not $effectiveApiKey) {
      $errorMessage = "Third-party provider '$modelProvider' is missing API key. Please set api_key, env_key, or experimental_bearer_token in [model_providers.$modelProvider], or set OPENAI_API_KEY in auth.json or environment."
    } else { $upstreamKind = "third_party" }
  }

  if (-not $upstreamKind) {
    Write-Host "[hash-context] $errorMessage" -ForegroundColor Red
    throw $errorMessage
  }

  return @{ kind = $upstreamKind; effective_base_url = $effectiveBaseUrl; api_key = $effectiveApiKey; provider_id = $modelProvider }
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
        $commandLine -like "*proxy_fastapi.py*" -or
        $commandLine -like "*proxy_server.py*" -or
        $commandLine -like "*web_server.py*" -or
        $commandLine -like "*backend.proxy_fastapi*" -or
        $commandLine -like "*backend.proxy_server*" -or
        $commandLine -like "*backend.web_server*" -or
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

  $logDir = Join-Path $root.Path "logs"
  New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  return Start-Process `
    -FilePath "npm.cmd" `
    -ArgumentList @("run", "window") `
    -WorkingDirectory $root.Path `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "electron-window.stdout.log") `
    -RedirectStandardError (Join-Path $logDir "electron-window.stderr.log") `
    -PassThru
}

$upstreamInfo = Get-CodexUpstreamInfo
$requiresAuth = if ($upstreamInfo.kind -eq "third_party") { "false" } else { "true" }

$hookCommand = (Join-Path $root "scripts\codex-context-hook.cmd").Replace("\", "/")
$hookConfig = "hooks.UserPromptSubmit=[{matcher='*',hooks=[{type='command',command='$hookCommand',timeout=10,statusMessage='HashContext'}]}]"

$configArgs = @(
  "-c", "model_providers.hash-context.name=Hash Context",
  "-c", "model_providers.hash-context.base_url=http://${loopbackHost}:$proxyPort/v1",
  "-c", "model_providers.hash-context.requires_openai_auth=$requiresAuth",
  "-c", "model_providers.hash-context.wire_api=responses",
  "-c", "model_providers.hash-context.supports_websockets=false",
  "-c", "model_provider=hash-context",
  "-c", "features.hooks=true",
  "-c", $hookConfig
)

if ($upstreamInfo.kind -eq "third_party") {
  $configArgs += @("-c", "model_context_window=200000")
}

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

function Test-TcpPortOpen {
  param([int] $Port)
  $client = [System.Net.Sockets.TcpClient]::new()
  try {
    $async = $client.BeginConnect("$loopbackHost", $Port, $null, $null)
    if (-not $async.AsyncWaitHandle.WaitOne(500)) {
      return $false
    }
    $client.EndConnect($async)
    return $true
  } catch {
    return $false
  } finally {
    $client.Close()
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

function Wait-TcpPortOpen {
  param(
    [string] $Name,
    [int] $Port,
    [int] $TimeoutSeconds = 30
  )
  $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
  while ((Get-Date) -lt $deadline) {
    if (Test-TcpPortOpen -Port $Port) {
      Write-Host "[ok] $Name -> ${loopbackHost}:$Port" -ForegroundColor Green
      return
    }
    Start-Sleep -Milliseconds 500
  }
  throw "$Name did not become ready: ${loopbackHost}:$Port"
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

$localNoProxy = "$loopbackHost,localhost,127.0.0.1,::1"
$env:NO_PROXY = if ($env:NO_PROXY) { "$localNoProxy,$env:NO_PROXY" } else { $localNoProxy }
$env:no_proxy = if ($env:no_proxy) { "$localNoProxy,$env:no_proxy" } else { $localNoProxy }

Stop-ProjectProcessOnPort -Port ([int] $proxyPort)
Stop-ProjectProcessOnPort -Port 8765
Stop-ProjectProcessOnPort -Port 5174
Stop-ProjectProcessOnPort -Port ([int] $controlPort)

Write-Host "[hash-context] starting local services and hidden context window..." -ForegroundColor Cyan
$previousStartHidden = $env:HASH_CONTEXT_START_HIDDEN
$previousControlPort = $env:HASH_CONTEXT_CONTROL_PORT
$previousForceUrl = $env:HASH_CONTEXT_FORCE_UPSTREAM_BASE_URL
$previousForceKey = $env:HASH_CONTEXT_FORCE_UPSTREAM_API_KEY
$env:HASH_CONTEXT_START_HIDDEN = "1"
$env:HASH_CONTEXT_CONTROL_PORT = $controlPort
if ($upstreamInfo.kind -eq "third_party") {
  $env:HASH_CONTEXT_FORCE_UPSTREAM_BASE_URL = $upstreamInfo.effective_base_url
  $env:HASH_CONTEXT_FORCE_UPSTREAM_API_KEY = $upstreamInfo.api_key
}
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

Wait-HttpOk -Name "proxy" -Url "http://${serviceProbeHost}:$proxyPort/api/proxy/health" -TimeoutSeconds 30
Wait-HttpOk -Name "backend" -Url "http://${serviceProbeHost}:8765/api/health" -TimeoutSeconds 30
if ($usesPackagedWindow) {
  Wait-HttpOk -Name "frontend" -Url "http://${loopbackHost}:8765/react/"
} else {
  Wait-HttpOk -Name "frontend" -Url "http://${loopbackHost}:5174/"
}
Wait-HttpOk -Name "window-control" -Url "http://${loopbackHost}:$controlPort/health" -TimeoutSeconds 90

Write-Host "[hash-context] starting Codex through local proxy..." -ForegroundColor Cyan
Write-Host "[hash-context] base_url=http://${loopbackHost}:$proxyPort/v1"
if ($upstreamInfo.kind -eq "third_party") {
  Write-Host "[hash-context] upstream: $($upstreamInfo.effective_base_url) (third-party)" -ForegroundColor Cyan
}
Write-Host "[hash-context] type context or ctx inside Codex to open the workbench"
Write-Host "[hash-context] if Codex says hooks need review, run /hooks and approve HashContext once"
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
  if ($null -eq $previousForceUrl) {
    Remove-Item Env:\HASH_CONTEXT_FORCE_UPSTREAM_BASE_URL -ErrorAction SilentlyContinue
  } else {
    $env:HASH_CONTEXT_FORCE_UPSTREAM_BASE_URL = $previousForceUrl
  }
  if ($null -eq $previousForceKey) {
    Remove-Item Env:\HASH_CONTEXT_FORCE_UPSTREAM_API_KEY -ErrorAction SilentlyContinue
  } else {
    $env:HASH_CONTEXT_FORCE_UPSTREAM_API_KEY = $previousForceKey
  }
}

exit $codexExitCode
