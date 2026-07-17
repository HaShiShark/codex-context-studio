param(
  [Parameter(Position = 0)]
  [string] $Command = "status",

  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]] $Rest = @()
)

$ErrorActionPreference = "Stop"

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$defaultHome = Join-Path $env:USERPROFILE ".codex-context-studio"
$requestedDevelopment = (
  $env:CODEX_CONTEXT_STUDIO_PROFILE -eq "development" -or
  ($Rest.Count -gt 0 -and $Rest[-1] -eq "dev")
)
$profile = if ($requestedDevelopment) { "development" } else { "production" }
$profileSuffix = if ($requestedDevelopment) { " dev" } else { "" }
$profileRoot = Join-Path $defaultHome $profile
$registrationDir = Join-Path $defaultHome "registrations"
$registrationPath = Join-Path $registrationDir "$profile.json"
$providerId = "codex-context-studio"
$hookName = if ($requestedDevelopment) { "codex-context-studio-hook-dev" } else { "codex-context-studio-hook" }
$notifyName = if ($requestedDevelopment) { "codex-context-studio-notify-dev" } else { "codex-context-studio-notify" }
$commandShimDir = if ($env:CODEX_CONTEXT_STUDIO_COMMAND_SHIM_DIR) { $env:CODEX_CONTEXT_STUDIO_COMMAND_SHIM_DIR } else { Join-Path $defaultHome "bin" }
$shimDir = if ($env:CODEX_CONTEXT_STUDIO_SHIM_DIR) { $env:CODEX_CONTEXT_STUDIO_SHIM_DIR } else { Join-Path $profileRoot "bin" }
$statePath = if ($env:CODEX_CONTEXT_STUDIO_PROXY_SWITCH_STATE) { $env:CODEX_CONTEXT_STUDIO_PROXY_SWITCH_STATE } else { Join-Path $profileRoot "state\codex-ctx-proxy.json" }
$skipPathUpdate = ($env:CODEX_CONTEXT_STUDIO_SKIP_PATH_UPDATE -eq "1")
$proxyPort = if ($env:CODEX_CONTEXT_STUDIO_PROXY_PORT) { $env:CODEX_CONTEXT_STUDIO_PROXY_PORT } else { "8787" }
$loopbackHost = if ($env:CODEX_CONTEXT_STUDIO_HOST) { $env:CODEX_CONTEXT_STUDIO_HOST } else { "localhost" }
$env:CODEX_CONTEXT_STUDIO_PROFILE = $profile
$env:CODEX_CONTEXT_STUDIO_ROOT = $defaultHome

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

function Test-ManagedShimPath {
  param([string] $Path)

  if (-not $Path) {
    return $false
  }

  if ((Test-PathUnder -Path $Path -Parent $commandShimDir) -or
      (Test-PathUnder -Path $Path -Parent (Join-Path $defaultHome "production\bin")) -or
      (Test-PathUnder -Path $Path -Parent (Join-Path $defaultHome "development\bin"))) {
    return $true
  }

  try {
    if ((Test-Path $Path) -and -not (Get-Item -LiteralPath $Path).PSIsContainer) {
      $shimText = Get-Content -Raw -LiteralPath $Path -ErrorAction SilentlyContinue
      return ($shimText -match "codex-ctx-proxy\.ps1")
    }
  } catch {
    return $false
  }

  return $false
}

function Read-SwitchState {
  if (-not (Test-Path $statePath)) {
    return $null
  }
  try {
    return (Get-Content -Raw -Path $statePath | ConvertFrom-Json)
  } catch {
    Write-Host "[codex-context-studio] ignoring invalid switch state: $statePath" -ForegroundColor DarkYellow
    return $null
  }
}

