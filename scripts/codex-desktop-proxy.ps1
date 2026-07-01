param(
  [Parameter(Position = 0)]
  [string] $Command = "status"
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$codexHome = Join-Path $env:USERPROFILE ".codex"
$configPath = if ($env:HASH_CONTEXT_DESKTOP_CONFIG) { $env:HASH_CONTEXT_DESKTOP_CONFIG } else { Join-Path $codexHome "config.toml" }
$stateDir = if ($env:HASH_CONTEXT_DESKTOP_STATE_DIR) { $env:HASH_CONTEXT_DESKTOP_STATE_DIR } else { Join-Path $env:USERPROFILE ".hash-context-codex" }
$statePath = Join-Path $stateDir "codex-desktop-proxy.json"
$proxyPort = if ($env:HASH_CONTEXT_PROXY_PORT) { $env:HASH_CONTEXT_PROXY_PORT } else { "8787" }
$controlPort = if ($env:HASH_CONTEXT_CONTROL_PORT) { $env:HASH_CONTEXT_CONTROL_PORT } else { "8790" }
$loopbackHost = if ($env:HASH_CONTEXT_HOST) { $env:HASH_CONTEXT_HOST } else { "localhost" }
$serviceProbeHost = if ($loopbackHost -eq "localhost") { "127.0.0.1" } else { $loopbackHost }
$desktopDataDir = if ($env:HASH_CONTEXT_DESKTOP_DATA_DIR) { $env:HASH_CONTEXT_DESKTOP_DATA_DIR } else { Join-Path $env:APPDATA "hash-context-codex-lab\data" }

function Read-DesktopState {
  if (-not (Test-Path $statePath)) {
    return $null
  }
  try {
    return (Get-Content -Raw -Path $statePath | ConvertFrom-Json)
  } catch {
    return $null
  }
}

function Save-DesktopState {
  param([hashtable] $State)
  New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
  $State | ConvertTo-Json -Depth 6 | Set-Content -Path $statePath -Encoding UTF8
}

function Get-ProxySnapshot {
  $sessionCount = 0
  $activeSessionId = ""
  $dataDir = $desktopDataDir
  if (-not (Test-Path $dataDir)) {
    $dataDir = Join-Path $projectRoot.Path "data"
  }
  $logPath = Join-Path $dataDir "proxy.log"
  $logLength = 0
  if (Test-Path $logPath) {
    $logLength = (Get-Item $logPath).Length
    try {
      $sessionIds = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
      foreach ($line in Get-Content -Path $logPath -ErrorAction Stop) {
        $match = [regex]::Match($line, "request session=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
        if ($match.Success) {
          $activeSessionId = $match.Groups[1].Value.ToLowerInvariant()
          [void] $sessionIds.Add($activeSessionId)
        }
      }
      $sessionCount = $sessionIds.Count
    } catch {
    }
  }

  $proxyStatePath = Join-Path $dataDir "proxy_state.json"
  if (-not $activeSessionId -and (Test-Path $proxyStatePath)) {
    try {
      $head = Get-Content -Path $proxyStatePath -TotalCount 5 -ErrorAction Stop
      foreach ($line in $head) {
        $match = [regex]::Match($line, '^\s*"active_session_id"\s*:\s*"([^"]*)"')
        if ($match.Success) {
          $activeSessionId = [string] $match.Groups[1].Value
          break
        }
      }
    } catch {
    }
  }

  return @{
    session_count = $sessionCount
    active_session_id = $activeSessionId
    proxy_log_length = $logLength
    data_dir = $dataDir
  }
}

function Repair-ProjectTables {
  param([string] $Text)

  if (-not $Text) {
    return ""
  }

  $lines = $Text -split "\r?\n"
  $kept = New-Object System.Collections.Generic.List[string]
  $skipMalformedProject = $false
  $removedCount = 0

  foreach ($line in $lines) {
    $isTableHeader = ($line -match "^\s*\[")

    if ($skipMalformedProject) {
      if ($isTableHeader) {
        $skipMalformedProject = $false
      } else {
        continue
      }
    }

    if ($isTableHeader -and $line -match "^\s*\[projects\.") {
      $validLiteralPath = ($line -match "^\s*\[projects\.'[^'\r\n]*'\]\s*$")
      $validBasicPath = ($line -match '^\s*\[projects\."(?:\\.|[^"\\\r\n])*"\]\s*$')
      if (-not ($validLiteralPath -or $validBasicPath)) {
        $removedCount += 1
        $skipMalformedProject = $true
        continue
      }
    }

    [void] $kept.Add($line)
  }

  if ($removedCount -gt 0) {
    Write-Host "[hash-context] removed malformed projects tables: $removedCount" -ForegroundColor DarkYellow
  }

  return (($kept -join "`r`n").TrimEnd() + "`r`n")
}

function ConvertTo-TomlBasicString {
  param([string] $Value)
  return '"' + $Value.Replace("\", "\\").Replace('"', '\"') + '"'
}

function Remove-DesktopManagedConfig {
  param(
    [string] $Text,
    [bool] $RemoveContextWindow = $false
  )

  $clean = $Text
  $clean = [regex]::Replace($clean, '(?m)^\s*hooks\.UserPromptSubmit\s*=.*\r?\n?', '')
  $clean = [regex]::Replace($clean, '(?ms)(?:^|\r?\n)\s*\[model_providers\.hash-context\]\s*\r?\n.*?(?=(?:\r?\n\s*\[)|\z)', "`r`n")
  if ($RemoveContextWindow) {
    $clean = [regex]::Replace($clean, '(?m)^\s*model_context_window\s*=\s*\d+\s*\r?\n?', '')
  }
  return ($clean.TrimEnd() + "`r`n")
}

function Test-CodexUpstreamInfo {
  param([string] $ConfigPath)

  if (-not (Test-Path $ConfigPath)) {
    Write-Host "[hash-context] Codex config not found: $ConfigPath" -ForegroundColor Red
    Write-Host "[hash-context] Please run 'codex' first to login (codex login) or configure a third-party API provider." -ForegroundColor Red
    throw "Codex config file not found."
  }

  $content = Get-Content -Raw -Path $ConfigPath -ErrorAction Stop
  if (-not $content.Trim()) {
    Write-Host "[hash-context] Codex config is empty: $ConfigPath" -ForegroundColor Red
    Write-Host "[hash-context] Please run 'codex' first to login (codex login) or configure a third-party API provider." -ForegroundColor Red
    throw "Codex config file is empty."
  }

  $modelProvider = "openai"
  $mpMatch = [regex]::Match($content, '^\s*model_provider\s*=\s*"([^"]+)"', [System.Text.RegularExpressions.RegexOptions]::Multiline)
  if ($mpMatch.Success) {
    $modelProvider = $mpMatch.Groups[1].Value
  }

  $openaiBaseUrl = ""
  $obuMatch = [regex]::Match($content, '^\s*openai_base_url\s*=\s*"([^"]+)"', [System.Text.RegularExpressions.RegexOptions]::Multiline)
  if ($obuMatch.Success) {
    $openaiBaseUrl = $obuMatch.Groups[1].Value
  }

  $effectiveBaseUrl = ""
  $providerName = ""
  $providerApiKey = ""
  $providerEnvKey = ""
  $providerBearerToken = ""
  $providerWireApi = ""
  $providerRequiresAuth = ""

  if ($openaiBaseUrl) {
    $effectiveBaseUrl = $openaiBaseUrl
  }

  if ($modelProvider -ne "openai") {
    $escapedId = [regex]::Escape($modelProvider)
    $sectionPattern = "\[model_providers\.$escapedId\][\s\S]*?(?=\[\s*model_providers\.|\z)"
    $sectionMatch = [regex]::Match($content, $sectionPattern)
    if (-not $sectionMatch.Success) {
      Write-Host "[hash-context] model_provider '$modelProvider' has no [model_providers.$modelProvider] section." -ForegroundColor Red
      Write-Host "[hash-context] Please configure [model_providers.$modelProvider] in your config or switch to a valid provider." -ForegroundColor Red
      throw "Provider section [model_providers.$modelProvider] not found."
    }

    $sectionText = $sectionMatch.Value
    if ($sectionText -match 'base_url\s*=\s*"([^"]+)"') {
      $effectiveBaseUrl = $Matches[1]
    }
    if ($sectionText -match 'name\s*=\s*"([^"]*)"') {
      $providerName = $Matches[1]
    }
    if ($sectionText -match 'api_key\s*=\s*"([^"]+)"') {
      $providerApiKey = $Matches[1]
    }
    if ($sectionText -match 'env_key\s*=\s*"([^"]+)"') {
      $providerEnvKey = $Matches[1]
    }
    if ($sectionText -match 'experimental_bearer_token\s*=\s*"([^"]+)"') {
      $providerBearerToken = $Matches[1]
    }
    if ($sectionText -match 'wire_api\s*=\s*"([^"]+)"') {
      $providerWireApi = $Matches[1]
    }
    if ($sectionText -match 'requires_openai_auth\s*=\s*(true|false)') {
      $providerRequiresAuth = $Matches[1]
    }
  }

  if (-not $effectiveBaseUrl) {
    $effectiveBaseUrl = "https://api.openai.com/v1"
  }

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
        if ($auth -and $auth.tokens) {
          $accessToken = if ($auth.tokens.access_token) { [string] $auth.tokens.access_token } else { "" }
          $accountId = if ($auth.tokens.account_id) { [string] $auth.tokens.account_id } else { "" }
          if ($accessToken -and $accountId) {
            $hasSubscription = $true
          }
        }
      } catch {}
    }

    $hasApiKey = $false
    if ($providerEnvKey) {
      $envValue = [Environment]::GetEnvironmentVariable($providerEnvKey, "User")
      if (-not $envValue) { $envValue = [Environment]::GetEnvironmentVariable($providerEnvKey, "Process") }
      if ($envValue) {
        $hasApiKey = $true
        $effectiveApiKey = $envValue
      }
    }
    if (-not $hasApiKey -and $providerApiKey) {
      $hasApiKey = $true
      $effectiveApiKey = $providerApiKey
    }
    if (-not $hasApiKey) {
      $openaiKey = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "User")
      if (-not $openaiKey) { $openaiKey = [Environment]::GetEnvironmentVariable("OPENAI_API_KEY", "Process") }
      if ($openaiKey) {
        $hasApiKey = $true
        $effectiveApiKey = $openaiKey
      }
    }
    if (-not $hasApiKey -and $auth -and $auth.OPENAI_API_KEY) {
      $hasApiKey = $true
      $effectiveApiKey = [string] $auth.OPENAI_API_KEY
    }

    if ($hasSubscription) {
      $upstreamKind = "subscription"
    } elseif ($hasApiKey) {
      $upstreamKind = "api_key"
    } else {
      $errorMessage = "No authentication found. Please login via 'codex login' or set OPENAI_API_KEY."
    }
  } else {
    if ($providerApiKey) {
      $effectiveApiKey = $providerApiKey
    } elseif ($providerBearerToken) {
      $effectiveApiKey = $providerBearerToken
    } elseif ($providerEnvKey) {
      $envValue = [Environment]::GetEnvironmentVariable($providerEnvKey, "User")
      if (-not $envValue) { $envValue = [Environment]::GetEnvironmentVariable($providerEnvKey, "Process") }
      if ($envValue) {
        $effectiveApiKey = $envValue
      }
    }

    if (-not $effectiveApiKey) {
      $authPath = Join-Path $env:USERPROFILE ".codex\auth.json"
      if (Test-Path $authPath) {
        try {
          $auth = Get-Content -Raw -Path $authPath | ConvertFrom-Json
          if ($auth -and $auth.OPENAI_API_KEY) {
            $effectiveApiKey = [string] $auth.OPENAI_API_KEY
          }
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
    } else {
      $upstreamKind = "third_party"
    }
  }

  if (-not $upstreamKind) {
    Write-Host "[hash-context] $errorMessage" -ForegroundColor Red
    throw $errorMessage
  }

  return @{
    kind = $upstreamKind
    effective_base_url = $effectiveBaseUrl
    api_key = $effectiveApiKey
    provider_id = $modelProvider
    provider_name = $providerName
    provider_wire_api = $providerWireApi
    provider_requires_auth = $providerRequiresAuth
    provider_env_key = $providerEnvKey
    provider_api_key = $providerApiKey
    provider_bearer_token = $providerBearerToken
  }
}

function Set-DesktopConfigEnabled {
  param(
    [string] $Mode,
    [bool] $RequiresOpenAiAuth = $true,
    [hashtable] $UpstreamInfo = $null
  )

  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $configPath) | Out-Null
  New-Item -ItemType Directory -Force -Path $stateDir | Out-Null

  $hadConfig = Test-Path $configPath
  $text = if ($hadConfig) {
    Repair-ProjectTables -Text (Get-Content -Raw -Path $configPath)
  } else {
    ""
  }

  $originalProvider = ""
  $mpMatch = [regex]::Match($text, '^\s*model_provider\s*=\s*"([^"]+)"', [System.Text.RegularExpressions.RegexOptions]::Multiline)
  if ($mpMatch.Success) {
    $originalProvider = $mpMatch.Groups[1].Value
  }

  $existingState = Read-DesktopState
  if ($originalProvider -eq "hash-context" -and $existingState -and $existingState.original_provider) {
    $originalProvider = [string] $existingState.original_provider
  }

  $text = Remove-DesktopManagedConfig -Text $text -RemoveContextWindow:(-not $RequiresOpenAiAuth)
  $text = $text -replace '(?m)^\s*model_provider\s*=\s*"[^"]*"\s*\r?\n?', ''

  $hookPath = (Join-Path $projectRoot.Path "scripts\codex-context-hook.cmd").Replace("\", "/")
  $hookCommand = ConvertTo-TomlBasicString $hookPath

  if ($text -notmatch '(?ms)\bhooks\s*=\s*true') {
    if ($text -match '(?m)^\[features\]') {
      $text = $text -replace '(?m)(^\[features\]\r?\n)', "`$1hooks = true`r`n"
    } else {
      $text = "[features]`r`nhooks = true`r`n`r`n" + $text
    }
  }

  $requiresAuth = if ($RequiresOpenAiAuth) { "true" } else { "false" }
  $contextWindowLine = ""
  if (-not $RequiresOpenAiAuth) {
    $contextWindowLine = "model_context_window = 200000`r`n"
  }

  $providerBlock = @"
[model_providers.hash-context]
name = "Hash Context"
base_url = "http://${loopbackHost}:$proxyPort/v1"
requires_openai_auth = $requiresAuth
wire_api = "responses"
supports_websockets = false
"@

  $hookBlock = @"
hooks.UserPromptSubmit = [{ matcher = "*", hooks = [{ type = "command", command = $hookCommand, timeout = 10, statusMessage = "HashContext" }] }]
"@

  $text = $text.TrimEnd()
  $header = "model_provider = `"hash-context`"`r`n" + $contextWindowLine + $hookBlock + "`r`n"
  $text = $header + $text.TrimEnd() + "`r`n`r`n" + $providerBlock + "`r`n"
  Set-Content -Path $configPath -Value $text -Encoding UTF8

  $snapshot = Get-ProxySnapshot
  $stateData = @{
    version = 2
    enabled = $true
    mode = $Mode
    config_path = $configPath
    had_config = $hadConfig
    service_pid = 0
    project_root = $projectRoot.Path
    proxy_port = $proxyPort
    control_port = $controlPort
    data_dir = $snapshot.data_dir
    session_count_before = $snapshot.session_count
    proxy_log_length_before = $snapshot.proxy_log_length
    updated_at = (Get-Date).ToUniversalTime().ToString("o")
    original_provider = $originalProvider
    upstream_kind = if ($UpstreamInfo) { [string] $UpstreamInfo.kind } else { "" }
    upstream_base_url = if ($UpstreamInfo) { [string] $UpstreamInfo.effective_base_url } else { "" }
    upstream_api_key = if ($UpstreamInfo) { [string] $UpstreamInfo.api_key } else { "" }
    upstream_provider_id = if ($UpstreamInfo) { [string] $UpstreamInfo.provider_id } else { "" }
  }
  Save-DesktopState $stateData
}

function Restore-DesktopConfig {
  $state = Read-DesktopState
  if (-not $state) {
    Write-Host "[hash-context] no proxy state to restore" -ForegroundColor DarkYellow
    return
  }

  if (-not (Test-Path $configPath)) {
    Write-Host "[hash-context] config missing, nothing to restore"
    return
  }

  $text = Repair-ProjectTables -Text (Get-Content -Raw -Path $configPath)

  $text = Remove-DesktopManagedConfig -Text $text -RemoveContextWindow:($state.upstream_kind -eq "third_party")
  $text = $text -replace '(?m)^\s*model_provider\s*=\s*"hash-context"\s*\r?\n?', ''

  $text = $text.Trim()
  if ($text -and $text -notmatch '(?m)^\s*model_provider\s*=' -and $state.original_provider) {
    $providerId = [string] $state.original_provider
    $text = "model_provider = `"$providerId`"`r`n`r`n" + $text
  }

  if ($text.Trim()) {
    Set-Content -Path $configPath -Value ($text.TrimEnd() + "`r`n") -Encoding UTF8
    Write-Host "[hash-context] restored config"
  } else {
    Remove-Item -Path $configPath -Force -ErrorAction SilentlyContinue
    Write-Host "[hash-context] removed empty config"
  }
}

