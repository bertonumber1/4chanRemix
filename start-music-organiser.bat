@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
start "" /b "%~dp0.venv\Scripts\python.exe" web_ui.py >> "%~dp0run.log" 2>&1

rem Wait for the server to actually answer before opening the browser --
rem otherwise the first tab load races the app and shows a connection error.
for /l %%i in (1,1,30) do (
    curl.exe -s -o nul -w "%%{http_code}" http://127.0.0.1:8082/api/health > "%TEMP%\mo-health.txt" 2>nul
    set /p MO_CODE=<"%TEMP%\mo-health.txt"
    if "!MO_CODE!"=="200" goto :up
    timeout /t 1 /nobreak >nul
)
:up
del "%TEMP%\mo-health.txt" >nul 2>&1
start "" http://127.0.0.1:8082/
