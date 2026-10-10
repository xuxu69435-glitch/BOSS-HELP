@echo off
setlocal
start "" "%SystemRoot%\System32\wscript.exe" //nologo "%~dpn0.vbs"
exit /b %errorlevel%