function Repair-DesktopConfig {
  if (-not (Test-Path $configPath)) {
    Write-Host "[hash-context] config not found: $configPath"
    return
  }

  $originalText = Get-Content -Raw -Path $configPath
  $repairedText = Repair-ProjectTables -Text $originalText
  $normalizedOriginal = $originalText.TrimEnd() + "`r`n"

  if ($repairedText -eq $normalizedOriginal) {
    Write-Host "[hash-context] config repair: no malformed projects tables found"
    return
  }

  Set-Content -Path $configPath -Value $repairedText -Encoding UTF8
  Write-Host "[hash-context] config repaired"
}

function Test-HttpOk {
  param([string] $Url)
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

function Get-ProjectPortOwners {
  param([int[]] $Ports)

  $owners = @()
  foreach ($port in $Ports) {
    $connections = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
      $ownerPid = [int] $connection.OwningProcess
      $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerPid" -ErrorAction SilentlyContinue
      if (-not $process) {
        continue
      }
      $commandLine = [string] $process.CommandLine
      if ($commandLine -and (
          $commandLine.Contains($projectRoot.Path) -or
          $commandLine -like "*proxy_fastapi.py*" -or
          $commandLine -like "*proxy_server.py*" -or
          $commandLine -like "*web_server.py*" -or
          $commandLine -like "*backend.proxy_fastapi*" -or
          $commandLine -like "*backend.proxy_server*" -or
          $commandLine -like "*backend.web_server*" -or
          $commandLine -like "*hash-proxy-server*" -or
          $commandLine -like "*hash-web-server*" -or
          $commandLine -like "*electron/context-window.cjs*" -or
          $commandLine -like "*react_app\vite.config.ts*"
        )) {
        $owners += [pscustomobject] @{
          Port = $port
          Pid = $ownerPid
          Name = [string] $process.Name
          CommandLine = $commandLine
        }
      }
    }
  }
  return $owners
}

