@echo off
pwsh -NoProfile -File "%~dp0start-local.ps1"
if errorlevel 1 (
    pause
    exit /b 1
)
start "" "http://localhost:3000/workbench"
