@echo off
rem Lanza el editor sin ventana de consola de por medio.
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m app.main %*
