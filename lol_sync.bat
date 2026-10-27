@echo off
title LOL Sync Tool
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0lol_sync.ps1"
pause
exit /b
