@echo off
setlocal

chcp 65001 >nul
cd /d "%~dp0"

if not exist "scripts\run-boss.ps1" (
    echo [ERROR] Cannot find scripts\run-boss.ps1
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run-boss.ps1"
set "BOSS_EXIT_CODE=%ERRORLEVEL%"

if not "%BOSS_EXIT_CODE%"=="0" (
    echo.
    echo BOSS Job Assistant exited with code %BOSS_EXIT_CODE%.
    pause
)

exit /b %BOSS_EXIT_CODE%
