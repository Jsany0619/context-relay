@echo off
cd /d "%~dp0"
if errorlevel 1 (
    pause
    exit /b 1
)
python -m relay %*
if errorlevel 1 (
    echo Context Relay could not start. Run: python -m relay --check
    pause
    exit /b 1
)
