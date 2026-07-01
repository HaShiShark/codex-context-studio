$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$BuildVenv = Join-Path $Root ".build-venv"
$PythonExe = Join-Path $BuildVenv "Scripts\python.exe"
$PythonDist = Join-Path $Root "python_dist"
$WebWork = Join-Path $Root ".tmp-pyinstaller-web"
$ProxyWork = Join-Path $Root ".tmp-pyinstaller-proxy"

function Remove-WorkspacePath {
  param([Parameter(Mandatory = $true)][string]$Path)

  if (-not (Test-Path $Path)) {
    return
  }

  $ResolvedRoot = [System.IO.Path]::GetFullPath($Root)
  $ResolvedTarget = [System.IO.Path]::GetFullPath((Resolve-Path $Path))
  if (-not $ResolvedTarget.StartsWith($ResolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to remove path outside workspace: $ResolvedTarget"
  }

  Remove-Item -LiteralPath $ResolvedTarget -Recurse -Force
}

if (-not (Test-Path $PythonExe)) {
  python -m venv $BuildVenv
}

& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -r (Join-Path $Root "requirements.txt") pyinstaller

Remove-WorkspacePath $PythonDist
Remove-WorkspacePath $WebWork
Remove-WorkspacePath $ProxyWork

& $PythonExe -m PyInstaller `
  --noconfirm `
  --clean `
  --distpath $PythonDist `
  --workpath $WebWork `
  --specpath $WebWork `
  --name hash-web-server `
  (Join-Path $Root "backend\web_server.py")

& $PythonExe -m PyInstaller `
  --noconfirm `
  --clean `
  --distpath $PythonDist `
  --workpath $ProxyWork `
  --specpath $ProxyWork `
  --name hash-proxy-server `
  (Join-Path $Root "backend\proxy_fastapi.py")
