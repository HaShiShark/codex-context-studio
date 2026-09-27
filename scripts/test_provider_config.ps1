param([string] $PythonExe, [string] $CmdEcho, [string] $FixtureDirectory)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "codex-provider-config.ps1")

foreach ($name in @("codex-desktop-proxy.ps1", "codex-ctx-proxy.ps1", "codex-with-context.ps1")) {
  $tokens = $null
  $parseErrors = $null
  $null = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot $name), [ref] $tokens, [ref] $parseErrors)
  if ($parseErrors.Count) { throw "Invalid launcher syntax: $name" }
}

$projection = Read-CodexProviderConfig -ConfigText @'
model_provider = 'custom'
[model_providers.custom]
name = 'Custom'
base_url = 'https://provider.test/v1'
supports_standalone_web_search = true
[model_providers.custom.http_headers]
x-project = 'hello there'
x-path = 'C:\test\example'
x-shell = 'a&b %PATH% | < > ! (quoted)'
'@
$arguments = @(Get-CodexProviderOptionArgs -UpstreamInfo @{provider_cli_options=$projection.provider_cli_options} -ProviderId "codex-context-studio")
$expected = @()
foreach ($option in $projection.provider_cli_options) { $expected += @("-c", "model_providers.codex-context-studio.$option") }
$echoArgs = & $PythonExe (Join-Path $PSScriptRoot "test_provider_config.py") --echo-args @arguments
if ($LASTEXITCODE -ne 0) { throw "Argument echo failed" }
$received = $echoArgs | ConvertFrom-Json
if ($received.Count -ne $expected.Count) { throw "Argument count changed during native invocation" }
for ($i=0; $i -lt $expected.Count; $i++) {
  if ($received[$i] -cne $expected[$i]) { throw "Provider option changed during native invocation at index $i" }
}
Write-Output "ok - launcher syntax and native provider argument roundtrip"

$received = (& $CmdEcho @arguments) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $received.Count -ne $expected.Count) { throw "CMD provider argument forwarding failed" }
for ($i=0; $i -lt $expected.Count; $i++) {
  if ($received[$i] -cne $expected[$i]) { throw "CMD changed provider option at index $i" }
}
Write-Output "ok - CMD provider argument roundtrip"

# Load function definitions only; never execute the launcher's startup/actions.
$tokens = $null
$parseErrors = $null
$desktopAst = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot "codex-desktop-proxy.ps1"), [ref] $tokens, [ref] $parseErrors)
$functions = $desktopAst.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $false)
. ([scriptblock]::Create(($functions | ForEach-Object { $_.Extent.Text }) -join "`n"))
function Write-DesktopCommandShims { return @{hook_cmd='C:\fixture\hook.cmd'; notify_cmd='C:\fixture\notify.cmd'} }
function Get-ProxySnapshot { return @{data_dir=$FixtureDirectory; session_count=0; proxy_log_length=0} }
$configPath = Join-Path $FixtureDirectory 'config.toml'
$stateDir = Join-Path $FixtureDirectory 'state'
$statePath = Join-Path $stateDir 'desktop.json'
$projectRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
$providerId = 'codex-context-studio'
$loopbackHost = 'localhost'
$proxyPort = 8787
$controlPort = 8790
$profile = 'test'
@'
model_provider = 'custom'
model = 'gpt-fixture'
[model_providers.custom]
name = 'Custom'
base_url = 'https://provider.test/v1'
'@ | Set-Content -LiteralPath $configPath -Encoding UTF8
$upstream = @{provider_id='custom'; kind='third_party'; provider_options=$projection.provider_options}
Set-DesktopConfigEnabled -Mode on -RequiresOpenAiAuth $false -UpstreamInfo $upstream
Set-DesktopConfigEnabled -Mode on -RequiresOpenAiAuth $false -UpstreamInfo $upstream