function Stop-ProjectServicePorts {
  param([string] $Reason)

  $ports = @([int] $proxyPort, 8765, [int] $controlPort, 5174)
  $owners = Get-ProjectPortOwners -Ports $ports | Sort-Object Pid -Unique
  foreach ($owner in $owners) {
    try {
      & taskkill /pid $([int] $owner.Pid) /t /f | Out-Null
      Write-Host "[hash-context] stopped service pid=$($owner.Pid) port=$($owner.Port) reason=$Reason"
    } catch {
      Write-Host "[hash-context] could not stop service pid=$($owner.Pid): $($_.Exception.Message)" -ForegroundColor DarkYellow
    }
  }
  if ($owners.Count -gt 0) {
    Start-Sleep -Milliseconds 800
  }
}

function Get-ProjectServiceProcesses {
  $projectPath = [string] $projectRoot.Path
  return @(
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
      Where-Object {
        $_.ProcessId -ne $PID -and $_.CommandLine
      } |
      Where-Object {
        $commandLine = [string] $_.CommandLine
        $inProject = $commandLine.Contains($projectPath)
        (
          $commandLine -like "*proxy_fastapi.py*" -or
          $commandLine -like "*proxy_server.py*" -or
          $commandLine -like "*web_server.py*" -or
          $commandLine -like "*backend.proxy_fastapi*" -or
          $commandLine -like "*backend.proxy_server*" -or
          $commandLine -like "*backend.web_server*" -or
          $commandLine -like "*hash-proxy-server*" -or
          $commandLine -like "*hash-web-server*" -or
          $commandLine -like "*electron/context-window.cjs*" -or
          $commandLine -like "*electron\context-window.cjs*" -or
          $commandLine -like "*Codex Context Proxy.exe*" -or
          $commandLine -like "*hashcode.exe*" -or
          ($inProject -and (
              $commandLine -like "*react_app\vite.config.ts*" -or
              $commandLine -like "*react_app/vite.config.ts*" -or
              $commandLine -like "*node_modules*electron*" -or
              $commandLine -like "*node_modules*vite*" -or
              $commandLine -like "*--app-path*$projectPath*electron*" -or
              $commandLine -like "*--user-data-dir*hash-context-codex-lab*"
            ))
        )
      } |
      Sort-Object ProcessId -Descending
  )
}

