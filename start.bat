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
    pip install -r requirements.txt
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install dependencies.
        echo         Run manually: pip install -r requirements.txt
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
echo         Dashboard : http://localhost:5000/
echo         API       : http://localhost:5000/api/health
echo.
echo [5/5] Service starting, browser will open when ready...

echo ========================================
echo  Service starting... Do NOT close this window.
echo  AIQuant 个人股票评分系统
echo ========================================
echo.

:: 后台等待端口 5000 就绪后再打开浏览器（最多等待 60 秒）
:: 使用 TcpClient 探测本地 5000 端口，连通即说明 Flask 已启动
start "" /b powershell -WindowStyle Hidden -Command "for($i=0;$i -lt 60;$i++){Start-Sleep -Seconds 1; try{$c=New-Object System.Net.Sockets.TcpClient('localhost',5000); $c.Close(); Start-Process 'http://localhost:5000/'; break}catch{}}"

:: 前台启动 Flask 服务（关闭本窗口即停止服务）
python app.py

:: If service stops
echo.
echo [INFO] Service stopped.
pause
