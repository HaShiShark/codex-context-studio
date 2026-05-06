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
$backupDir = Join-Path $stateDir "backups"
$proxyPort = if ($env:HASH_CONTEXT_PROXY_PORT) { $env:HASH_CONTEXT_PROXY_PORT } else { "8787" }
$controlPort = if ($env:HASH_CONTEXT_CONTROL_PORT) { $env:HASH_CONTEXT_CONTROL_PORT } else { "8790" }

$topBegin = "# BEGIN HASH_CONTEXT_DESKTOP_TOP"
$topEnd = "# END HASH_CONTEXT_DESKTOP_TOP"
$providerBegin = "# BEGIN HASH_CONTEXT_DESKTOP_PROVIDER"
$providerEnd = "# END HASH_CONTEXT_DESKTOP_PROVIDER"

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
  $proxyStatePath = Join-Path $projectRoot.Path "data\proxy_state.json"
  if (Test-Path $proxyStatePath) {
    try {
      $raw = Get-Content -Raw -Path $proxyStatePath | ConvertFrom-Json
      if ($raw.sessions) {
        $sessionCount = @($raw.sessions).Count
      }
      $activeSessionId = [string] $raw.active_session_id
    } catch {
    }
  }

  $logPath = Join-Path $projectRoot.Path "data\proxy.log"
  $logLength = 0
  if (Test-Path $logPath) {
    $logLength = (Get-Item $logPath).Length
  }

  return @{
    session_count = $sessionCount
    active_session_id = $activeSessionId
    proxy_log_length = $logLength
  }
}

