param(
  [switch]$SkipBuild
)

$ErrorActionPreference = "Continue"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..\")
$results = New-Object System.Collections.Generic.List[object]

function Add-Result {
  param(
    [string]$Name,
    [bool]$Passed,
    [string]$Command,
    [string]$Notes
  )

  $results.Add([pscustomobject]@{
    name = $Name
    passed = $Passed
    command = $Command
    notes = $Notes
  }) | Out-Null
}

function Run-Check {
  param(
    [string]$Name,
    [string]$Command
  )

  Write-Host ""
  Write-Host "== $Name =="
  Write-Host $Command
  Push-Location $root
  try {
    Invoke-Expression $Command
    $exitCode = if ($LASTEXITCODE -eq $null) { 0 } else { $LASTEXITCODE }
    Add-Result -Name $Name -Passed ($exitCode -eq 0) -Command $Command -Notes "exit code $exitCode"
  } catch {
    Add-Result -Name $Name -Passed $false -Command $Command -Notes $_.Exception.Message
  } finally {
    Pop-Location
  }
}

Write-Host "Long Context 15-Task Benchmark final check"
Write-Host "Root: $root"

Run-Check -Name "TypeScript typecheck" -Command "npm run typecheck"
Run-Check -Name "Compact proxy tests" -Command "npm run test:compact-proxy"

if (-not $SkipBuild) {
  Run-Check -Name "React build" -Command "npm run build:react"
}

Write-Host ""
Write-Host "== Summary =="
$passedCount = 0
foreach ($result in $results) {
  $status = if ($result.passed) { "PASS" } else { "FAIL" }
  if ($result.passed) {
    $passedCount += 1
  }
  Write-Host ("{0} {1} - {2}" -f $status, $result.name, $result.notes)
}

Write-Host ""
Write-Host ("Checks passed: {0}/{1}" -f $passedCount, $results.Count)

if ($passedCount -ne $results.Count) {
  exit 1
}

exit 0
