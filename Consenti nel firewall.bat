@echo off
rem Apre la porta del server nel firewall di Windows (serve l'autorizzazione UAC)
setlocal
net session >nul 2>&1
if not %errorlevel%==0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
netsh advfirewall firewall add rule name="PptRemote 8787" dir=in action=allow protocol=TCP localport=8787
netsh advfirewall firewall add rule name="PptRemote discovery 8788" dir=in action=allow protocol=UDP localport=8788
echo.
echo Regole firewall aggiunte per la porta 8787 (TCP) e 8788 (UDP).
pause
