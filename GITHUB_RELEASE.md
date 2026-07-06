# Codex Context Studio v1.1.0

A visual, editable context layer for Codex. This release focuses on the Context Studio identity, clearer bilingual documentation, and safer transcript rebuilding for newer Codex request metadata.

## What's New

- Renamed the user-facing project identity to Codex Context Studio
- Reworked the English and Chinese READMEs with updated feature descriptions, architecture diagrams, and screenshots
- Moved screenshots into language-specific folders for clearer documentation
- Preserved Codex internal turn metadata and turn IDs when rebuilding request input
- Added support for `custom_tool_call.namespace` during response-to-request projection
- Improved compact request metadata detection across differently cased client metadata keys
- Localized grouped tool-call labels in the context workbench
- Expanded proxy core and cursor delta tests for metadata and request rebuild behavior

## Download

Download and run:

```text
Codex Context Proxy Setup 1.1.0.exe
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
