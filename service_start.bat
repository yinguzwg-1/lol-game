@echo off
title 5v5 Draw Service
cd /d "%~dp0"

rem --- Auto-heal: install flask if missing ---
python -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo flask missing, auto installing...
    python -m pip install flask --quiet --timeout 60 -i https://mirrors.aliyun.com/pypi/simple/ >nul 2>nul
    python -c "import flask" >nul 2>nul
    if errorlevel 1 python -m pip install flask --quiet --timeout 60 >nul 2>nul
)
python -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found or flask install failed.
    echo Please check Python is in PATH.
    pause
    exit /b 1
)

echo Service starting on port 9529 ...
echo Keep this window open while using the app.
python app.py
pause