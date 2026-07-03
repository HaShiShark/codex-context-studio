@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0codex-turn-ended-notify.ps1" %*