function Stop-ProjectServiceProcesses {
  param([string] $Reason)

  $targets = Get-ProjectServiceProcesses | Sort-Object ProcessId -Unique
  foreach ($target in $targets) {
    try {
      & taskkill /pid $([int] $target.ProcessId) /t /f | Out-Null
      Write-Host "[hash-context] stopped project service pid=$($target.ProcessId) name=$($target.Name) reason=$Reason"
    } catch {
      Write-Host "[hash-context] could not stop project service pid=$($target.ProcessId): $($_.Exception.Message)" -ForegroundColor DarkYellow
    }
  }
  if ($targets.Count -gt 0) {
    Start-Sleep -Milliseconds 800
  }
}

function Test-SourceProxyRunning {
  $owners = Get-ProjectPortOwners -Ports @([int] $proxyPort)
  foreach ($owner in $owners) {
    if ($owner.CommandLine -like "*proxy_fastapi.py*" -or
        $owner.CommandLine -like "*proxy_server.py*" -or
        $owner.CommandLine -like "*backend.proxy_fastapi*" -or
        $owner.CommandLine -like "*backend.proxy_server*") {
      return $true
    }
  }
  return $false
}

function Get-PackagedWindowExe {
  $installRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot.Path "..\.."))
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

  $logDir = Join-Path $projectRoot.Path "logs"
  New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  return Start-Process `
    -FilePath "npm.cmd" `
    -ArgumentList @("run", "window") `
    -WorkingDirectory $projectRoot.Path `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "electron-window.stdout.log") `
    -RedirectStandardError (Join-Path $logDir "electron-window.stderr.log") `
    -PassThru
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

