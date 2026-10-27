@echo off
title LOL LCU Diagnose 10
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnose10.ps1"
pause
exit /b
