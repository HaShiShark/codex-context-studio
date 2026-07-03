param(
  [Parameter(ValueFromRemainingArguments = $true)]
  [string[]] $ForwardArgs
)

$ErrorActionPreference = "Continue"

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

function Get-ProjectRoot {
  return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Get-LogDir {
  $root = Get-ProjectRoot
  $logDir = Join-Path $root "logs"
  New-Item -ItemType Directory -Force -Path $logDir | Out-Null
  return $logDir
}

function Write-NotifyLog {
  param([string] $Message)
  try {
    Add-Content -Path (Join-Path (Get-LogDir) "codex-context-hook.log") -Value "$((Get-Date).ToUniversalTime().ToString("o")) $Message" -Encoding UTF8
  } catch {
  }
}

function Get-MainTurnStatePath {
  try {
    return (Join-Path (Get-LogDir) "codex-main-turn.json")
  } catch {
    return ""
  }
}

function Find-CodexSessionId {
  param([object] $Value)

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
      if ($keyText -match "session|conversation|thread|window|request") {
        $found = Find-CodexSessionId -Value $Value[$key]
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

  foreach ($property in $Value.PSObject.Properties) {
    if ($property.Name -match "session|conversation|thread|window|request") {
      $found = Find-CodexSessionId -Value $property.Value
      if ($found) {
        return $found
      }
    }
  }
  foreach ($property in $Value.PSObject.Properties) {
    $found = Find-CodexSessionId -Value $property.Value
    if ($found) {
      return $found
    }
  }

  return ""
}

function Find-TurnId {
  param([object] $Value)

  if ($null -eq $Value) {
    return ""
  }
  if ($Value -is [System.Collections.IDictionary]) {
    foreach ($key in @("turn_id", "turnId")) {
      if ($Value.Contains($key)) {
        $candidate = ([string] $Value[$key]).Trim()
        if ($candidate) {
          return $candidate
        }
      }
    }
    foreach ($key in $Value.Keys) {
      $found = Find-TurnId -Value $Value[$key]
      if ($found) {
        return $found
      }
    }
    return ""
  }
  foreach ($property in $Value.PSObject.Properties) {
    if ($property.Name -in @("turn_id", "turnId")) {
      $candidate = ([string] $property.Value).Trim()
      if ($candidate) {
        return $candidate
      }
    }
  }
  foreach ($property in $Value.PSObject.Properties) {
    $found = Find-TurnId -Value $property.Value
    if ($found) {
      return $found
    }
  }
  return ""
}

function Read-LastActiveMainTurn {
  $path = Get-MainTurnStatePath
  if (-not $path -or -not (Test-Path $path)) {
    return $null
  }

  try {
    $state = Get-Content -Raw -Path $path -Encoding UTF8 | ConvertFrom-Json
  } catch {
    return $null
  }

  if ($state.active_turns) {
    $turns = @($state.active_turns)
    if ($turns.Count -gt 0) {
      return $turns[$turns.Count - 1]
    }
  }
  if ($state.session_id) {
    return $state
  }
  return $null
}

function Clear-ActiveMainTurn {
  param(
    [string] $SessionId,
    [string] $TurnId
  )

  $path = Get-MainTurnStatePath
  if (-not $path -or -not (Test-Path $path)) {
    return
  }

  if (-not $SessionId) {
    Remove-Item -Path $path -Force -ErrorAction SilentlyContinue
    return
  }

  try {
    $state = Get-Content -Raw -Path $path -Encoding UTF8 | ConvertFrom-Json
    if ($state.active_turns) {
      $remaining = @(
        foreach ($turn in @($state.active_turns)) {
          $turnSessionId = ([string] $turn.session_id).Trim()
          $turnTurnId = ([string] $turn.turn_id).Trim()
          if ($turnSessionId -ne $SessionId -or ($TurnId -and $turnTurnId -ne $TurnId)) {
            $turn
          }
        }
      )
      if ($remaining.Count -gt 0) {
        @{ active_turns = $remaining; updated_at = (Get-Date).ToUniversalTime().ToString("o") } |
          ConvertTo-Json -Compress -Depth 6 |
          Set-Content -Path $path -Encoding UTF8
      } else {
        Remove-Item -Path $path -Force -ErrorAction SilentlyContinue
      }
      return
    }
    $stateSessionId = ([string] $state.session_id).Trim()
    $stateTurnId = ([string] $state.turn_id).Trim()
    if ($stateSessionId -eq $SessionId -and ((-not $TurnId) -or $stateTurnId -eq $TurnId)) {
      Remove-Item -Path $path -Force -ErrorAction SilentlyContinue
    }
  } catch {
    Write-NotifyLog "main-turn state clear failed session_id=$SessionId turn_id=$TurnId error=$($_.Exception.Message)"
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
    Write-NotifyLog "main-turn state ok session_id=$SessionId turn_id=$TurnId running=$Running"
  } catch {
    Write-NotifyLog "main-turn state failed session_id=$SessionId turn_id=$TurnId running=$Running error=$($_.Exception.Message)"
  }
}

function Find-CodexComputerUseNotify {
  try {
    $root = Join-Path $env:LOCALAPPDATA "OpenAI\Codex\runtimes\cua_node"
    if (-not (Test-Path $root)) {
      return ""
    }
    $candidate = Get-ChildItem -Path $root -Filter "codex-computer-use.exe" -Recurse -ErrorAction SilentlyContinue |
      Sort-Object LastWriteTimeUtc -Descending |
      Select-Object -First 1
    if ($candidate) {
      return $candidate.FullName
    }
  } catch {
  }
  return ""
}

function Invoke-ForwardNotify {
  param(
    [string[]] $ArgsToForward,
    [string] $InputText = ""
  )

  $command = ""
  $arguments = @()
  if ($ArgsToForward -and $ArgsToForward.Count -gt 0) {
    $command = $ArgsToForward[0]
    if ($ArgsToForward.Count -gt 1) {
      $arguments = @($ArgsToForward[1..($ArgsToForward.Count - 1)])
    }
  } else {
    $command = Find-CodexComputerUseNotify
    if ($command) {
      $arguments = @("turn-ended")
    }
  }

  if (-not $command) {
    return
  }

  try {
    if ($InputText) {
      $InputText | & $command @arguments | Out-Null
    } else {
      & $command @arguments | Out-Null
    }
  } catch {
    Write-NotifyLog "forward notify failed command=$command error=$($_.Exception.Message)"
  }
}

$raw = ""
try {
  $raw = ([Console]::In.ReadToEnd()).TrimStart([char] 0xFEFF)
} catch {
}

$inputPayload = $null
if ($raw.Trim()) {
  try {
    $inputPayload = $raw | ConvertFrom-Json
  } catch {
    Write-NotifyLog "notify invalid-json raw=$($raw.Substring(0, [Math]::Min(1000, $raw.Length)))"
  }
}

$sessionId = Find-CodexSessionId -Value $inputPayload
$turnId = Find-TurnId -Value $inputPayload

if (-not $sessionId) {
  $activeTurn = Read-LastActiveMainTurn
  if ($activeTurn) {
    $sessionId = ([string] $activeTurn.session_id).Trim()
    if (-not $turnId) {
      $turnId = ([string] $activeTurn.turn_id).Trim()
    }
  }
}

Set-MainTurnState -SessionId $sessionId -TurnId $turnId -Running $false
Clear-ActiveMainTurn -SessionId $sessionId -TurnId $turnId
Invoke-ForwardNotify -ArgsToForward $ForwardArgs -InputText $raw

exit 0