function Stop-CodexProcesses {
  param([string] $Reason)

  $targets = @(
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
      Where-Object {
        $_.ProcessId -ne $PID -and
        ($_.Name -ieq "Codex.exe" -or $_.Name -ieq "codex.exe")
      } |
      Sort-Object ProcessId -Descending
  )

  if (-not $targets -or $targets.Count -eq 0) {
    Write-Host "[hash-context] no Codex processes to stop before $Reason"
    return
  }

  foreach ($target in $targets) {
    try {
      Stop-Process -Id ([int] $target.ProcessId) -Force -ErrorAction Stop
      Write-Host "[hash-context] stopped Codex process pid=$($target.ProcessId) name=$($target.Name)"
    } catch {
      Write-Host "[hash-context] could not stop Codex process pid=$($target.ProcessId): $($_.Exception.Message)" -ForegroundColor DarkYellow
    }
  }

  Start-Sleep -Milliseconds 500
}

function Test-DesktopConfigInstalled {
  $state = Read-DesktopState
  if (-not $state -or -not [bool] $state.enabled) {
    return $false
  }
  if (-not (Test-Path $configPath)) {
    return $false
  }
  try {
    $text = Get-Content -Raw -Path $configPath
    return (
      $text.Contains('model_provider = "hash-context"') -and
      $text.Contains("hooks.UserPromptSubmit") -and
      $text.Contains("codex-context-hook.cmd") -and
      ($text.Contains("http://${loopbackHost}:$proxyPort/v1") -or $text.Contains("http://localhost:$proxyPort/v1") -or $text.Contains("http://127.0.0.1:$proxyPort/v1"))
    )
  } catch {
    return $false
  }
}

