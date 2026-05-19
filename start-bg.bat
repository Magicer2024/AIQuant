@echo off
setlocal EnableDelayedExpansion

title AIQuant Background Start

echo ========================================
echo   AIQuant Background Start (Silent)
echo ========================================
echo.

cd /d "%~dp0"

python --version >nul
if %errorlevel% neq 0 (
    echo [ERROR] Python not found.
    exit /b 1
)

netstat -ano | findstr ":5000" | findstr "LISTENING" >nul
if %errorlevel% equ 0 (
    echo Service already running.
    start http://localhost:5000/
    exit /b 0
)

echo Starting service in background...
start /min "AIQuant-Backend" pythonw app.py

timeout /t 2 >nul

echo Opening dashboard...
start http://localhost:5000/

echo Done! Service running in background.
timeout /t 3 >nul
