# Codex Context Studio v1.1.3

This release adds intelligent context suggestions and adapts the proxy to the latest Codex GPT-5.6 Responses Lite protocol.

## What's New

- Added automatic context suggestions after eligible Codex sessions become idle
- Added manual suggestion generation, proposal previews, and explicit apply or dismiss actions
- Added transcript-version checks so stale suggestions cannot overwrite newer conversation state
- Added configurable suggestion timing and desktop notifications for newly generated proposals
- Added Responses Lite protocol detection through the canonical `additional_tools` request item
- Preserved GPT-5.6 developer instructions, tool definitions, unknown provider items, and top-level request fields during proxy reconstruction
- Added Responses Lite-aware compact simulation and readable structured displays for additional tools
- Improved assistant activity continuity, runtime layout isolation, and packaged-app branding

## Download

Download and run:

```text
Codex Context Studio Setup 1.1.3.exe
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