function Start-DesktopServices {
  if ($env:HASH_CONTEXT_USE_BUNDLED_PYTHON -ne "1" -and
      (Test-TcpPortOpen -Port ([int] $proxyPort)) -and
      -not (Test-SourceProxyRunning)) {
    Stop-ProjectServicePorts -Reason "refresh source proxy"
  }

  if ((Test-HttpOk "http://${serviceProbeHost}:$proxyPort/api/proxy/health") -and
      (Test-HttpOk "http://${serviceProbeHost}:8765/api/health") -and
      (Test-HttpOk "http://${loopbackHost}:$controlPort/health")) {
    Write-Host "[hash-context] desktop services already running"
    return 0
  }

  Stop-ProjectServicePorts -Reason "refresh incomplete desktop services"

  $previousStartHidden = $env:HASH_CONTEXT_START_HIDDEN
  $previousControlPort = $env:HASH_CONTEXT_CONTROL_PORT
  $previousHost = $env:HASH_CONTEXT_HOST
  $previousPreferSource = $env:HASH_CONTEXT_PREFER_SOURCE_SERVERS
  $env:HASH_CONTEXT_START_HIDDEN = "1"
  $env:HASH_CONTEXT_CONTROL_PORT = $controlPort
  $env:HASH_CONTEXT_HOST = $loopbackHost
  if ($null -eq $previousPreferSource -and $env:HASH_CONTEXT_USE_BUNDLED_PYTHON -ne "1") {
    $env:HASH_CONTEXT_PREFER_SOURCE_SERVERS = "1"
  }
  $process = Start-ContextWindow
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
  if ($null -eq $previousHost) {
    Remove-Item Env:\HASH_CONTEXT_HOST -ErrorAction SilentlyContinue
  } else {
    $env:HASH_CONTEXT_HOST = $previousHost
  }
  if ($null -eq $previousPreferSource) {
    Remove-Item Env:\HASH_CONTEXT_PREFER_SOURCE_SERVERS -ErrorAction SilentlyContinue
  } else {
    $env:HASH_CONTEXT_PREFER_SOURCE_SERVERS = $previousPreferSource
  }

  Wait-HttpOk -Name "proxy" -Url "http://${serviceProbeHost}:$proxyPort/api/proxy/health" -TimeoutSeconds 30
  Wait-HttpOk -Name "backend" -Url "http://${serviceProbeHost}:8765/api/health" -TimeoutSeconds 30
  Wait-HttpOk -Name "window-control" -Url "http://${loopbackHost}:$controlPort/health" -TimeoutSeconds 90
  return $process.Id
}

