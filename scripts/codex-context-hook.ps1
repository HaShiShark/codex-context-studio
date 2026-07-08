$ErrorActionPreference = "Stop"

try {
  $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
  [Console]::InputEncoding = $utf8NoBom
  [Console]::OutputEncoding = $utf8NoBom
  $OutputEncoding = $utf8NoBom
} catch {
}

$loopbackHost = if ($env:HASH_CONTEXT_HOST) { $env:HASH_CONTEXT_HOST } else { "localhost" }
$serviceProbeHost = if ($loopbackHost -eq "localhost") { "127.0.0.1" } else { $loopbackHost }
$proxyPort = if ($env:HASH_CONTEXT_PROXY_PORT) { $env:HASH_CONTEXT_PROXY_PORT } else { "8787" }
$backendPort = if ($env:HASH_WEB_PORT) { $env:HASH_WEB_PORT } else { "8765" }
$hashContextHome = Join-Path $env:USERPROFILE ".hash-context-codex"

function Get-HashContextLogDir {
  $logDir = Join-Path $hashContextHome "logs"
  New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  return $logDir
}

function Write-HookJson {
  param(
    [hashtable] $Payload
  )
  $Payload | ConvertTo-Json -Compress -Depth 8
}

function Write-HookLog {
  param(
    [string] $Message
  )
  try {
    $logDir = Get-HashContextLogDir
    Add-Content -Path (Join-Path $logDir "codex-context-hook.log") -Value "$((Get-Date).ToUniversalTime().ToString("o")) $Message" -Encoding UTF8
  } catch {
  }
}

function Get-MainTurnStatePath {
  try {
    $logDir = Get-HashContextLogDir
    return (Join-Path $logDir "codex-main-turn.json")
  } catch {
    return ""
  }
}