function Save-SwitchState {
  param(
    [bool] $Enabled,
    [string] $RealCodex
  )
  $stateDir = Split-Path -Parent $statePath
  New-Item -ItemType Directory -Force -Path $stateDir | Out-Null
  $now = (Get-Date).ToUniversalTime().ToString("o")
  $existing = Read-SwitchState
  $createdAt = if ($existing -and $existing.created_at) { [string] $existing.created_at } else { $now }
  $payload = [ordered]@{
    version = 1
    enabled = $Enabled
    project_root = $projectRoot.Path
    real_codex = $RealCodex
    shim_dir = (ConvertTo-FullPath $shimDir)
    created_at = $createdAt
    updated_at = $now
  }
  $payload | ConvertTo-Json -Depth 4 | Set-Content -Path $statePath -Encoding UTF8
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

function Find-RealCodex {
  param([string] $Preferred)

  if ($Preferred -and (Test-Path $Preferred) -and -not (Test-ManagedShimPath -Path $Preferred)) {
    return (ConvertTo-FullPath $Preferred)
  }

  $commands = @(Get-Command codex -All -ErrorAction SilentlyContinue)
  foreach ($commandInfo in $commands) {
    $candidate = Get-CommandPath $commandInfo
    if (-not $candidate) {
      continue
    }
    if (Test-ManagedShimPath -Path $candidate) {
      continue
    }
    if (Test-Path $candidate) {
      return (ConvertTo-FullPath $candidate)
    }
  }

  throw "Could not find the real Codex command. Install the official Codex CLI first, then run this command again."
}

function Add-ShimDirToPath {
  if ($skipPathUpdate) {
    return
  }

  $fullShimDir = ConvertTo-FullPath $commandShimDir
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $entries = @()
  if ($userPath) {
    $entries = @($userPath -split ";" | Where-Object { $_ })
  }

  $exists = $false
  foreach ($entry in $entries) {
    if ((ConvertTo-FullPath $entry).Equals($fullShimDir, [System.StringComparison]::OrdinalIgnoreCase)) {
      $exists = $true
      break
    }
  }

  if (-not $exists) {
    $newPath = if ($userPath) { "$fullShimDir;$userPath" } else { $fullShimDir }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
  }

  $processEntries = @($env:Path -split ";" | Where-Object { $_ })
  $processHasShim = $false
  foreach ($entry in $processEntries) {
    if ((ConvertTo-FullPath $entry).Equals($fullShimDir, [System.StringComparison]::OrdinalIgnoreCase)) {
      $processHasShim = $true
      break
    }
  }
  if (-not $processHasShim) {
    $env:Path = "$fullShimDir;$env:Path"
  }
}

function Remove-ShimDirFromPath {
  if ($skipPathUpdate) {
    return
  }

  $fullShimDir = ConvertTo-FullPath $commandShimDir
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  if ($userPath) {
    $entries = @($userPath -split ";" | Where-Object {
      $_ -and -not (ConvertTo-FullPath $_).Equals($fullShimDir, [System.StringComparison]::OrdinalIgnoreCase)
    })
    [Environment]::SetEnvironmentVariable("Path", ($entries -join ";"), "User")
  }

  $processEntries = @($env:Path -split ";" | Where-Object {
    $_ -and -not (ConvertTo-FullPath $_).Equals($fullShimDir, [System.StringComparison]::OrdinalIgnoreCase)
  })
  $env:Path = ($processEntries -join ";")
}

function Save-ProfileRegistration {
  New-Item -ItemType Directory -Force -Path $registrationDir | Out-Null
  [ordered]@{
    version = 1
    profile = $profile
    project_root = $projectRoot.Path
    updated_at = (Get-Date).ToUniversalTime().ToString("o")
  } | ConvertTo-Json -Depth 3 | Set-Content -Path $registrationPath -Encoding UTF8
}

function Write-Shims {
  New-Item -ItemType Directory -Force -Path $commandShimDir | Out-Null
  New-Item -ItemType Directory -Force -Path $shimDir | Out-Null
  Save-ProfileRegistration

  $routerShim = @'
$ErrorActionPreference = "Stop"
$studioHome = Join-Path $env:USERPROFILE ".codex-context-studio"
$registrationDir = Join-Path $studioHome "registrations"
$forwardArgs = @($args)
$isControl = $forwardArgs.Count -ge 3 -and $forwardArgs[0] -eq "ctx" -and $forwardArgs[1] -in @("desktop", "proxy")
$isDevelopment = $isControl -and $forwardArgs[-1] -eq "dev"
$selectedProfile = if ($isDevelopment) { "development" } else { "production" }

if ($isDevelopment) {
  $forwardArgs = @($forwardArgs[0..($forwardArgs.Count - 2)])
}

function Read-Registration([string] $Profile) {
  $path = Join-Path $registrationDir "$Profile.json"
  if (-not (Test-Path -LiteralPath $path)) { return $null }
  try { return Get-Content -Raw -LiteralPath $path | ConvertFrom-Json } catch { return $null }
}

if ($isControl -and $isDevelopment -and -not (Read-Registration "development")) {
  $candidateRoot = (Get-Location).Path
  $managerCandidate = Join-Path $candidateRoot "scripts\codex-ctx-proxy.ps1"
  $packageCandidate = Join-Path $candidateRoot "package.json"
  if ((Test-Path -LiteralPath $managerCandidate) -and (Test-Path -LiteralPath $packageCandidate)) {
    try {
      $package = Get-Content -Raw -LiteralPath $packageCandidate | ConvertFrom-Json
      if ([string] $package.name -eq "codex-context-studio") {
        New-Item -ItemType Directory -Force -Path $registrationDir | Out-Null
        [ordered]@{ version = 1; profile = "development"; project_root = $candidateRoot; updated_at = (Get-Date).ToUniversalTime().ToString("o") } |
          ConvertTo-Json -Depth 3 | Set-Content -LiteralPath (Join-Path $registrationDir "development.json") -Encoding UTF8
      }
    } catch {}
  }
}

if (-not $isControl) {
  foreach ($candidateProfile in @("development", "production")) {
    $candidateState = Join-Path $studioHome "$candidateProfile\state\codex-ctx-proxy.json"
    if (Test-Path -LiteralPath $candidateState) {
      try {
        $candidatePayload = Get-Content -Raw -LiteralPath $candidateState | ConvertFrom-Json
        if ([bool] $candidatePayload.enabled) { $selectedProfile = $candidateProfile; break }
      } catch {}
    }
  }
}

$registration = Read-Registration $selectedProfile
if (-not $registration -and -not $isControl) {
  $registration = Read-Registration "production"
  if (-not $registration) { $registration = Read-Registration "development"; $selectedProfile = "development" }
}
if (-not $registration) {
  throw "Codex Context Studio $selectedProfile project is not registered. Run the matching install command from that project first."
}

$projectRoot = ([string] $registration.project_root).Trim()
$manager = Join-Path $projectRoot "scripts\codex-ctx-proxy.ps1"
if (-not (Test-Path -LiteralPath $manager)) {
  throw "Codex Context Studio $selectedProfile project root is unavailable: $projectRoot"
}

$env:CODEX_CONTEXT_STUDIO_PROFILE = $selectedProfile
$env:CODEX_CONTEXT_STUDIO_ROOT = $studioHome
& powershell -NoProfile -ExecutionPolicy Bypass -File $manager __dispatch @forwardArgs
exit $LASTEXITCODE
'@
  Set-Content -Path (Join-Path $commandShimDir "codex.ps1") -Value $routerShim -Encoding UTF8

  $cmdShim = @'
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0codex.ps1" %*
exit /b %ERRORLEVEL%
'@
  Set-Content -Path (Join-Path $commandShimDir "codex.cmd") -Value $cmdShim -Encoding ASCII

  Set-Content -Path (Join-Path $shimDir "current-project-root.txt") -Value $projectRoot.Path -Encoding UTF8
  $hookPsShim = @"
`$ErrorActionPreference = "Stop"
`$env:CODEX_CONTEXT_STUDIO_PROFILE = '$profile'
`$env:CODEX_CONTEXT_STUDIO_ROOT = '$($defaultHome.Replace("'", "''"))'
`$env:CODEX_CONTEXT_STUDIO_DATA_DIR = '$((Join-Path $defaultHome "shared").Replace("'", "''"))'
& '$($projectRoot.Path.Replace("'", "''"))\scripts\codex-context-hook.ps1'
exit `$LASTEXITCODE
"@
  Set-Content -Path (Join-Path $shimDir "$hookName.ps1") -Value $hookPsShim -Encoding UTF8
  Set-Content -Path (Join-Path $shimDir "$hookName.cmd") -Value "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0$hookName.ps1`"`r`nexit /b %ERRORLEVEL%`r`n" -Encoding ASCII

  $notifyPsShim = @"
param([Parameter(ValueFromRemainingArguments = `$true)][string[]] `$ForwardArgs)
`$ErrorActionPreference = "Stop"
`$env:CODEX_CONTEXT_STUDIO_PROFILE = '$profile'
`$env:CODEX_CONTEXT_STUDIO_ROOT = '$($defaultHome.Replace("'", "''"))'
`$env:CODEX_CONTEXT_STUDIO_DATA_DIR = '$((Join-Path $defaultHome "shared").Replace("'", "''"))'
& '$($projectRoot.Path.Replace("'", "''"))\scripts\codex-turn-ended-notify.ps1' @ForwardArgs
exit `$LASTEXITCODE
"@
  Set-Content -Path (Join-Path $shimDir "$notifyName.ps1") -Value $notifyPsShim -Encoding UTF8
  Set-Content -Path (Join-Path $shimDir "$notifyName.cmd") -Value "@echo off`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"%~dp0$notifyName.ps1`" %*`r`nexit /b %ERRORLEVEL%`r`n" -Encoding ASCII
}

function Ensure-Installed {
  param([bool] $Enabled)

  $state = Read-SwitchState
  $preferred = if ($state -and $state.real_codex) { [string] $state.real_codex } else { "" }
  $realCodex = Find-RealCodex -Preferred $preferred
  Write-Shims
  Save-SwitchState -Enabled $Enabled -RealCodex $realCodex
  Add-ShimDirToPath
  return $realCodex
}

function Ensure-ControlShimInstalled {
  param([bool] $Enabled)

  $state = Read-SwitchState
  $preferred = if ($state -and $state.real_codex) { [string] $state.real_codex } else { "" }
  $realCodex = ""
  try {
    $realCodex = Find-RealCodex -Preferred $preferred
  } catch {
    Write-Host "[codex-context-studio] official Codex CLI was not found yet; control commands were installed anyway." -ForegroundColor DarkYellow
    Write-Host "[codex-context-studio] install the official Codex CLI before running: codex ctx proxy on" -ForegroundColor DarkYellow
  }
  Write-Shims
  Save-SwitchState -Enabled $Enabled -RealCodex $realCodex
  Add-ShimDirToPath
  return $realCodex
}

function Show-Status {
  $state = Read-SwitchState
  $enabled = $false
  $realCodex = ""
  if ($state) {
    $enabled = [bool] $state.enabled
    $realCodex = [string] $state.real_codex
  }

  $firstCodex = Get-Command codex -ErrorAction SilentlyContinue
  $firstCodexPath = if ($firstCodex) { Get-CommandPath $firstCodex } else { "" }

  Write-Host "[codex-context-studio] profile: $profile"
  Write-Host "[codex-context-studio] project root: $($projectRoot.Path)"
  Write-Host "[codex-context-studio] proxy switch: $(if ($enabled) { 'on' } else { 'off' })"
  Write-Host "[codex-context-studio] shim dir: $shimDir"
  Write-Host "[codex-context-studio] state: $statePath"
  if ($realCodex) {
    Write-Host "[codex-context-studio] real codex: $realCodex"
  }
  if ($firstCodexPath) {
    Write-Host "[codex-context-studio] current codex resolves to: $firstCodexPath"
  }
}

function Write-PathRefreshHint {
  if (-not $skipPathUpdate) {
    Write-Host "[codex-context-studio] open a new terminal for bare 'codex' commands to pick up the shim." -ForegroundColor DarkYellow
  }
}

function Invoke-RealCodex {
  param([string[]] $ForwardArgs)
  $state = Read-SwitchState
  $preferred = if ($state -and $state.real_codex) { [string] $state.real_codex } else { "" }
  $realCodex = Find-RealCodex -Preferred $preferred
  & $realCodex @ForwardArgs
  exit $LASTEXITCODE
}

function Test-DesktopHookConfigInstalled {
  $desktopConfigPath = if ($env:CODEX_CONTEXT_STUDIO_DESKTOP_CONFIG) { $env:CODEX_CONTEXT_STUDIO_DESKTOP_CONFIG } else { Join-Path $env:USERPROFILE ".codex\config.toml" }
  if (-not (Test-Path $desktopConfigPath)) {
    return $false
  }

  try {
    $configText = Get-Content -Raw -Path $desktopConfigPath
    return (
      $configText.Contains("hooks.UserPromptSubmit") -and
      $configText.Contains("$hookName.cmd")
    )
  } catch {
    return $false
  }
}


$topBeginManaged = "# BEGIN CODEX_CONTEXT_STUDIO_DESKTOP_TOP"
$topEndManaged = "# END CODEX_CONTEXT_STUDIO_DESKTOP_TOP"
$providerBeginManaged = "# BEGIN CODEX_CONTEXT_STUDIO_DESKTOP_PROVIDER"
$providerEndManaged = "# END CODEX_CONTEXT_STUDIO_DESKTOP_PROVIDER"

function Get-CodexUpstreamInfo {
  $configPath = if ($env:CODEX_CONTEXT_STUDIO_DESKTOP_CONFIG) { $env:CODEX_CONTEXT_STUDIO_DESKTOP_CONFIG } else { Join-Path $env:USERPROFILE ".codex\config.toml" }

  if (-not (Test-Path $configPath)) {
    Write-Host "[codex-context-studio] Codex config not found: $configPath" -ForegroundColor Red
    Write-Host "[codex-context-studio] Please run 'codex' first to login (codex login) or configure a third-party API provider." -ForegroundColor Red
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
    Write-Host "[codex-context-studio] Codex config is empty after stripping managed blocks." -ForegroundColor Red
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

  if ($openaiBaseUrl) {
    $effectiveBaseUrl = $openaiBaseUrl
  }

  if (-not [string]::Equals($modelProvider, "openai", [System.StringComparison]::Ordinal)) {
    $escapedId = [regex]::Escape($modelProvider)
    $sectionPattern = "\[model_providers\.$escapedId\][\s\S]*?(?=\[\s*model_providers\.|\z)"
    $sectionMatch = [regex]::Match($content, $sectionPattern)
    if (-not $sectionMatch.Success) {
      Write-Host "[codex-context-studio] model_provider '$modelProvider' has no [model_providers.$modelProvider] section." -ForegroundColor Red
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
  }
}

function Get-CodexContextStudioCodexArgs {
  param([hashtable] $UpstreamInfo = $null)

  $requiresAuth = "true"
  if ($UpstreamInfo -and $UpstreamInfo.kind -eq "third_party") {
    $requiresAuth = "false"
  }

  Write-Shims
  $hookCommand = (Join-Path $shimDir "$hookName.cmd").Replace("\", "/")
  $hookConfig = "hooks.UserPromptSubmit=[{matcher='*',hooks=[{type='command',command='$hookCommand',timeout=10,statusMessage='CodexContextStudio'}]}]"
  $notifyCommand = (Join-Path $shimDir "$notifyName.cmd").Replace("\", "/")
  $notifyConfig = "notify=['$notifyCommand']"

  $configArgs = @(
    "-c", "model_providers.$providerId.name=Codex Context Studio",
    "-c", "model_providers.$providerId.base_url=http://${loopbackHost}:$proxyPort/v1",
    "-c", "model_providers.$providerId.requires_openai_auth=$requiresAuth",
    "-c", "model_providers.$providerId.wire_api=responses",
    "-c", "model_providers.$providerId.supports_websockets=false",
    "-c", "model_provider=$providerId",
    "-c", $notifyConfig
  )

  if ($UpstreamInfo -and $UpstreamInfo.kind -eq "third_party") {
    $configArgs += @("-c", "model_context_window=200000")
  }

  if (-not (Test-DesktopHookConfigInstalled)) {
    $configArgs += @(
      "-c", "features.hooks=true",
      "-c", $hookConfig
    )
  }

  $autoCompactTokenLimit = $env:CODEX_CONTEXT_STUDIO_AUTO_COMPACT_TOKEN_LIMIT
  if ($autoCompactTokenLimit) {
    $autoCompactTokenLimit = $autoCompactTokenLimit.Trim()
    if ($autoCompactTokenLimit -notmatch '^\d+$') {
      throw "CODEX_CONTEXT_STUDIO_AUTO_COMPACT_TOKEN_LIMIT must be an integer token count."
    }
    $configArgs += @("-c", "model_auto_compact_token_limit=$autoCompactTokenLimit")
  }

  return $configArgs
}

function Invoke-DesktopProxyCommand {
  param([string] $DesktopCommand)

  $desktopScript = Join-Path $projectRoot.Path "scripts\codex-desktop-proxy.ps1"
  & powershell -NoProfile -ExecutionPolicy Bypass -File $desktopScript $DesktopCommand 2>&1 | ForEach-Object {
    Write-Host $_
  }
  return [int] $LASTEXITCODE
}

function Invoke-CodexContextStudioCodex {
  param([string[]] $ForwardArgs)
  $state = Read-SwitchState
  $preferred = if ($state -and $state.real_codex) { [string] $state.real_codex } else { "" }
  $realCodex = Find-RealCodex -Preferred $preferred

  $upstreamInfo = Get-CodexUpstreamInfo
  $configArgs = Get-CodexContextStudioCodexArgs -UpstreamInfo $upstreamInfo

  $previousForceUrl = $env:CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_BASE_URL
  $previousForceKey = $env:CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_API_KEY

  $localNoProxy = "$loopbackHost,localhost,127.0.0.1,::1"
  $previousNoProxy = $env:NO_PROXY
  $previousLowerNoProxy = $env:no_proxy
  $env:NO_PROXY = if ($env:NO_PROXY) { "$localNoProxy,$env:NO_PROXY" } else { $localNoProxy }
  $env:no_proxy = if ($env:no_proxy) { "$localNoProxy,$env:no_proxy" } else { $localNoProxy }

  if ($upstreamInfo.kind -eq "third_party") {
    $env:CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_BASE_URL = $upstreamInfo.effective_base_url
    $env:CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_API_KEY = $upstreamInfo.api_key
    Write-Host "[codex-context-studio] upstream: $($upstreamInfo.effective_base_url) (third-party)" -ForegroundColor Cyan
  }

  try {
    & $realCodex @configArgs @ForwardArgs
    exit $LASTEXITCODE
  } finally {
    if ($null -eq $previousForceUrl) {
      Remove-Item Env:\CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_BASE_URL -ErrorAction SilentlyContinue
    } else {
      $env:CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_BASE_URL = $previousForceUrl
    }
    if ($null -eq $previousForceKey) {
      Remove-Item Env:\CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_API_KEY -ErrorAction SilentlyContinue
    } else {
      $env:CODEX_CONTEXT_STUDIO_FORCE_UPSTREAM_API_KEY = $previousForceKey
    }
    if ($null -eq $previousNoProxy) {
      Remove-Item Env:\NO_PROXY -ErrorAction SilentlyContinue
    } else {
      $env:NO_PROXY = $previousNoProxy
    }
    if ($null -eq $previousLowerNoProxy) {
      Remove-Item Env:\no_proxy -ErrorAction SilentlyContinue
    } else {
      $env:no_proxy = $previousLowerNoProxy
    }
  }
}

function Remove-ProfileInstallation {
  $desktopStatePath = Join-Path $profileRoot "state\desktop.json"
  $desktopEnabled = $false
  if (Test-Path -LiteralPath $desktopStatePath) {
    try {
      $desktopState = Get-Content -Raw -LiteralPath $desktopStatePath | ConvertFrom-Json
      $desktopEnabled = [bool] $desktopState.enabled
    } catch {}
  }
  if ($desktopEnabled) {
    Invoke-DesktopProxyCommand -DesktopCommand "off" | Out-Null
  }
  Remove-Item -LiteralPath $profileRoot -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $registrationPath -Force -ErrorAction SilentlyContinue

  $remainingRegistrations = @(Get-ChildItem -LiteralPath $registrationDir -Filter "*.json" -File -ErrorAction SilentlyContinue)
  if ($remainingRegistrations.Count -eq 0) {
    Remove-Item -LiteralPath $commandShimDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-ShimDirFromPath
  }

  Write-Host "[codex-context-studio] removed $profile registration and shims"
}

function Invoke-Dispatch {
  param([string[]] $ForwardArgs)

  if ($ForwardArgs.Count -ge 3 -and $ForwardArgs[0] -eq "ctx" -and $ForwardArgs[1] -eq "proxy") {
    switch ($ForwardArgs[2]) {
      "on" {
        $realCodex = Ensure-Installed -Enabled $true
        $desktopExitCode = Invoke-DesktopProxyCommand -DesktopCommand "on"
        if ($desktopExitCode -ne 0) {
          exit $desktopExitCode
        }
        Write-Host "[codex-context-studio] codex ctx proxy on"
        Write-Host "[codex-context-studio] persistent proxy services are running"
        Write-Host "[codex-context-studio] real codex: $realCodex"
        exit 0
      }
      "off" {
        $realCodex = Ensure-Installed -Enabled $false
        $desktopExitCode = Invoke-DesktopProxyCommand -DesktopCommand "off"
        if ($desktopExitCode -ne 0) {
          exit $desktopExitCode
        }
        Write-Host "[codex-context-studio] codex ctx proxy off"
        Write-Host "[codex-context-studio] persistent proxy services are stopped"
        Write-Host "[codex-context-studio] codex now passes through to: $realCodex"
        exit 0
      }
      "status" {
        Show-Status
        exit 0
      }
      "uninstall" {
        Remove-ProfileInstallation
        exit 0
      }
      default {
        Write-Host "Usage: codex ctx proxy <on|off|status|uninstall>$profileSuffix" -ForegroundColor Red
        exit 2
      }
    }
  }

  if ($ForwardArgs.Count -ge 3 -and $ForwardArgs[0] -eq "ctx" -and $ForwardArgs[1] -eq "desktop") {
    if ($ForwardArgs[2] -eq "uninstall") {
      Remove-ProfileInstallation
      exit 0
    }
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $projectRoot.Path "scripts\codex-desktop-proxy.ps1") $ForwardArgs[2]
    exit $LASTEXITCODE
  }

  $state = Read-SwitchState
  if ($state -and [bool] $state.enabled) {
    Invoke-CodexContextStudioCodex -ForwardArgs $ForwardArgs
  }

  Invoke-RealCodex -ForwardArgs $ForwardArgs
}

switch ($Command) {
  "install" {
    $state = Read-SwitchState
    $enabled = if ($state) { [bool] $state.enabled } else { $false }
    $realCodex = Ensure-ControlShimInstalled -Enabled $enabled
    Write-Host "[codex-context-studio] installed $profile codex ctx proxy shim"
    if ($realCodex) {
      Write-Host "[codex-context-studio] real codex: $realCodex"
    }
    Write-Host "[codex-context-studio] after opening a new terminal, run: codex ctx proxy on$profileSuffix"
    Write-Host "[codex-context-studio] proxy stays off until you enable it."
    Write-PathRefreshHint
    break
  }
  "on" {
    $realCodex = Ensure-Installed -Enabled $true
    $desktopExitCode = Invoke-DesktopProxyCommand -DesktopCommand "on"
    if ($desktopExitCode -ne 0) {
      exit $desktopExitCode
    }
    Write-Host "[codex-context-studio] codex ctx proxy on"
    Write-Host "[codex-context-studio] persistent proxy services are running"
    Write-Host "[codex-context-studio] real codex: $realCodex"
    Write-PathRefreshHint
    break
  }
  "off" {
    $realCodex = Ensure-Installed -Enabled $false
    $desktopExitCode = Invoke-DesktopProxyCommand -DesktopCommand "off"
    if ($desktopExitCode -ne 0) {
      exit $desktopExitCode
    }
    Write-Host "[codex-context-studio] codex ctx proxy off"
    Write-Host "[codex-context-studio] persistent proxy services are stopped"
    Write-Host "[codex-context-studio] codex now passes through to: $realCodex"
    break
  }
  "status" {
    Show-Status
    break
  }
  "uninstall" {
    Remove-ProfileInstallation
    break
  }
  "__dispatch" {
    Invoke-Dispatch -ForwardArgs $Rest
    break
  }
  default {
    Write-Host "Usage: codex-ctx-proxy.ps1 <install|on|off|status|uninstall>$profileSuffix" -ForegroundColor Red
    Write-Host "After install, use: codex ctx proxy <on|off|status|uninstall>$profileSuffix"
    exit 2
  }
}