function Update-ServicePid {
  param([int] $ServiceProcessId)
  $state = Read-DesktopState
  if (-not $state) {
    return
  }
  Save-DesktopState @{
    version = 2
    enabled = [bool] $state.enabled
    mode = [string] $state.mode
    config_path = [string] $state.config_path
    had_config = [bool] $state.had_config
    service_pid = $ServiceProcessId
    project_root = [string] $state.project_root
    proxy_port = [string] $state.proxy_port
    control_port = [string] $state.control_port
    data_dir = if ($state.data_dir) { [string] $state.data_dir } else { $desktopDataDir }
    session_count_before = [int] $state.session_count_before
    proxy_log_length_before = [int64] $state.proxy_log_length_before
    updated_at = (Get-Date).ToUniversalTime().ToString("o")
    original_provider = [string] $state.original_provider
    upstream_kind = if ($state.upstream_kind) { [string] $state.upstream_kind } else { "" }
    upstream_base_url = if ($state.upstream_base_url) { [string] $state.upstream_base_url } else { "" }
    upstream_api_key = if ($state.upstream_api_key) { [string] $state.upstream_api_key } else { "" }
    upstream_provider_id = if ($state.upstream_provider_id) { [string] $state.upstream_provider_id } else { "" }
  }
}

function Get-ResidualServicePorts {
  $ports = @([int] $proxyPort, 8765, [int] $controlPort, 5174)
  $open = @()
  foreach ($port in $ports) {
    if (Test-TcpPortOpen -Port $port) {
      $open += $port
    }
  }
  return $open
}

function Stop-DesktopServices {
  $state = Read-DesktopState
  if ($state -and $state.service_pid -and [int] $state.service_pid -gt 0) {
    $pidToStop = [int] $state.service_pid
    $process = Get-Process -Id $pidToStop -ErrorAction SilentlyContinue
    if ($process) {
      & taskkill /pid $pidToStop /t /f | Out-Null
      Write-Host "[hash-context] stopped desktop services pid=$pidToStop"
    }
  }

  # The saved service_pid only covers the Electron launcher, and when the
  # state file is missing (or the launcher was started from a different
  # terminal) it covers nothing. Kill by listening port + command-line
  # signature, then keep retrying until the ports are actually free so we
  # never leave orphaned proxy/backend/vite processes behind.
  $maxAttempts = 5
  for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    Stop-ProjectServicePorts -Reason "desktop services stop (attempt $attempt)"
    Stop-ProjectServiceProcesses -Reason "desktop services stop (attempt $attempt)"

    $residual = Get-ResidualServicePorts
    if ($residual.Count -eq 0) {
      if ($attempt -gt 1) {
        Write-Host "[hash-context] all desktop service ports cleared after $attempt attempt(s)"
      }
      return
    }

    Write-Host "[hash-context] ports still listening after attempt ${attempt}: $($residual -join ', '); retrying" -ForegroundColor DarkYellow
    Start-Sleep -Milliseconds 700
  }

  $stillOpen = Get-ResidualServicePorts
  if ($stillOpen.Count -gt 0) {
    Write-Host "[hash-context] WARNING: ports still listening after $maxAttempts attempts: $($stillOpen -join ', '). Run 'codex ctx desktop status' to inspect." -ForegroundColor Red
  }
}

function Stop-DesktopProxy {
  param([string] $Reason)

  Stop-CodexProcesses -Reason $Reason
  Restore-DesktopConfig
  Stop-DesktopServices
  Remove-Item Env:\HASH_CONTEXT_FORCE_UPSTREAM_BASE_URL -ErrorAction SilentlyContinue
  Remove-Item Env:\HASH_CONTEXT_FORCE_UPSTREAM_API_KEY -ErrorAction SilentlyContinue
  if (Test-Path $statePath) {
    Remove-Item -Path $statePath -Force
  }
}

