# Codex Context Studio v1.1.1

## Highlights

- Migrated Windows packaging into the dedicated `packaging/` pipeline
- Bundled an embedded Python runtime with the Electron app
- Updated packaged backend startup to run Python modules directly
- Improved installed hook and notification shim resolution
- Updated documentation to use `npm run package:win`

## Verification

- `npm test`
- `npm run package:win`
