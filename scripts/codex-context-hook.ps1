$ErrorActionPreference = "Stop"

try {
  $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
  [Console]::InputEncoding = $utf8NoBom
  [Console]::OutputEncoding = $utf8NoBom
  $OutputEncoding = $utf8NoBom
} catch {
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
    $root = Resolve-Path (Join-Path $PSScriptRoot "..")
    $logDir = Join-Path $root "logs"
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    Add-Content -Path (Join-Path $logDir "codex-context-hook.log") -Value "$((Get-Date).ToUniversalTime().ToString("o")) $Message" -Encoding UTF8
  } catch {
  }
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

function Sync-LocalCodexSession {
  param(
    [string] $SessionId
  )

  if (-not $SessionId) {
    return
  }

  try {
    $payload = @{ session_id = $SessionId; title = "Codex $($SessionId.Substring(0, 8))" } | ConvertTo-Json -Compress
    Invoke-WebRequest -Uri "http://127.0.0.1:8765/api/codex-local-session-sync" -Method Post -Body $payload -ContentType "application/json" -UseBasicParsing -TimeoutSec 8 | Out-Null
    Write-HookLog "local-session-sync ok session_id=$SessionId"
  } catch {
    Write-HookLog "local-session-sync failed session_id=$SessionId error=$($_.Exception.Message)"
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
    $response = Invoke-WebRequest -Uri "http://127.0.0.1:8765/api/context-edit-marker-consume" -Method Post -Body $payload -ContentType "application/json; charset=utf-8" -UseBasicParsing -TimeoutSec 2
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
  Sync-LocalCodexSession -SessionId $sessionId
  $showUrl = "http://127.0.0.1:$controlPort/show"
  if ($sessionId) {
    $showUrl = "$showUrl`?session_id=$([uri]::EscapeDataString($sessionId))"
  }
  Invoke-WebRequest -Uri $showUrl -Method Post -UseBasicParsing -TimeoutSec 2 | Out-Null
  Write-HookJson @{
    continue = $false
    suppressOutput = $true
    stopReason = "Opened Hash Context Workbench"
  }
} catch {
  Write-HookJson @{
    continue = $false
    suppressOutput = $false
    stopReason = "Hash Context Workbench is not running: $($_.Exception.Message)"
  }
}
