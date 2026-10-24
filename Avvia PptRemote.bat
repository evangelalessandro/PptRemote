@echo off
rem Avvia PptRemote senza finestra nera usando pythonw (se disponibile)
setlocal
cd /d "%~dp0server"

where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw ppremote.py
) else (
    start "" python ppremote.py
)
endlocal
