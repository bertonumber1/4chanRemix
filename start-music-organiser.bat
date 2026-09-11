@echo off
cd /d "%~dp0"
start "" /b "%~dp0.venv\Scripts\python.exe" web_ui.py >> "%~dp0run.log" 2>&1
