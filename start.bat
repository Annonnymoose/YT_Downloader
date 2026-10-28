@echo off
setlocal EnableDelayedExpansion

:: Ensure the script always runs inside its own folder
cd /d "%~dp0"

echo.
echo  YT-Downloader - Quick Start
echo  ============================
echo.

:: Detect working Python command (handles global python and py launcher, avoids Windows Store stub)
set "PY_CMD="
python -c "import sys; sys.exit(0)" >nul 2>&1
if !errorlevel! equ 0 (
    set "PY_CMD=python"
) else (
    py -3 -c "import sys; sys.exit(0)" >nul 2>&1
    if !errorlevel! equ 0 (
        set "PY_CMD=py -3"
    )
)

if "%PY_CMD%"=="" (
    echo  [ERROR] Python is not accessible in this environment.
    echo  Make sure Python is installed from https://python.org and "Add Python to PATH" is checked.
    pause
    exit /b 1
)

echo  Using Python: %PY_CMD%

:: Verify existing .venv is valid; if broken or missing, (re)create it
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if !errorlevel! neq 0 (
        echo  Detected broken virtual environment. Recreating .venv...
        rmdir /s /q ".venv"
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo  Creating virtual environment...
    %PY_CMD% -m venv .venv
    if !errorlevel! neq 0 (
        echo  [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

:: Use the venv Python directly to avoid activation PATH issues
set "VENV_PY=.venv\Scripts\python.exe"

:: Install / upgrade dependencies
echo  Installing dependencies...
"%VENV_PY%" -m pip install --upgrade pip -q
"%VENV_PY%" -m pip install -q -r requirements.txt
if !errorlevel! neq 0 (
    echo  [ERROR] Failed to install dependencies from requirements.txt.
    pause
    exit /b 1
)

echo.
echo  Starting server at http://127.0.0.1:5000
echo  Press Ctrl+C to stop.
echo.

start "" http://127.0.0.1:5000
"%VENV_PY%" app.py
pause