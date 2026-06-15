param(
  [switch] $Reset
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$VenvDir = Join-Path $Root ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
$Requirements = Join-Path $Root "requirements.txt"

function Test-CondaPath {
  param([string] $Value)
  if (-not $Value) { return $false }
  return $Value -match "(?i)(^|[\\/])(anaconda|miniconda|conda)([\\/]|$)"
}

function Get-PythonInfo {
  param(
    [string] $Command,
    [string[]] $CommandArgs = @()
  )

  $code = "import sys; print(sys.executable); print(getattr(sys, '_base_executable', '')); print(sys.prefix); print(sys.base_prefix); print('.'.join(map(str, sys.version_info[:3])))"
  try {
    $allArgs = @($CommandArgs) + @("-c", $code)
    $output = @(& $Command @allArgs 2>$null)
    if ($LASTEXITCODE -ne 0 -or $output.Count -lt 5) { return $null }
    return [pscustomobject] @{
      executable = [string] $output[0]
      base_executable = [string] $output[1]
      prefix = [string] $output[2]
      base_prefix = [string] $output[3]
      version = @(([string] $output[4]).Split('.') | ForEach-Object { [int] $_ })
    }
  } catch {
    return $null
  }
}

function Test-CondaPythonInfo {
  param($Info)
  if (-not $Info) { return $true }
  return (
    (Test-CondaPath ([string] $Info.executable)) -or
    (Test-CondaPath ([string] $Info.base_executable)) -or
    (Test-CondaPath ([string] $Info.prefix)) -or
    (Test-CondaPath ([string] $Info.base_prefix))
  )
}

function Get-VenvCfgHome {
  $cfg = Join-Path $VenvDir "pyvenv.cfg"
  if (-not (Test-Path $cfg)) { return "" }
  foreach ($line in Get-Content -Path $cfg) {
    if ($line -match '^\s*home\s*=\s*(.+?)\s*$') {
      return [string] $Matches[1]
    }
  }
  return ""
}

function Get-PyLauncherPythonPaths {
  $paths = @()
  try {
    $lines = & py -0p 2>$null
  } catch {
    return $paths
  }
  foreach ($line in $lines) {
    $match = [regex]::Match([string] $line, '([A-Za-z]:\\.*?python\.exe)\s*$')
    if ($match.Success) {
      $paths += [string] $match.Groups[1].Value
    }
  }
  return $paths
}

function Find-ProjectPython {
  $candidates = @()
  if ($env:PYTHON) {
    $candidates += [pscustomobject] @{ Command = $env:PYTHON; Args = @() }
  }
  foreach ($pythonPath in Get-PyLauncherPythonPaths) {
    $candidates += [pscustomobject] @{ Command = $pythonPath; Args = @() }
  }
  if ($IsWindows -or $env:OS -eq "Windows_NT") {
    $candidates += [pscustomobject] @{ Command = "py"; Args = @("-3.13") }
    $candidates += [pscustomobject] @{ Command = "py"; Args = @("-3.12") }
    $candidates += [pscustomobject] @{ Command = "py"; Args = @("-3.11") }
    $candidates += [pscustomobject] @{ Command = "py"; Args = @("-3.10") }
    $candidates += [pscustomobject] @{ Command = "py"; Args = @("-3") }
    $candidates += [pscustomobject] @{ Command = "python"; Args = @() }
  } else {
    $candidates += [pscustomobject] @{ Command = "python3"; Args = @() }
    $candidates += [pscustomobject] @{ Command = "python"; Args = @() }
  }

  foreach ($candidate in $candidates) {
    $command = [string] $candidate.Command
    $commandArgs = [string[]] $candidate.Args
    $info = Get-PythonInfo -Command $command -CommandArgs $commandArgs
    if (-not $info) { continue }
    if (Test-CondaPythonInfo $info) { continue }
    $version = @($info.version)
    if ($version.Count -ge 2 -and ([int] $version[0] -gt 3 -or ([int] $version[0] -eq 3 -and [int] $version[1] -ge 10))) {
      return @{ command = $command; args = $commandArgs; info = $info }
    }
  }

  Write-Host "[setup:python] Checked Python candidates:" -ForegroundColor DarkYellow
  foreach ($candidate in $candidates) {
    $command = [string] $candidate.Command
    $commandArgs = [string[]] $candidate.Args
    $info = Get-PythonInfo -Command $command -CommandArgs $commandArgs
    if (-not $info) {
      Write-Host "[setup:python]   $command $($commandArgs -join ' ') -> not usable" -ForegroundColor DarkYellow
    } elseif (Test-CondaPythonInfo $info) {
      Write-Host "[setup:python]   $command $($commandArgs -join ' ') -> rejected conda base $($info.base_prefix)" -ForegroundColor DarkYellow
    } else {
      Write-Host "[setup:python]   $command $($commandArgs -join ' ') -> version $($info.version -join '.')" -ForegroundColor DarkYellow
    }
  }
  throw "No suitable non-conda Python >= 3.10 found. Install Python from python.org, or set PYTHON to an official Python executable, then run npm run setup:python again."
}

function Backup-CondaVenv {
  if (-not (Test-Path $VenvDir)) { return }
  $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $backup = Join-Path $Root ".venv-conda-backup-$timestamp"
  Rename-Item -LiteralPath $VenvDir -NewName (Split-Path -Leaf $backup)
  Write-Host "[setup:python] moved conda-based .venv to $backup" -ForegroundColor DarkYellow
}

if (Test-Path $VenvPython) {
  $existingInfo = Get-PythonInfo -Command $VenvPython
  if (Test-CondaPythonInfo $existingInfo) {
    if (-not $Reset) {
      $venvHome = Get-VenvCfgHome
      Write-Host "[setup:python] Existing .venv is based on conda/anaconda:" -ForegroundColor Red
      Write-Host "[setup:python] pyvenv.cfg home = $venvHome" -ForegroundColor Red
      Write-Host "[setup:python] Run npm run setup:python:reset to replace it with a project-owned non-conda .venv." -ForegroundColor Red
      throw "Existing .venv is conda-based."
    }
    Backup-CondaVenv
  }
}

if (-not (Test-Path $VenvPython)) {
  $python = Find-ProjectPython
  Write-Host "[setup:python] creating .venv with $($python.info.executable)"
  $venvArgs = @($python.args) + @("-m", "venv", $VenvDir)
  & $python.command @venvArgs
}

$finalInfo = Get-PythonInfo -Command $VenvPython
if (Test-CondaPythonInfo $finalInfo) {
  throw "Created .venv is still conda-based ($($finalInfo.base_prefix)); refusing to continue."
}

Write-Host "[setup:python] using $($finalInfo.executable)"
Write-Host "[setup:python] base $($finalInfo.base_prefix)"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r $Requirements
Write-Host "[setup:python] project Python environment is ready" -ForegroundColor Green