function Remove-ManagedBlocks {
  param([string] $Text)
  $escapedTopBegin = [regex]::Escape($topBegin)
  $escapedTopEnd = [regex]::Escape($topEnd)
  $escapedProviderBegin = [regex]::Escape($providerBegin)
  $escapedProviderEnd = [regex]::Escape($providerEnd)
  $Text = [regex]::Replace($Text, "(?ms)\r?\n?$escapedTopBegin\r?\n.*?\r?\n$escapedTopEnd\r?\n?", "`r`n")
  $Text = [regex]::Replace($Text, "(?ms)\r?\n?$escapedProviderBegin\r?\n.*?\r?\n$escapedProviderEnd\r?\n?", "`r`n")
  return $Text.TrimEnd() + "`r`n"
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

function Get-TopBlock {
  $hookPath = (Join-Path $projectRoot.Path "scripts\codex-context-hook.cmd").Replace("\", "/")
  $hookCommand = ConvertTo-TomlBasicString $hookPath
  return @"
$topBegin
model_provider = "hash-context"
features.codex_hooks = true
hooks.UserPromptSubmit = [{ matcher = "*", hooks = [{ type = "command", command = $hookCommand, timeout = 5, statusMessage = "HashContext" }] }]
$topEnd
"@
}

function Get-ProviderBlock {
  return @"
$providerBegin
[model_providers.hash-context]
name = "Hash Context"
base_url = "http://localhost:$proxyPort/v1"
requires_openai_auth = true
wire_api = "responses"
supports_websockets = false
$providerEnd
"@
}

function Set-DesktopConfigEnabled {
  param([string] $Mode)

  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $configPath) | Out-Null
  New-Item -ItemType Directory -Force -Path $backupDir | Out-Null

  $hadConfig = Test-Path $configPath
  $originalText = if ($hadConfig) { Get-Content -Raw -Path $configPath } else { "" }
  $backupPath = Join-Path $backupDir ("config.toml." + (Get-Date -Format "yyyyMMdd-HHmmss") + ".bak")
  if ($hadConfig) {
    Copy-Item -Path $configPath -Destination $backupPath -Force
  } else {
    Set-Content -Path $backupPath -Value "" -Encoding UTF8
  }

  $text = Repair-ProjectTables -Text (Remove-ManagedBlocks -Text $originalText)
  $topBlock = Get-TopBlock
  $providerBlock = Get-ProviderBlock
  $firstTable = [regex]::Match($text, "(?m)^\s*\[")
  if ($firstTable.Success) {
    $text = $text.Insert($firstTable.Index, $topBlock + "`r`n")
  } else {
    $text = $topBlock + "`r`n" + $text
  }
  $text = $text.TrimEnd() + "`r`n`r`n" + $providerBlock + "`r`n"
  Set-Content -Path $configPath -Value $text -Encoding UTF8

  $snapshot = Get-ProxySnapshot
  Save-DesktopState @{
    version = 1
    enabled = $true
    mode = $Mode
    config_path = $configPath
    backup_path = $backupPath
    had_config = $hadConfig
    service_pid = 0
    project_root = $projectRoot.Path
    proxy_port = $proxyPort
    control_port = $controlPort
    session_count_before = $snapshot.session_count
    proxy_log_length_before = $snapshot.proxy_log_length
    updated_at = (Get-Date).ToUniversalTime().ToString("o")
  }
}

function Restore-DesktopConfig {
  $hadConfig = Test-Path $configPath
  if (-not $hadConfig) {
    return
  }
  $text = Get-Content -Raw -Path $configPath
  $text = Repair-ProjectTables -Text (Remove-ManagedBlocks -Text $text)
  if ($text.Trim()) {
    Set-Content -Path $configPath -Value $text -Encoding UTF8
  } else {
    Remove-Item -Path $configPath -Force
  }
}

function Repair-DesktopConfig {
  if (-not (Test-Path $configPath)) {
    Write-Host "[hash-context] config not found: $configPath"
    return
  }

  New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
  $originalText = Get-Content -Raw -Path $configPath
  $repairedText = Repair-ProjectTables -Text $originalText
  $normalizedOriginal = $originalText.TrimEnd() + "`r`n"

  if ($repairedText -eq $normalizedOriginal) {
    Write-Host "[hash-context] config repair: no malformed projects tables found"
    return
  }

  $backupPath = Join-Path $backupDir ("config.toml.repair." + (Get-Date -Format "yyyyMMdd-HHmmss") + ".bak")
  Copy-Item -Path $configPath -Destination $backupPath -Force
  Set-Content -Path $configPath -Value $repairedText -Encoding UTF8
  Write-Host "[hash-context] config repaired"
  Write-Host "[hash-context] backup: $backupPath"
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

  return Start-Process -FilePath "npm.cmd" -ArgumentList @("run", "window") -WorkingDirectory $projectRoot.Path -WindowStyle Hidden -PassThru
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
  if (-not (Test-Path $configPath)) {
    return $false
  }
  try {
    $text = Get-Content -Raw -Path $configPath
    return (
      $text.Contains($topBegin) -and
      $text.Contains($providerBegin) -and
      $text.Contains('model_provider = "hash-context"') -and
      ($text.Contains("http://localhost:$proxyPort/v1") -or $text.Contains("http://127.0.0.1:$proxyPort/v1"))
    )
  } catch {
    return $false
  }
}

function Start-DesktopServices {
  if ((Test-HttpOk "http://127.0.0.1:$proxyPort/api/proxy/sessions") -and
      (Test-HttpOk "http://127.0.0.1:8765/api/init") -and
      (Test-HttpOk "http://127.0.0.1:$controlPort/health")) {
    Write-Host "[hash-context] desktop services already running"
    return 0
  }

  $previousStartHidden = $env:HASH_CONTEXT_START_HIDDEN
  $previousControlPort = $env:HASH_CONTEXT_CONTROL_PORT
  $env:HASH_CONTEXT_START_HIDDEN = "1"
  $env:HASH_CONTEXT_CONTROL_PORT = $controlPort
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

  Wait-HttpOk -Name "proxy" -Url "http://127.0.0.1:$proxyPort/api/proxy/sessions"
  Wait-HttpOk -Name "backend" -Url "http://127.0.0.1:8765/api/init"
  Wait-HttpOk -Name "window-control" -Url "http://127.0.0.1:$controlPort/health"
  return $process.Id
}

function Update-ServicePid {
  param([int] $ServiceProcessId)
  $state = Read-DesktopState
  if (-not $state) {
    return
  }
  Save-DesktopState @{
    version = 1
    enabled = [bool] $state.enabled
    mode = [string] $state.mode
    config_path = [string] $state.config_path
    backup_path = [string] $state.backup_path
    had_config = [bool] $state.had_config
    service_pid = $ServiceProcessId
    project_root = [string] $state.project_root
    proxy_port = [string] $state.proxy_port
    control_port = [string] $state.control_port
    session_count_before = [int] $state.session_count_before
    proxy_log_length_before = [int64] $state.proxy_log_length_before
    updated_at = (Get-Date).ToUniversalTime().ToString("o")
  }
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
}

function Show-DesktopStatus {
  $state = Read-DesktopState
  $snapshot = Get-ProxySnapshot
  $enabled = ($state -and [bool] $state.enabled)
  $beforeSessions = if ($state) { [int] $state.session_count_before } else { $snapshot.session_count }
  $beforeLog = if ($state) { [int64] $state.proxy_log_length_before } else { $snapshot.proxy_log_length }
  Write-Host "[hash-context] desktop proxy: $(if ($enabled) { 'on' } else { 'off' })"
  Write-Host "[hash-context] config: $configPath"
  Write-Host "[hash-context] config blocks: $(if (Test-DesktopConfigInstalled) { 'installed' } else { 'missing' })"
  Write-Host "[hash-context] services proxy: $(if (Test-HttpOk "http://127.0.0.1:$proxyPort/api/proxy/sessions") { 'ready' } else { 'not ready' })"
  Write-Host "[hash-context] services backend: $(if (Test-HttpOk "http://127.0.0.1:8765/api/init") { 'ready' } else { 'not ready' })"
  Write-Host "[hash-context] services control: $(if (Test-HttpOk "http://127.0.0.1:$controlPort/health") { 'ready' } else { 'not ready' })"
  Write-Host "[hash-context] sessions before/current: $beforeSessions/$($snapshot.session_count)"
  Write-Host "[hash-context] proxy log bytes before/current: $beforeLog/$($snapshot.proxy_log_length)"
  if ($state -and $state.backup_path) {
    Write-Host "[hash-context] backup: $($state.backup_path)"
  }
  if ($snapshot.session_count -gt $beforeSessions -or $snapshot.proxy_log_length -gt $beforeLog) {
    Write-Host "[hash-context] probe signal: proxy activity increased" -ForegroundColor Green
  } elseif ($enabled) {
    Write-Host "[hash-context] probe signal: no desktop request observed yet; open a fresh desktop chat and send a short message"
  }
}

switch ($Command) {
  "probe" {
    Set-DesktopConfigEnabled -Mode "probe"
    $serviceProcessId = Start-DesktopServices
    Update-ServicePid -ServiceProcessId $serviceProcessId
    Write-Host "[hash-context] desktop probe is armed"
    Write-Host "[hash-context] keep this desktop app open; use a fresh chat for testing, then run: codex ctx desktop status"
    Write-Host "[hash-context] restore with: codex ctx desktop off"
    break
  }
  "on" {
    Stop-CodexProcesses -Reason "desktop proxy on"
    Set-DesktopConfigEnabled -Mode "on"
    $serviceProcessId = Start-DesktopServices
    Update-ServicePid -ServiceProcessId $serviceProcessId
    Write-Host "[hash-context] desktop proxy on"
    Write-Host "[hash-context] keep this desktop app open; use a fresh chat for testing"
    break
  }
  "off" {
    Stop-CodexProcesses -Reason "desktop proxy off"
    Restore-DesktopConfig
    Stop-DesktopServices
    if (Test-Path $statePath) {
      Remove-Item -Path $statePath -Force
    }
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
