@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo   music-organiser - Windows setup
echo.

where py >nul 2>&1
if errorlevel 1 (
    echo   ERROR: Python not found. Install Python 3.10+ from python.org
    echo   ^(check "Add python.exe to PATH" during install^), then re-run this.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo   Creating virtual environment in .venv\ ...
    py -m venv .venv
    if errorlevel 1 (
        echo   ERROR: venv creation failed.
        pause
        exit /b 1
    )
) else (
    echo   .venv already exists, reusing it.
)

echo   Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo   ERROR: dependency install failed - see the output above.
    pause
    exit /b 1
)

echo.
echo   Done. Start the app with start-music-organiser.bat
echo   (fingerprint comparison needs fpcalc.exe on PATH too - see README.md)
echo.
pause
