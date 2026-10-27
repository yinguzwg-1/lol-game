@echo off
title LOL LCU Diagnose 9
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0diagnose9.ps1"
pause
exit /b
