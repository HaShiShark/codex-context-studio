!macro customInstall
  DetailPrint "Installing codex ctx proxy command shim..."
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\app\scripts\codex-ctx-proxy.ps1" install'
  Pop $0
  DetailPrint "codex ctx proxy command shim setup result: $0"
!macroend

!macro customUnInstall
  DetailPrint "Removing codex ctx proxy command shim..."
  nsExec::ExecToLog 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\resources\app\scripts\codex-ctx-proxy.ps1" uninstall'
  Pop $0
  DetailPrint "codex ctx proxy command shim removal result: $0"
!macroend
