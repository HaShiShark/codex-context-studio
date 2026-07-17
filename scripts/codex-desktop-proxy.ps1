param(
  [Parameter(Position = 0)]
  [string] $Command = "status"
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$codexHome = Join-Path $env:USERPROFILE ".codex"
$studioRoot = if ($env:CODEX_CONTEXT_STUDIO_ROOT) { $env:CODEX_CONTEXT_STUDIO_ROOT } else { Join-Path $env:USERPROFILE ".codex-context-studio" }
$profile = if ($env:CODEX_CONTEXT_STUDIO_PROFILE -eq "development") { "development" } else { "production" }
$isDevelopment = ($profile -eq "development")
$profileRoot = Join-Path $studioRoot $profile
$providerId = "codex-context-studio"
$profileLabel = if ($isDevelopment) { "development" } else { "production" }
$hookName = if ($isDevelopment) { "codex-context-studio-hook-dev" } else { "codex-context-studio-hook" }
$notifyName = if ($isDevelopment) { "codex-context-studio-notify-dev" } else { "codex-context-studio-notify" }
$configPath = if ($env:CODEX_CONTEXT_STUDIO_DESKTOP_CONFIG) { $env:CODEX_CONTEXT_STUDIO_DESKTOP_CONFIG } else { Join-Path $codexHome "config.toml" }
$stateDir = if ($env:CODEX_CONTEXT_STUDIO_DESKTOP_STATE_DIR) { $env:CODEX_CONTEXT_STUDIO_DESKTOP_STATE_DIR } else { Join-Path $profileRoot "state" }
$shimDir = if ($env:CODEX_CONTEXT_STUDIO_SHIM_DIR) { $env:CODEX_CONTEXT_STUDIO_SHIM_DIR } else { Join-Path $profileRoot "bin" }
$statePath = Join-Path $stateDir "desktop.json"
$proxyPort = if ($env:CODEX_CONTEXT_STUDIO_PROXY_PORT) { $env:CODEX_CONTEXT_STUDIO_PROXY_PORT } else { "8787" }
$controlPort = if ($env:CODEX_CONTEXT_STUDIO_CONTROL_PORT) { $env:CODEX_CONTEXT_STUDIO_CONTROL_PORT } else { "8790" }
$backendPort = if ($env:CODEX_CONTEXT_STUDIO_WEB_PORT) { $env:CODEX_CONTEXT_STUDIO_WEB_PORT } else { "8765" }
$frontendPort = if ($env:CODEX_CONTEXT_STUDIO_FRONTEND_PORT) { $env:CODEX_CONTEXT_STUDIO_FRONTEND_PORT } else { "5174" }
$loopbackHost = if ($env:CODEX_CONTEXT_STUDIO_HOST) { $env:CODEX_CONTEXT_STUDIO_HOST } else { "localhost" }
$serviceProbeHost = if ($loopbackHost -eq "localhost") { "127.0.0.1" } else { $loopbackHost }
$runtimeDataDir = if ($env:CODEX_CONTEXT_STUDIO_DESKTOP_DATA_DIR) { $env:CODEX_CONTEXT_STUDIO_DESKTOP_DATA_DIR } else { Join-Path $studioRoot "shared" }
$env:CODEX_CONTEXT_STUDIO_PROFILE = $profile
$env:CODEX_CONTEXT_STUDIO_ROOT = $studioRoot
$env:CODEX_CONTEXT_STUDIO_DATA_DIR = $runtimeDataDir

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

function Write-DesktopCommandShims {
  New-Item -ItemType Directory -Force -Path $shimDir | Out-Null
  Set-Content -Path (Join-Path $shimDir "current-project-root.txt") -Value $projectRoot.Path -Encoding UTF8

  $escapedRoot = $projectRoot.Path.Replace("'", "''")
  $escapedStudioRoot = $studioRoot.Replace("'", "''")
  $escapedRuntimeDataDir = $runtimeDataDir.Replace("'", "''")

  $hookPsShim = "`$ErrorActionPreference = `"Stop`"`r`n`$env:CODEX_CONTEXT_STUDIO_PROFILE = '$profile'`r`n`$env:CODEX_CONTEXT_STUDIO_ROOT = '$escapedStudioRoot'`r`n`$env:CODEX_CONTEXT_STUDIO_DATA_DIR = '$escapedRuntimeDataDir'`r`n& '$escapedRoot\scripts\codex-context-hook.ps1'`r`nexit `$LASTEXITCODE`r`n"
  Set-Content -Path (Join-Path $shimDir "$hookName.ps1") -Value $hookPsShim -Encoding UTF8
  Set-Content -Path (Join-Path $shimDir "$hookName.cmd") -Value "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0$hookName.ps1`"`r`nexit /b %ERRORLEVEL%`r`n" -Encoding ASCII

  $notifyPsShim = "param([Parameter(ValueFromRemainingArguments = `$true)][string[]] `$ForwardArgs)`r`n`$ErrorActionPreference = `"Stop`"`r`n`$env:CODEX_CONTEXT_STUDIO_PROFILE = '$profile'`r`n`$env:CODEX_CONTEXT_STUDIO_ROOT = '$escapedStudioRoot'`r`n`$env:CODEX_CONTEXT_STUDIO_DATA_DIR = '$escapedRuntimeDataDir'`r`n& '$escapedRoot\scripts\codex-turn-ended-notify.ps1' @ForwardArgs`r`nexit `$LASTEXITCODE`r`n"
  Set-Content -Path (Join-Path $shimDir "$notifyName.ps1") -Value $notifyPsShim -Encoding UTF8
  Set-Content -Path (Join-Path $shimDir "$notifyName.cmd") -Value "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0$notifyName.ps1`" %*`r`nexit /b %ERRORLEVEL%`r`n" -Encoding ASCII

  return @{
    hook_cmd = Join-Path $shimDir "$hookName.cmd"
    notify_cmd = Join-Path $shimDir "$notifyName.cmd"
  }
}

function Get-ProxySnapshot {
  $sessionCount = 0
  $activeSessionId = ""
  $dataDir = $runtimeDataDir

  $indexPath = Join-Path $dataDir "index.json"
  if (Test-Path $indexPath) {
    try {
      $index = Get-Content -Raw -Path $indexPath -Encoding UTF8 | ConvertFrom-Json
      if ($index.active_session_id) {
        $activeSessionId = [string] $index.active_session_id
      }
      if ($index.sessions) {
        $sessionCount = @($index.sessions).Count
      }
    } catch {
    }
  }

  $logPath = Join-Path $dataDir "proxy.log"
  $logLength = 0
  if (Test-Path $logPath) {
    $logLength = (Get-Item $logPath).Length
    try {
      $sessionIds = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
      $logActiveSessionId = ""
      foreach ($line in Get-Content -Path $logPath -ErrorAction Stop) {
        $match = [regex]::Match($line, "request session=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")
        if ($match.Success) {
          $logActiveSessionId = $match.Groups[1].Value.ToLowerInvariant()
          [void] $sessionIds.Add($logActiveSessionId)
        }
      }
      if (-not $activeSessionId) {
        $activeSessionId = $logActiveSessionId
      }
      if ($sessionCount -eq 0) {
        $sessionCount = $sessionIds.Count
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
    Write-Host "[codex-context-studio] removed malformed projects tables: $removedCount" -ForegroundColor DarkYellow
  }

  return (($kept -join "`r`n").TrimEnd() + "`r`n")
}

function ConvertTo-TomlBasicString {
  param([string] $Value)
  return '"' + $Value.Replace("\", "\\").Replace('"', '\"') + '"'
}

function ConvertFrom-TomlInlineStringArray {
  param(
    [string] $Text,
    [string] $Key
  )

  $match = [regex]::Match($Text, "(?m)^\s*$([regex]::Escape($Key))\s*=\s*\[(?<body>[^\r\n]*)\]")
  if (-not $match.Success) {
    return @()
  }

  $values = @()
  $body = $match.Groups["body"].Value
  $stringMatches = [regex]::Matches($body, '"((?:\\.|[^"\\])*)"|''([^'']*)''')
  foreach ($stringMatch in $stringMatches) {
    if ($stringMatch.Groups[1].Success) {
      $values += ($stringMatch.Groups[1].Value.Replace('\"', '"').Replace('\\', '\'))
    } elseif ($stringMatch.Groups[2].Success) {
      $values += $stringMatch.Groups[2].Value
    }
  }
  return $values
}

function ConvertTo-TomlInlineStringArray {
  param([string[]] $Values)
  $quoted = @()
  foreach ($value in $Values) {
    $quoted += (ConvertTo-TomlBasicString $value)
  }
  return "[ " + ($quoted -join ", ") + " ]"
}

function Test-CodexContextStudioNotifyCommand {
  param([string] $Value)
  if (-not $Value) {
    return $false
  }
  return (
    [string] $Value -match '(?i)(?:^|[\\/])codex-context-studio-notify(?:-dev)?\.(?:cmd|ps1)$' -or
    [string] $Value -like "*codex-turn-ended-notify.cmd" -or
    [string] $Value -like "*codex-turn-ended-notify.ps1"
  )
}

function Normalize-OriginalNotifyArgs {
  param([string[]] $NotifyArgs)
  $args = @($NotifyArgs | Where-Object { $null -ne $_ -and ([string] $_).Trim() })
  if (-not $args -or $args.Count -eq 0) {
    return @()
  }

  while ($args.Count -gt 0 -and (Test-CodexContextStudioNotifyCommand -Value ([string] $args[0]))) {
    if ($args.Count -le 1) {
      return @()
    }
    $args = @($args[1..($args.Count - 1)])
  }

  $clean = @()
  for ($i = 0; $i -lt $args.Count; $i++) {
    $arg = [string] $args[$i]
    if ($arg -eq "--previous-notify") {
      if (($i + 1) -lt $args.Count) {
        $i += 1
      }
      continue
    }
    if (Test-CodexContextStudioNotifyCommand -Value $arg) {
      continue
    }
    $clean += $arg
  }

  return @($clean)
}

function Remove-DesktopManagedConfig {
  param(
    [string] $Text,
    [bool] $RemoveContextWindow = $false
  )

  $clean = $Text
  $clean = [regex]::Replace($clean, '(?m)^\s*hooks\.UserPromptSubmit\s*=.*\r?\n?', '')
  $clean = [regex]::Replace($clean, '(?m)^\s*notify\s*=\s*\[[^\r\n]*\]\s*\r?\n?', '')
  $escapedProviderId = [regex]::Escape($providerId)
  $clean = [regex]::Replace($clean, "(?ms)(?:^|\r?\n)\s*\[model_providers\.$escapedProviderId\]\s*\r?\n.*?(?=(?:\r?\n\s*\[)|\z)", "`r`n")
  if ($RemoveContextWindow) {
    $clean = [regex]::Replace($clean, '(?m)^\s*model_context_window\s*=\s*\d+\s*\r?\n?', '')
  }
  return ($clean.TrimEnd() + "`r`n")
}

function Test-CodexUpstreamInfo {
  param([string] $ConfigPath)

  if (-not (Test-Path $ConfigPath)) {
    Write-Host "[codex-context-studio] Codex config not found: $ConfigPath" -ForegroundColor Red
    Write-Host "[codex-context-studio] Please run 'codex' first to login (codex login) or configure a third-party API provider." -ForegroundColor Red
    throw "Codex config file not found."
  }

  $content = Get-Content -Raw -Path $ConfigPath -ErrorAction Stop
  if (-not $content.Trim()) {
    Write-Host "[codex-context-studio] Codex config is empty: $ConfigPath" -ForegroundColor Red
    Write-Host "[codex-context-studio] Please run 'codex' first to login (codex login) or configure a third-party API provider." -ForegroundColor Red
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

  if (-not [string]::Equals($modelProvider, "openai", [System.StringComparison]::Ordinal)) {
    $escapedId = [regex]::Escape($modelProvider)
    $sectionPattern = "\[model_providers\.$escapedId\][\s\S]*?(?=\[\s*model_providers\.|\z)"
    $sectionMatch = [regex]::Match($content, $sectionPattern)
    if (-not $sectionMatch.Success) {
      Write-Host "[codex-context-studio] model_provider '$modelProvider' has no [model_providers.$modelProvider] section." -ForegroundColor Red
      Write-Host "[codex-context-studio] Please configure [model_providers.$modelProvider] in your config or switch to a valid provider." -ForegroundColor Red
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
    Write-Host "[codex-context-studio] $errorMessage" -ForegroundColor Red
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
  if ($originalProvider -eq $providerId) {
    $originalProvider = if ($existingState -and $existingState.original_provider) {
      [string] $existingState.original_provider
    } else {
      ""
    }
  }
  $originalNotifyArgs = Normalize-OriginalNotifyArgs -NotifyArgs (ConvertFrom-TomlInlineStringArray -Text $text -Key "notify")

  $text = Remove-DesktopManagedConfig -Text $text -RemoveContextWindow:(-not $RequiresOpenAiAuth)
  $text = $text -replace '(?m)^\s*model_provider\s*=\s*"[^"]*"\s*\r?\n?', ''

  $commandShims = Write-DesktopCommandShims
  $hookPath = ([string] $commandShims.hook_cmd).Replace("\", "/")
  $hookCommand = ConvertTo-TomlBasicString $hookPath
  $notifyPath = ([string] $commandShims.notify_cmd).Replace("\", "/")
  $notifyArgs = @($notifyPath) + @($originalNotifyArgs)
  $notifyConfig = ConvertTo-TomlInlineStringArray -Values $notifyArgs

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
[model_providers.$providerId]
name = "Codex Context Studio"
base_url = "http://${loopbackHost}:$proxyPort/v1"
requires_openai_auth = $requiresAuth
wire_api = "responses"
supports_websockets = false
"@

  $hookBlock = @"
hooks.UserPromptSubmit = [{ matcher = "*", hooks = [{ type = "command", command = $hookCommand, timeout = 10, statusMessage = "CodexContextStudio" }] }]
notify = $notifyConfig
"@

  $text = $text.TrimEnd()
  $header = "model_provider = `"$providerId`"`r`n" + $contextWindowLine + $hookBlock + "`r`n"
  $text = $header + $text.TrimEnd() + "`r`n`r`n" + $providerBlock + "`r`n"
  Set-Content -Path $configPath -Value $text -Encoding UTF8

  $snapshot = Get-ProxySnapshot
  $stateData = @{
    version = 2
    enabled = $true
    mode = $Mode
    profile = $profile
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
    original_notify_args = @($originalNotifyArgs)
  }
  Save-DesktopState $stateData
}

function Restore-DesktopConfig {
  $state = Read-DesktopState
  if (-not $state) {
    Write-Host "[codex-context-studio] no proxy state to restore" -ForegroundColor DarkYellow
    Repair-DesktopConfig
    return
  }

  if (-not (Test-Path $configPath)) {
    Write-Host "[codex-context-studio] config missing, nothing to restore"
    return
  }

  $text = Repair-ProjectTables -Text (Get-Content -Raw -Path $configPath)
  $currentNotifyArgs = Normalize-OriginalNotifyArgs -NotifyArgs (ConvertFrom-TomlInlineStringArray -Text $text -Key "notify")
  $originalNotifyArgs = @()
  if ($state.PSObject.Properties.Name -contains "original_notify_args") {
    $originalNotifyArgs = @($state.original_notify_args)
  } elseif ($currentNotifyArgs.Count -gt 0) {
    $originalNotifyArgs = @($currentNotifyArgs)
  }

  $text = Remove-DesktopManagedConfig -Text $text -RemoveContextWindow:($state.upstream_kind -eq "third_party")
  $text = $text -replace "(?m)^\s*model_provider\s*=\s*`"$([regex]::Escape($providerId))`"\s*\r?\n?", ''

  $text = $text.Trim()
  if ($text -and $text -notmatch '(?m)^\s*model_provider\s*=' -and $state.original_provider) {
    $providerId = [string] $state.original_provider
    $text = "model_provider = `"$providerId`"`r`n`r`n" + $text
  }

  if ($originalNotifyArgs.Count -gt 0) {
    $notifyConfig = ConvertTo-TomlInlineStringArray -Values @($originalNotifyArgs)
    $text = "notify = $notifyConfig`r`n" + $text.TrimStart()
  }

  if ($text.Trim()) {
    Set-Content -Path $configPath -Value ($text.TrimEnd() + "`r`n") -Encoding UTF8
    Write-Host "[codex-context-studio] restored config"
  } else {
    Remove-Item -Path $configPath -Force -ErrorAction SilentlyContinue
    Write-Host "[codex-context-studio] removed empty config"
  }
}

function Repair-DesktopConfig {
  if (-not (Test-Path $configPath)) {
    Write-Host "[codex-context-studio] config not found: $configPath"
    return
  }

  $originalText = Get-Content -Raw -Path $configPath
  $repairedText = Repair-ProjectTables -Text $originalText
  $notifyArgs = Normalize-OriginalNotifyArgs -NotifyArgs (ConvertFrom-TomlInlineStringArray -Text $repairedText -Key "notify")
  $repairedText = [regex]::Replace($repairedText, '(?m)^\s*notify\s*=\s*\[[^\r\n]*\]\s*\r?\n?', '')
  if ($notifyArgs.Count -gt 0) {
    $notifyConfig = ConvertTo-TomlInlineStringArray -Values @($notifyArgs)
    $repairedText = "notify = $notifyConfig`r`n" + $repairedText.TrimStart()
  }
  $normalizedOriginal = $originalText.TrimEnd() + "`r`n"

  if ($repairedText -eq $normalizedOriginal) {
    Write-Host "[codex-context-studio] config repair: no changes needed"
    return
  }

  Set-Content -Path $configPath -Value $repairedText -Encoding UTF8
  Write-Host "[codex-context-studio] config repaired"
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
          $commandLine -like "*web_server.py*" -or
          $commandLine -like "*backend.proxy_fastapi*" -or
          $commandLine -like "*backend.web_server*" -or
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

  $ports = @([int] $proxyPort, [int] $backendPort, [int] $controlPort, [int] $frontendPort)
  $owners = Get-ProjectPortOwners -Ports $ports | Sort-Object Pid -Unique
  foreach ($owner in $owners) {
    try {
      & taskkill /pid $([int] $owner.Pid) /t /f | Out-Null
      Write-Host "[codex-context-studio] stopped service pid=$($owner.Pid) port=$($owner.Port) reason=$Reason"
    } catch {
      Write-Host "[codex-context-studio] could not stop service pid=$($owner.Pid): $($_.Exception.Message)" -ForegroundColor DarkYellow
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
          $commandLine -like "*web_server.py*" -or
          $commandLine -like "*backend.proxy_fastapi*" -or
          $commandLine -like "*backend.web_server*" -or
          $commandLine -like "*electron/context-window.cjs*" -or
          $commandLine -like "*electron\context-window.cjs*" -or
          $commandLine -like "*Codex Context Studio.exe*" -or
          $commandLine -like "*codex-context-studio.exe*" -or
          ($inProject -and (
              $commandLine -like "*react_app\vite.config.ts*" -or
              $commandLine -like "*react_app/vite.config.ts*" -or
              $commandLine -like "*node_modules*electron*" -or
              $commandLine -like "*node_modules*vite*" -or
              $commandLine -like "*--app-path*$projectPath*electron*" -or
              $commandLine -like "*--user-data-dir*codex-context-studio*"
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
      Write-Host "[codex-context-studio] stopped project service pid=$($target.ProcessId) name=$($target.Name) reason=$Reason"
    } catch {
      Write-Host "[codex-context-studio] could not stop project service pid=$($target.ProcessId): $($_.Exception.Message)" -ForegroundColor DarkYellow
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
        $owner.CommandLine -like "*backend.proxy_fastapi*") {
      return $true
    }
  }
  return $false
}

function Get-PackagedWindowExe {
  if ($isDevelopment) {
    return ""
  }
  $installRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot.Path "..\.."))
  $candidates = @(
    (Join-Path $installRoot "Codex Context Studio.exe"),
    (Join-Path $installRoot "codex-context-studio.exe")
  )
  foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
      return $candidate
    }
  }
  return ""
}

function Start-ContextWindow {
  $logDir = Join-Path $stateDir "logs"
  New-Item -ItemType Directory -Force -Path $logDir | Out-Null

  if (-not $isDevelopment) {
    $packagedExe = Get-PackagedWindowExe
    if (-not $packagedExe) {
      throw "Codex Context Studio production executable was not found for project root: $($projectRoot.Path)"
    }
    $env:CODEX_CONTEXT_STUDIO_USE_BUNDLED_PYTHON = "1"
    $env:CODEX_CONTEXT_STUDIO_USE_BUILT_FRONTEND = "1"
    return Start-Process `
      -FilePath $packagedExe `
      -WorkingDirectory (Split-Path -Parent $packagedExe) `
      -WindowStyle Hidden `
      -RedirectStandardOutput (Join-Path $logDir "electron-window.stdout.log") `
      -RedirectStandardError (Join-Path $logDir "electron-window.stderr.log") `
      -PassThru
  }

  $env:CODEX_CONTEXT_STUDIO_PREFER_SOURCE_SERVERS = "1"
  $env:CODEX_CONTEXT_STUDIO_USE_BUILT_FRONTEND = "0"
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

  $candidates = @(
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
      Where-Object {
        if ($_.ProcessId -eq $PID) {
          return $false
        }

        if ($_.Name -ieq "Codex.exe") {
          return $true
        }

        # The unified Codex desktop package now uses ChatGPT.exe for its UI
        # process. Restrict this match to the Codex package so a standalone
        # ChatGPT installation is never closed by this command.
        $executablePath = [string] $_.ExecutablePath
        return (
          $_.Name -ieq "ChatGPT.exe" -and
          $executablePath -match '[\\/]WindowsApps[\\/]OpenAI\.Codex_[^\\/]+[\\/]app[\\/]ChatGPT\.exe$'
        )
      }
  )

  if (-not $candidates -or $candidates.Count -eq 0) {
    Write-Host "[codex-context-studio] no Codex processes to stop before $Reason"
    return
  }

  $candidateIds = @($candidates | ForEach-Object { [int] $_.ProcessId })
  $targets = @(
    $candidates |
      Where-Object { $candidateIds -notcontains [int] $_.ParentProcessId } |
      Sort-Object ProcessId -Descending
  )

  foreach ($target in $targets) {
    try {
      & taskkill /pid $([int] $target.ProcessId) /t /f | Out-Null
      if ($LASTEXITCODE -ne 0 -and (Get-Process -Id ([int] $target.ProcessId) -ErrorAction SilentlyContinue)) {
        throw "taskkill exited with code $LASTEXITCODE"
      }
      Write-Host "[codex-context-studio] stopped Codex desktop tree pid=$($target.ProcessId) name=$($target.Name)"
    } catch {
      Write-Host "[codex-context-studio] could not stop Codex process pid=$($target.ProcessId): $($_.Exception.Message)" -ForegroundColor DarkYellow
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
      $text.Contains("model_provider = `"$providerId`"") -and
      $text.Contains("hooks.UserPromptSubmit") -and
      $text.Contains("$hookName.cmd") -and
      ($text.Contains("http://${loopbackHost}:$proxyPort/v1") -or $text.Contains("http://localhost:$proxyPort/v1") -or $text.Contains("http://127.0.0.1:$proxyPort/v1"))
    )
  } catch {
    return $false
  }
}

function Start-DesktopServices {
  if ($env:CODEX_CONTEXT_STUDIO_USE_BUNDLED_PYTHON -ne "1" -and
      (Test-TcpPortOpen -Port ([int] $proxyPort)) -and
      -not (Test-SourceProxyRunning)) {
    Stop-ProjectServicePorts -Reason "refresh source proxy"
  }

  $existingState = Read-DesktopState
  $existingPid = if ($existingState) { [int] $existingState.service_pid } else { 0 }
  $sameRuntime = (
    $existingState -and
    [string] $existingState.profile -eq $profile -and
    [string] $existingState.project_root -eq $projectRoot.Path -and
    $existingPid -gt 0 -and
    (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)
  )
  if ($sameRuntime -and
      (Test-HttpOk "http://${serviceProbeHost}:$proxyPort/api/proxy/health") -and
      (Test-HttpOk "http://${serviceProbeHost}:$backendPort/api/health") -and
      (Test-HttpOk "http://${loopbackHost}:$controlPort/health")) {
    Write-Host "[codex-context-studio] $profileLabel desktop services already running"
    return $existingPid
  }

  Stop-ProjectServicePorts -Reason "refresh incomplete desktop services"

  $previousStartHidden = $env:CODEX_CONTEXT_STUDIO_START_HIDDEN
  $previousControlPort = $env:CODEX_CONTEXT_STUDIO_CONTROL_PORT
  $previousHost = $env:CODEX_CONTEXT_STUDIO_HOST
  $previousPreferSource = $env:CODEX_CONTEXT_STUDIO_PREFER_SOURCE_SERVERS
  $env:CODEX_CONTEXT_STUDIO_START_HIDDEN = "1"
  $env:CODEX_CONTEXT_STUDIO_CONTROL_PORT = $controlPort
  $env:CODEX_CONTEXT_STUDIO_HOST = $loopbackHost
  if ($null -eq $previousPreferSource -and
      $env:CODEX_CONTEXT_STUDIO_USE_BUNDLED_PYTHON -ne "1" -and
      -not (Get-PackagedWindowExe)) {
    $env:CODEX_CONTEXT_STUDIO_PREFER_SOURCE_SERVERS = "1"
  }
  $process = Start-ContextWindow
  if ($null -eq $previousStartHidden) {
    Remove-Item Env:\CODEX_CONTEXT_STUDIO_START_HIDDEN -ErrorAction SilentlyContinue
  } else {
    $env:CODEX_CONTEXT_STUDIO_START_HIDDEN = $previousStartHidden
  }
  if ($null -eq $previousControlPort) {
    Remove-Item Env:\CODEX_CONTEXT_STUDIO_CONTROL_PORT -ErrorAction SilentlyContinue
  } else {
    $env:CODEX_CONTEXT_STUDIO_CONTROL_PORT = $previousControlPort
  }
  if ($null -eq $previousHost) {
    Remove-Item Env:\CODEX_CONTEXT_STUDIO_HOST -ErrorAction SilentlyContinue
  } else {
    $env:CODEX_CONTEXT_STUDIO_HOST = $previousHost
  }
  if ($null -eq $previousPreferSource) {
    Remove-Item Env:\CODEX_CONTEXT_STUDIO_PREFER_SOURCE_SERVERS -ErrorAction SilentlyContinue
  } else {
    $env:CODEX_CONTEXT_STUDIO_PREFER_SOURCE_SERVERS = $previousPreferSource
  }

  Wait-HttpOk -Name "proxy" -Url "http://${serviceProbeHost}:$proxyPort/api/proxy/health" -TimeoutSeconds 30
  Wait-HttpOk -Name "backend" -Url "http://${serviceProbeHost}:$backendPort/api/health" -TimeoutSeconds 30
  Wait-HttpOk -Name "window-control" -Url "http://${loopbackHost}:$controlPort/health" -TimeoutSeconds 90
  return $process.Id
}

function Invoke-ProxySessionPrune {
  $url = "http://${serviceProbeHost}:$proxyPort/api/proxy/sessions/prune-codex"
  try {
    $response = Invoke-RestMethod -Method Post -Uri $url -TimeoutSec 8
    $deleted = @($response.deleted_session_ids).Count
    $codexCount = if ($null -ne $response.codex_thread_count) { [int] $response.codex_thread_count } else { 0 }
    Write-Host "[codex-context-studio] proxy session prune: deleted=$deleted codex_threads=$codexCount"
  } catch {
    Write-Host "[codex-context-studio] proxy session prune skipped: $($_.Exception.Message)" -ForegroundColor DarkYellow
  }
}

function Update-ServicePid {
  param([int] $ServiceProcessId)
  $state = Read-DesktopState
  if (-not $state) {
    return
  }
  $originalNotifyArgs = @()
  if ($state.PSObject.Properties.Name -contains "original_notify_args") {
    $originalNotifyArgs = @($state.original_notify_args)
  }
  Save-DesktopState @{
    version = 2
    enabled = [bool] $state.enabled
    mode = [string] $state.mode
    profile = $profile
    config_path = [string] $state.config_path
    had_config = [bool] $state.had_config
    service_pid = $ServiceProcessId
    project_root = [string] $state.project_root
    proxy_port = [string] $state.proxy_port
    control_port = [string] $state.control_port
    data_dir = if ($state.data_dir) { [string] $state.data_dir } else { $runtimeDataDir }
    session_count_before = [int] $state.session_count_before
    proxy_log_length_before = [int64] $state.proxy_log_length_before
    updated_at = (Get-Date).ToUniversalTime().ToString("o")
    original_provider = [string] $state.original_provider
    upstream_kind = if ($state.upstream_kind) { [string] $state.upstream_kind } else { "" }
    upstream_base_url = if ($state.upstream_base_url) { [string] $state.upstream_base_url } else { "" }
    upstream_api_key = if ($state.upstream_api_key) { [string] $state.upstream_api_key } else { "" }
    upstream_provider_id = if ($state.upstream_provider_id) { [string] $state.upstream_provider_id } else { "" }
    original_notify_args = @($originalNotifyArgs)
  }
}

function Get-ResidualServicePorts {
  $ports = @([int] $proxyPort, [int] $backendPort, [int] $controlPort, [int] $frontendPort)
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
      Write-Host "[codex-context-studio] stopped desktop services pid=$pidToStop"
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
        Write-Host "[codex-context-studio] all desktop service ports cleared after $attempt attempt(s)"
      }
      return
    }

    Write-Host "[codex-context-studio] ports still listening after attempt ${attempt}: $($residual -join ', '); retrying" -ForegroundColor DarkYellow
    Start-Sleep -Milliseconds 700
  }

  $stillOpen = Get-ResidualServicePorts
  if ($stillOpen.Count -gt 0) {
    Write-Host "[codex-context-studio] WARNING: ports still listening after $maxAttempts attempts: $($stillOpen -join ', '). Run 'codex ctx desktop status$profileSuffix' to inspect." -ForegroundColor Red
  }
}

function Stop-DesktopProxy {
  param([string] $Reason)

  Stop-CodexProcesses -Reason $Reason
  Restore-DesktopConfig
  Stop-DesktopServices
  Remove-Item Env:\CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_BASE_URL -ErrorAction SilentlyContinue
  Remove-Item Env:\CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_API_KEY -ErrorAction SilentlyContinue
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
  Write-Host "[codex-context-studio] profile: $profileLabel"
  Write-Host "[codex-context-studio] desktop proxy: $(if ($enabled) { 'on' } else { 'off' })"
  Write-Host "[codex-context-studio] project root: $($projectRoot.Path)"
  Write-Host "[codex-context-studio] config: $configPath"
  Write-Host "[codex-context-studio] config installed: $(if (Test-DesktopConfigInstalled) { 'yes' } else { 'no' })"
  if ($state -and $state.upstream_kind) {
    Write-Host "[codex-context-studio] upstream: $($state.upstream_kind) -> $($state.upstream_base_url)"
  }
  if (Test-Path $configPath) {
    $configText = Get-Content -Raw -Path $configPath
    if ($configText.Contains('features.codex_hooks = true')) {
      Write-Host "[codex-context-studio] config warning: features.codex_hooks is deprecated; use features.hooks" -ForegroundColor DarkYellow
    }
  }
  Write-Host "[codex-context-studio] services proxy: $(if (Test-HttpOk "http://${serviceProbeHost}:$proxyPort/api/proxy/health") { 'ready' } else { 'not ready' })"
  Write-Host "[codex-context-studio] services backend: $(if (Test-HttpOk "http://${serviceProbeHost}:$backendPort/api/health") { 'ready' } else { 'not ready' })"
  Write-Host "[codex-context-studio] services control: $(if (Test-HttpOk "http://${loopbackHost}:$controlPort/health") { 'ready' } else { 'not ready' })"
  Write-Host "[codex-context-studio] data dir: $($snapshot.data_dir)"
  Write-Host "[codex-context-studio] sessions before/current: $beforeSessions/$($snapshot.session_count)"
  Write-Host "[codex-context-studio] proxy log bytes before/current: $beforeLog/$($snapshot.proxy_log_length)"
  if ($snapshot.session_count -gt $beforeSessions -or $snapshot.proxy_log_length -gt $beforeLog) {
    Write-Host "[codex-context-studio] probe signal: proxy activity increased" -ForegroundColor Green
  } elseif ($enabled) {
    Write-Host "[codex-context-studio] probe signal: no desktop request observed yet; open a fresh desktop chat and send a short message"
  }
}

switch ($Command) {
  "probe" {
    $upstreamInfo = Test-CodexUpstreamInfo -ConfigPath $configPath
    $requiresAuth = ($upstreamInfo.kind -ne "third_party")
    Set-DesktopConfigEnabled -Mode "probe" -RequiresOpenAiAuth $requiresAuth -UpstreamInfo $upstreamInfo
    if ($upstreamInfo.kind -eq "third_party") {
      $env:CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_BASE_URL = $upstreamInfo.effective_base_url
      $env:CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_API_KEY = $upstreamInfo.api_key
    }
    $serviceProcessId = Start-DesktopServices
    Update-ServicePid -ServiceProcessId $serviceProcessId
    Invoke-ProxySessionPrune
    if ($upstreamInfo.kind -eq "third_party") {
      Write-Host "[codex-context-studio] upstream: $($upstreamInfo.effective_base_url) (third-party)" -ForegroundColor Cyan
    }
    Write-Host "[codex-context-studio] desktop probe is armed"
    Write-Host "[codex-context-studio] keep this desktop app open; use a fresh chat for testing, then run: codex ctx desktop status$profileSuffix"
    Write-Host "[codex-context-studio] if Codex says hooks need review, run /hooks and approve CodexContextStudio once"
    Write-Host "[codex-context-studio] restore with: codex ctx desktop off$profileSuffix"
    break
  }
  "on" {
    Stop-DesktopProxy -Reason "desktop proxy on reset"
    $upstreamInfo = Test-CodexUpstreamInfo -ConfigPath $configPath
    $requiresAuth = ($upstreamInfo.kind -ne "third_party")
    Set-DesktopConfigEnabled -Mode "on" -RequiresOpenAiAuth $requiresAuth -UpstreamInfo $upstreamInfo
    if ($upstreamInfo.kind -eq "third_party") {
      $env:CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_BASE_URL = $upstreamInfo.effective_base_url
      $env:CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_API_KEY = $upstreamInfo.api_key
    }
    $serviceProcessId = Start-DesktopServices
    Update-ServicePid -ServiceProcessId $serviceProcessId
    Invoke-ProxySessionPrune
    if ($upstreamInfo.kind -eq "third_party") {
      Write-Host "[codex-context-studio] upstream: $($upstreamInfo.effective_base_url) (third-party)" -ForegroundColor Cyan
    }
    Write-Host "[codex-context-studio] $profileLabel desktop proxy on"
    Write-Host "[codex-context-studio] keep this desktop app open; use a fresh chat for testing"
    Write-Host "[codex-context-studio] if Codex says hooks need review, run /hooks and approve CodexContextStudio once"
    break
  }
  "off" {
    Stop-DesktopProxy -Reason "desktop proxy off"
    Write-Host "[codex-context-studio] $profileLabel desktop proxy off"
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
    Write-Host "Usage: codex ctx desktop <probe|on|off|status|repair|uninstall>$profileSuffix" -ForegroundColor Red
    exit 2
  }
}
