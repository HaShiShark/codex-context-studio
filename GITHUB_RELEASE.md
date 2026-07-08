# Codex Context Studio v1.1.2

This release improves runtime port handling across the packaged app, CLI launcher, Desktop proxy, Vite development server, and frontend realtime connection.

## What's New

- Exposed runtime proxy settings in the web bootstrap payload
- Updated the frontend realtime WebSocket URL to use the runtime proxy port
- Routed proxy usage reset through the backend facade endpoint
- Made Vite dev proxy targets respect `HASH_CONTEXT_HOST`, `HASH_WEB_PORT`, and `HASH_CONTEXT_PROXY_PORT`
- Propagated runtime host, proxy, backend, and control ports into packaged Electron services
- Updated CLI and Desktop scripts to avoid hardcoded backend/frontend ports
- Moved hook and notify logs into the user `.hash-context-codex/logs` directory
- Added Brotli to packaged Python dependency validation
- Improved Desktop service snapshot and cleanup behavior for custom data and port settings

## Download

Download and run:

```text
Codex Context Proxy Setup 1.1.2.exe
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
