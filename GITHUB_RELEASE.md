# Codex Context Studio v1.1.1

This release updates the Windows packaging pipeline and makes the installed app use a bundled embedded Python runtime instead of the previous PyInstaller server executables.

## What's New

- Replaced the old PyInstaller packaging path with a dedicated `packaging/` build pipeline
- Bundled an embedded Python runtime into the Electron app package
- Updated Electron startup to run backend services through `python -m backend.web_server` and `python -m backend.proxy_fastapi`
- Moved NSIS installer customization into `packaging/windows/installer.nsh`
- Updated CLI and Desktop shims so hooks and notifications resolve through the installed shim directory
- Improved Desktop config repair for managed notify commands
- Redirected packaged Electron window logs to the user state log directory
- Updated documentation to use `npm run package:win`

## Download

Download and run:

```text
Codex Context Proxy Setup 1.1.1.exe
```

After installation, open a new terminal and enable the proxy:

```powershell
codex ctx proxy on
```

Then use Codex normally:

```powershell
codex
```

Disable anytime:

```powershell
codex ctx proxy off
```

## Notes

- This project does not replace Codex.
- CLI support is the primary path.
- Codex Desktop support is experimental because it modifies local Codex configuration.
