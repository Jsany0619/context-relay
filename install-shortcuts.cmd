@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-shortcuts.ps1" %*
set "exitCode=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %exitCode%
