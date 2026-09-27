function Read-CodexProviderConfig {
  param([string] $ConfigText)

  $projectDir = Split-Path -Parent $PSScriptRoot
  $candidates = @(
    $env:CODEX_CONTEXT_STUDIO_PYTHON,
    (Join-Path $projectDir "python-runtime\python.exe"),
    (Join-Path $projectDir ".venv\Scripts\python.exe")
  )
  $pythonCommand = ""
  $pythonArgs = @()
  foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) {
      $pythonCommand = $candidate
      break
    }
  }
  if (-not $pythonCommand) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
      $pythonCommand = "py"
      $pythonArgs = @("-3")
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
      $pythonCommand = "python"
    } else {
      throw "Python 3.11+ is required to read Codex configuration. Run npm run setup:python."
    }
  }

  # Function-local encoding: Windows PowerShell otherwise sends ASCII to stdin.
  $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
  $result = $ConfigText | & $pythonCommand @pythonArgs (Join-Path $PSScriptRoot "codex_provider_config.py")
  if ($LASTEXITCODE -ne 0) { throw "Cannot read Codex provider configuration." }
  return ($result | ConvertFrom-Json)
}

function Get-CodexProviderOptionArgs {
  param([hashtable] $UpstreamInfo, [string] $ProviderId)
  foreach ($option in $UpstreamInfo.provider_cli_options) {
    "-c"
    $argument = "model_providers.$ProviderId.$option"
    if ($PSVersionTable.PSVersion.Major -lt 7 -or $PSNativeCommandArgumentPassing -eq "Legacy") {
      # Windows PowerShell's legacy native argument binder consumes unescaped
      # double quotes. Preserve TOML strings and backslashes for the child.
      $argument = [regex]::Replace($argument, '(\\*)"', {
        param($match)
        ('\' * ($match.Groups[1].Value.Length * 2 + 1)) + '"'
      })
    }
    $argument
  }
}