function Save-ActiveMainTurn {
  param(
    [string] $SessionId,
    [string] $TurnId
  )

  if (-not $SessionId) {
    return
  }

  $path = Get-MainTurnStatePath
  if (-not $path) {
    return
  }

  try {
    $existingTurns = @()
    if (Test-Path $path) {
      try {
        $existing = Get-Content -Raw -Path $path -Encoding UTF8 | ConvertFrom-Json
        if ($existing.active_turns) {
          $existingTurns = @($existing.active_turns)
        } elseif ($existing.session_id) {
          $existingTurns = @($existing)
        }
      } catch {
        $existingTurns = @()
      }
    }
    $turns = @(
      foreach ($turn in $existingTurns) {
        $turnSessionId = ([string] $turn.session_id).Trim()
        if ($turnSessionId -and $turnSessionId -ne $SessionId) {
          $turn
        }
      }
      [pscustomobject] @{
        session_id = $SessionId
        turn_id = $TurnId
        started_at = (Get-Date).ToUniversalTime().ToString("o")
      }
    )
    $payload = @{
      active_turns = $turns
      session_id = $SessionId
      turn_id = $TurnId
      updated_at = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json -Compress
    Set-Content -Path $path -Value $payload -Encoding UTF8
  } catch {
    Write-HookLog "main-turn state save failed session_id=$SessionId error=$($_.Exception.Message)"
  }
}

function Clear-ActiveMainTurn {
  param(
    [string] $SessionId
  )

  if (-not $SessionId) {
    return
  }

  $path = Get-MainTurnStatePath
  if (-not $path -or -not (Test-Path $path)) {
    return
  }

  try {
    $state = Get-Content -Raw -Path $path -Encoding UTF8 | ConvertFrom-Json
    $existingTurns = @()
    if ($state.active_turns) {
      $existingTurns = @($state.active_turns)
    } elseif ($state.session_id) {
      $existingTurns = @($state)
    }

    $remaining = @(
      foreach ($turn in $existingTurns) {
        $turnSessionId = ([string] $turn.session_id).Trim()
        if ($turnSessionId -and $turnSessionId -ne $SessionId) {
          $turn
        }
      }
    )

    if ($remaining.Count -le 0) {
      Remove-Item -Path $path -Force -ErrorAction SilentlyContinue
      return
    }

    $lastTurn = $remaining[($remaining.Count - 1)]
    $payload = @{
      active_turns = $remaining
      session_id = ([string] $lastTurn.session_id).Trim()
      turn_id = ([string] $lastTurn.turn_id).Trim()
      updated_at = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json -Compress -Depth 6
    Set-Content -Path $path -Value $payload -Encoding UTF8
  } catch {
    Write-HookLog "main-turn state clear failed session_id=$SessionId error=$($_.Exception.Message)"
  }
}

function Set-MainTurnState {
  param(
    [string] $SessionId,
    [string] $TurnId,
    [bool] $Running
  )

  if (-not $SessionId) {
    return
  }

  try {
    $encodedSessionId = [uri]::EscapeDataString($SessionId)
    $payload = @{
      turn_id = $TurnId
      running = $Running
    } | ConvertTo-Json -Compress
    Invoke-WebRequest -Uri "http://${serviceProbeHost}:$proxyPort/api/proxy/sessions/$encodedSessionId/main-turn" -Method Post -Body $payload -ContentType "application/json" -UseBasicParsing -TimeoutSec 2 | Out-Null
    Write-HookLog "main-turn state ok session_id=$SessionId turn_id=$TurnId running=$Running"
  } catch {
    Write-HookLog "main-turn state failed session_id=$SessionId turn_id=$TurnId running=$Running error=$($_.Exception.Message)"
  }
}

function Start-MainCodexTurn {
  param(
    [string] $SessionId,
    [string] $TurnId
  )

  if (-not $SessionId) {
    return
  }
  if (-not $TurnId) {
    $TurnId = [guid]::NewGuid().ToString("N")
  }

  Save-ActiveMainTurn -SessionId $SessionId -TurnId $TurnId
  Set-MainTurnState -SessionId $SessionId -TurnId $TurnId -Running $true
}

function Find-CodexSessionId {
  param(
    [object] $Value
  )

  if ($null -eq $Value) {
    return ""
  }

  if ($Value -is [string]) {
    $match = [regex]::Match($Value, "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
    if ($match.Success) {
      return $match.Value.ToLowerInvariant()
    }
    return ""
  }

  if ($Value -is [System.Collections.IDictionary]) {
    foreach ($key in $Value.Keys) {
      $keyText = [string] $key
      $candidate = $Value[$key]
      if ($keyText -match "session|conversation|thread|window|request") {
        $found = Find-CodexSessionId -Value $candidate
        if ($found) {
          return $found
        }
      }
    }
    foreach ($key in $Value.Keys) {
      $found = Find-CodexSessionId -Value $Value[$key]
      if ($found) {
        return $found
      }
    }
    return ""
  }

  if ($Value -is [System.Collections.IEnumerable] -and -not ($Value -is [string])) {
    foreach ($item in $Value) {
      $found = Find-CodexSessionId -Value $item
      if ($found) {
        return $found
      }
    }
    return ""
  }

  $properties = $Value.PSObject.Properties
  foreach ($property in $properties) {
    if ($property.Name -match "session|conversation|thread|window|request") {
      $found = Find-CodexSessionId -Value $property.Value
      if ($found) {
        return $found
      }
    }
  }
  foreach ($property in $properties) {
    $found = Find-CodexSessionId -Value $property.Value
    if ($found) {
      return $found
    }
  }

  return ""
}

function Find-LatestHistorySessionId {
  param(
    [string] $Prompt
  )

  try {
    $historyPath = Join-Path $env:USERPROFILE ".codex\history.jsonl"
    if (-not (Test-Path $historyPath)) {
      return ""
    }
    $lines = Get-Content -Path $historyPath -Tail 80 -ErrorAction Stop
    [array]::Reverse($lines)
    foreach ($line in $lines) {
      try {
        $entry = $line | ConvertFrom-Json
      } catch {
        continue
      }
      $text = ([string] $entry.text).Trim()
      $sessionId = [string] $entry.session_id
      if ($text -eq $Prompt -and $sessionId -match "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$") {
        return $sessionId.ToLowerInvariant()
      }
    }
  } catch {
  }
  return ""
}

function Test-ProxySessionExists {
  param(
    [string] $SessionId
  )

  if (-not $SessionId) {
    return $false
  }

  try {
    $encodedSessionId = [uri]::EscapeDataString($SessionId)
    Invoke-WebRequest -Uri "http://${serviceProbeHost}:$backendPort/api/proxy/sessions/$encodedSessionId" -Method Get -UseBasicParsing -TimeoutSec 2 | Out-Null
    return $true
  } catch {
    return $false
  }
}

function Sync-LocalCodexSession {
  param(
    [string] $SessionId
  )

  if (-not $SessionId) {
    return
  }

  if (Test-ProxySessionExists -SessionId $SessionId) {
    Write-HookLog "local-session-sync skipped proxy-session-exists session_id=$SessionId"
    return
  }

  try {
    $payload = @{ session_id = $SessionId } | ConvertTo-Json -Compress
    Invoke-WebRequest -Uri "http://${serviceProbeHost}:$backendPort/api/codex-local-session-sync" -Method Post -Body $payload -ContentType "application/json" -UseBasicParsing -TimeoutSec 8 | Out-Null
    Write-HookLog "local-session-sync ok session_id=$SessionId"
  } catch {
    Write-HookLog "local-session-sync failed session_id=$SessionId error=$($_.Exception.Message)"
  }
}

function Start-LocalCodexSessionSync {
  param(
    [string] $SessionId
  )

  if (-not $SessionId) {
    return
  }

  if (Test-ProxySessionExists -SessionId $SessionId) {
    Write-HookLog "local-session-sync skipped proxy-session-exists session_id=$SessionId"
    return
  }

  try {
    $logDir = Get-HashContextLogDir
    $logPath = Join-Path $logDir "codex-context-hook.log"
    $escapedSessionId = $SessionId.Replace("'", "''")
    $escapedLogPath = $logPath.Replace("'", "''")
    $script = @"
`$ErrorActionPreference = "Stop"
function Write-BackgroundLog {
  param([string] `$Message)
  try {
    Add-Content -Path '$escapedLogPath' -Value "`$((Get-Date).ToUniversalTime().ToString("o")) `$Message" -Encoding UTF8
  } catch {
  }
}
`$sessionId = '$escapedSessionId'
try {
  `$payload = @{ session_id = `$sessionId } | ConvertTo-Json -Compress
  Invoke-WebRequest -Uri "http://${serviceProbeHost}:$backendPort/api/codex-local-session-sync" -Method Post -Body `$payload -ContentType "application/json" -UseBasicParsing -TimeoutSec 20 | Out-Null
  Write-BackgroundLog "local-session-sync ok session_id=`$sessionId"
} catch {
  Write-BackgroundLog "local-session-sync failed session_id=`$sessionId error=`$(`$_.Exception.Message)"
}
"@
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($script))
    Start-Process -FilePath "powershell" -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $encoded) -WindowStyle Hidden | Out-Null
    Write-HookLog "local-session-sync started session_id=$SessionId"
  } catch {
    Write-HookLog "local-session-sync start failed session_id=$SessionId error=$($_.Exception.Message)"
  }
}

