@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install_env.ps1"
set "BOSS_SETUP_EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %BOSS_SETUP_EXIT_CODE%
