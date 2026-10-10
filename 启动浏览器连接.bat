@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "scripts\start_browser_bridge.ps1"
set "BRIDGE_EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %BRIDGE_EXIT_CODE%