function Consume-ContextEditMarker {
  param(
    [string] $SessionId
  )

  if (-not $SessionId) {
    return $null
  }

  try {
    $payload = @{ session_id = $SessionId } | ConvertTo-Json -Compress
    $response = Invoke-WebRequest -Uri "http://${serviceProbeHost}:$backendPort/api/context-edit-marker-consume" -Method Post -Body $payload -ContentType "application/json; charset=utf-8" -UseBasicParsing -TimeoutSec 2
    $body = $response.Content | ConvertFrom-Json
    if ($body -and $body.marker) {
      Write-HookLog "context-edit-marker consumed session_id=$SessionId marker=$($response.Content)"
      return $body.marker
    }
  } catch {
    Write-HookLog "context-edit-marker consume failed session_id=$SessionId error=$($_.Exception.Message)"
  }

  return $null
}

function Format-ContextEditSystemMessage {
  param(
    [object] $Marker
  )

  return "Context has been edited."
}

$raw = ([Console]::In.ReadToEnd()).TrimStart([char] 0xFEFF)
try {
  $inputPayload = $raw | ConvertFrom-Json
} catch {
  Write-HookLog "invalid-json raw=$($raw.Substring(0, [Math]::Min(1000, $raw.Length)))"
  Write-HookJson @{ continue = $true; suppressOutput = $true }
  exit 0
}

$prompt = ([string] $inputPayload.prompt).Trim()
$commands = @("/context", "/ctx", "context", "ctx")

$sessionIdForLog = ""
try {
  $sessionIdForLog = Find-CodexSessionId -Value $inputPayload
  if (-not $sessionIdForLog) {
    $sessionIdForLog = Find-LatestHistorySessionId -Prompt $prompt
  }
  Write-HookLog "prompt=$prompt session_id=$sessionIdForLog payload=$($raw.Substring(0, [Math]::Min(3000, $raw.Length)))"
} catch {
}

if ($commands -notcontains $prompt) {
  $turnIdForLog = ([string] $inputPayload.turn_id).Trim()
  Start-MainCodexTurn -SessionId $sessionIdForLog -TurnId $turnIdForLog

  $contextEditMarker = Consume-ContextEditMarker -SessionId $sessionIdForLog
  if ($contextEditMarker) {
    Write-HookJson @{
      continue = $true
      suppressOutput = $false
      systemMessage = Format-ContextEditSystemMessage -Marker $contextEditMarker
    }
    exit 0
  }

  Write-HookJson @{ continue = $true; suppressOutput = $true }
  exit 0
}

$controlPort = $env:HASH_CONTEXT_CONTROL_PORT
if (-not $controlPort) {
  $controlPort = "8790"
}

try {
  $sessionId = Find-CodexSessionId -Value $inputPayload
  if (-not $sessionId) {
    $sessionId = Find-LatestHistorySessionId -Prompt $prompt
  }
  if ($sessionId) {
    Set-MainTurnState -SessionId $sessionId -TurnId "" -Running $false
    Clear-ActiveMainTurn -SessionId $sessionId
  }
  $showUrl = "http://${loopbackHost}:$controlPort/show"
  if ($sessionId) {
    $showUrl = "$showUrl`?session_id=$([uri]::EscapeDataString($sessionId))"
  }
  Invoke-WebRequest -Uri $showUrl -Method Post -UseBasicParsing -TimeoutSec 2 | Out-Null
  Write-HookLog "show ok session_id=$sessionId"
  Start-LocalCodexSessionSync -SessionId $sessionId
  Write-HookJson @{
    continue = $false
    decision = "block"
    reason = "Opened Hash Context Workbench"
    suppressOutput = $true
    stopReason = "Opened Hash Context Workbench"
  }
} catch {
  $reason = "Hash Context Workbench is not running: $($_.Exception.Message)"
  Write-HookJson @{
    continue = $false
    decision = "block"
    reason = $reason
    suppressOutput = $false
    stopReason = $reason
  }
}
