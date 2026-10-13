@echo off
echo.
echo  YT-Downloader - Quick Start
echo  ============================
echo.

:: Check Python
where python >nul 2>&1 || (echo  [ERROR] Python not found. Install from https://python.org && pause && exit /b 1)

:: Create venv if missing
if not exist ".venv" (
    echo  Creating virtual environment...
    python -m venv .venv
)

:: Activate
call .venv\Scripts\activate.bat

:: Install / upgrade deps
echo  Installing dependencies...
pip install -q -r requirements.txt

echo.
echo  Starting server at http://127.0.0.1:5000
echo  Press Ctrl+C to stop.
echo.
start "" http://127.0.0.1:5000
python app.py
pause
