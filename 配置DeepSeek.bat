@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist "venvs\boss-agent-cli\Scripts\python.exe" (
    echo [ERROR] Project Python environment not found.
    pause
    exit /b 1
)

"venvs\boss-agent-cli\Scripts\python.exe" -X utf8 "scripts\configure_deepseek.py"
set "SETUP_EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %SETUP_EXIT_CODE%