function Show-DesktopStatus {
  $state = Read-DesktopState
  $snapshot = Get-ProxySnapshot
  $enabled = ($state -and [bool] $state.enabled)
  $beforeSessions = if ($state) { [int] $state.session_count_before } else { $snapshot.session_count }
  $beforeLog = if ($state) { [int64] $state.proxy_log_length_before } else { $snapshot.proxy_log_length }
  Write-Host "[hash-context] desktop proxy: $(if ($enabled) { 'on' } else { 'off' })"
  Write-Host "[hash-context] config: $configPath"
  Write-Host "[hash-context] config installed: $(if (Test-DesktopConfigInstalled) { 'yes' } else { 'no' })"
  if ($state -and $state.upstream_kind) {
    Write-Host "[hash-context] upstream: $($state.upstream_kind) -> $($state.upstream_base_url)"
  }
  if (Test-Path $configPath) {
    $configText = Get-Content -Raw -Path $configPath
    if ($configText.Contains('features.codex_hooks = true')) {
      Write-Host "[hash-context] config warning: features.codex_hooks is deprecated; use features.hooks" -ForegroundColor DarkYellow
    }
  }
  Write-Host "[hash-context] services proxy: $(if (Test-HttpOk "http://${serviceProbeHost}:$proxyPort/api/proxy/health") { 'ready' } else { 'not ready' })"
  Write-Host "[hash-context] services backend: $(if (Test-HttpOk "http://${serviceProbeHost}:8765/api/health") { 'ready' } else { 'not ready' })"
  Write-Host "[hash-context] services control: $(if (Test-HttpOk "http://${loopbackHost}:$controlPort/health") { 'ready' } else { 'not ready' })"
  Write-Host "[hash-context] data dir: $($snapshot.data_dir)"
  Write-Host "[hash-context] sessions before/current: $beforeSessions/$($snapshot.session_count)"
  Write-Host "[hash-context] proxy log bytes before/current: $beforeLog/$($snapshot.proxy_log_length)"
  if ($snapshot.session_count -gt $beforeSessions -or $snapshot.proxy_log_length -gt $beforeLog) {
    Write-Host "[hash-context] probe signal: proxy activity increased" -ForegroundColor Green
  } elseif ($enabled) {
    Write-Host "[hash-context] probe signal: no desktop request observed yet; open a fresh desktop chat and send a short message"
  }
}

switch ($Command) {
  "probe" {
    $upstreamInfo = Test-CodexUpstreamInfo -ConfigPath $configPath
    $requiresAuth = ($upstreamInfo.kind -ne "third_party")
    Set-DesktopConfigEnabled -Mode "probe" -RequiresOpenAiAuth $requiresAuth -UpstreamInfo $upstreamInfo
    if ($upstreamInfo.kind -eq "third_party") {
      $env:HASH_CONTEXT_FORCE_UPSTREAM_BASE_URL = $upstreamInfo.effective_base_url
      $env:HASH_CONTEXT_FORCE_UPSTREAM_API_KEY = $upstreamInfo.api_key
    }
    $serviceProcessId = Start-DesktopServices
    Update-ServicePid -ServiceProcessId $serviceProcessId
    if ($upstreamInfo.kind -eq "third_party") {
      Write-Host "[hash-context] upstream: $($upstreamInfo.effective_base_url) (third-party)" -ForegroundColor Cyan
    }
    Write-Host "[hash-context] desktop probe is armed"
    Write-Host "[hash-context] keep this desktop app open; use a fresh chat for testing, then run: codex ctx desktop status"
    Write-Host "[hash-context] if Codex says hooks need review, run /hooks and approve HashContext once"
    Write-Host "[hash-context] restore with: codex ctx desktop off"
    break
  }
  "on" {
    Stop-DesktopProxy -Reason "desktop proxy on reset"
    $upstreamInfo = Test-CodexUpstreamInfo -ConfigPath $configPath
    $requiresAuth = ($upstreamInfo.kind -ne "third_party")
    Set-DesktopConfigEnabled -Mode "on" -RequiresOpenAiAuth $requiresAuth -UpstreamInfo $upstreamInfo
    if ($upstreamInfo.kind -eq "third_party") {
      $env:HASH_CONTEXT_FORCE_UPSTREAM_BASE_URL = $upstreamInfo.effective_base_url
      $env:HASH_CONTEXT_FORCE_UPSTREAM_API_KEY = $upstreamInfo.api_key
    }
    $serviceProcessId = Start-DesktopServices
    Update-ServicePid -ServiceProcessId $serviceProcessId
    if ($upstreamInfo.kind -eq "third_party") {
      Write-Host "[hash-context] upstream: $($upstreamInfo.effective_base_url) (third-party)" -ForegroundColor Cyan
    }
    Write-Host "[hash-context] desktop proxy on"
    Write-Host "[hash-context] keep this desktop app open; use a fresh chat for testing"
    Write-Host "[hash-context] if Codex says hooks need review, run /hooks and approve HashContext once"
    break
  }
  "off" {
    Stop-DesktopProxy -Reason "desktop proxy off"
    Write-Host "[hash-context] desktop proxy off"
    break
  }
  "status" {
    Show-DesktopStatus
    break
  }
  "repair" {
    Repair-DesktopConfig
    break
  }
  default {
    Write-Host "Usage: codex ctx desktop <probe|on|off|status|repair>" -ForegroundColor Red
    exit 2
  }
}
