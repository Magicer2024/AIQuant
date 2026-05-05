@echo off
setlocal EnableDelayedExpansion

title AIQuant Start

echo ========================================
echo   AIQuant Start Script
echo ========================================
echo.

:: Change to script directory
cd /d "%~dp0"

:: --- Step 1: Check Python ---
python --version >nul
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.11+ and add it to PATH.
    echo.
    pause
    exit /b 1
)
echo [1/5] Python OK
python --version

:: --- Step 2: Check dependencies ---
echo [2/5] Checking dependencies...
python -c "import flask" >nul
if %errorlevel% neq 0 (
    echo         Flask missing. Installing dependencies...
    pip install -r requirements.txt flask flask-cors schedule
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies.
        echo         Run manually: pip install -r requirements.txt flask flask-cors schedule
        pause
        exit /b 1
    )
    echo         Dependencies installed.
) else (
    echo         Dependencies OK.
)

:: --- Step 3: Stop old Flask service ---
echo [3/5] Checking for old service...
netstat -ano | findstr ":5000" | findstr "LISTENING" >nul
if %errorlevel% equ 0 (
    echo         Found old service on port 5000. Stopping...
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000" ^| findstr "LISTENING"') do (
        taskkill /F /PID %%a >nul 2>nul
        if !errorlevel! equ 0 (
            echo         Stopped process PID %%a
        ) else (
            echo         Failed to stop PID %%a (may need admin)
        )
    )
    timeout /t 1 >nul
) else (
    echo         No old service found.
)

:: Also kill any pythonw.exe that may be running app.py
tasklist | findstr "pythonw.exe" >nul
if %errorlevel% equ 0 (
    taskkill /F /IM pythonw.exe >nul 2>nul
    echo         Stopped pythonw.exe background process.
)

:: --- Step 4: Start service ---
echo [4/5] Starting Flask service...
echo         Dashboard : http://localhost:5000/dashboard
echo         Governance: http://localhost:5000/governance
echo         Agent     : http://localhost:5000/agent
echo         Market    : http://localhost:5000/market
echo         API       : http://localhost:5000/api/health
echo.
echo [5/5] Opening browser...
start http://localhost:5000/dashboard

echo ========================================
echo  Service starting... Do NOT close this window.
echo  Three Provinces ^& Six Ministries governance enabled
echo ========================================
echo.

:: Start the service
python app.py

:: If service stops
echo.
echo [INFO] Service stopped.
pause
