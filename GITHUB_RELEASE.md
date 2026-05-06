# Codex Context Proxy v0.1.0

A visual, editable context layer for Codex. Let AI edit AI's context with surgical precision, giving you more control and freedom over what Codex sees.

## What's Included

- Visual context map for Codex sessions
- Token overview for current context usage
- Manual context inspection and editing panel
- AI-assisted context compression for noisy tool output
- Codex CLI support through `codex ctx proxy on/off/status`
- Experimental Codex Desktop support through `codex ctx desktop on/off/status`
- Windows installer with bundled Electron app and Python backend

## Download

Download and run:

```text
Codex Context Proxy Setup 0.1.0.exe
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
- It does not modify the official Codex CLI source code.
- CLI support is the primary path.
- Codex Desktop support is experimental because it modifies local Codex configuration.
