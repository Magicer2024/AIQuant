@echo off
setlocal EnableDelayedExpansion

title AIQuant Start

echo ========================================
echo   AIQuant Start Script
echo ========================================
echo.

:: Change to script directory
cd /d "%~dp0"

:: --- Step 1: Check Python (3.11+) + show the actual interpreter in use ---
:: 打印 sys.executable：虚拟环境未激活/多个 Python 串 PATH 时，一眼看出用的是哪个解释器
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.11+ and add it to PATH.
    echo.
    pause
    exit /b 1
)
set "PY_EXE="
for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do set "PY_EXE=%%i"
echo [1/5] Python OK
python --version
echo         Interpreter: %PY_EXE%

:: 版本闸门：项目要求 3.11+（tomllib/异常组等语法与依赖下限）
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
    echo [ERROR] Python 3.11+ required, but the interpreter above is older.
    echo         If you have a virtualenv, activate it before running this script.
    pause
    exit /b 1
)

:: pip 必须与当前 python 同源（避免 pip 装进别的环境、python 却 import 不到）
python -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] pip is not available for: %PY_EXE%
    echo         Fix it with:  python -m ensurepip --upgrade
    pause
    exit /b 1
)

:: --- Step 2: Check dependencies (probe all runtime packages, not just flask) ---
:: 输出 ALL_OK 或 MISSING:pkg1,pkg2（scikit-learn 的导入名是 sklearn）
echo [2/5] Checking dependencies...
set "DEP_LINE="
for /f "delims=" %%i in ('python -c "import importlib.util,sys; mods=['flask','flask_cors','schedule','akshare','baostock','requests','pandas','numpy','sklearn','joblib']; miss=[m for m in mods if importlib.util.find_spec(m) is None]; print('ALL_OK' if not miss else 'MISSING:'+','.join(miss)); sys.exit(1 if miss else 0)"') do set "DEP_LINE=%%i"
echo !DEP_LINE! | findstr /b "ALL_OK" >nul
if %errorlevel% equ 0 (
    echo         Dependencies OK.
) else (
    echo         Missing: !DEP_LINE:MISSING:=!
    echo         Installing dependencies via "%PY_EXE%" ...
    :: 用 python -m pip 保证装进上面显示的同一个解释器
    python -m pip install -r requirements.txt -c constraints.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        echo         Run manually:  "%PY_EXE%" -m pip install -r requirements.txt -c constraints.txt
        pause
        exit /b 1
    )
    :: 安装后复检：仍缺包则明确报出缺谁，而不是带着残缺依赖启动
    set "DEP_LINE="
    for /f "delims=" %%i in ('python -c "import importlib.util,sys; mods=['flask','flask_cors','schedule','akshare','baostock','requests','pandas','numpy','sklearn','joblib']; miss=[m for m in mods if importlib.util.find_spec(m) is None]; print('ALL_OK' if not miss else 'MISSING:'+','.join(miss)); sys.exit(1 if miss else 0)"') do set "DEP_LINE=%%i"
    echo !DEP_LINE! | findstr /b "ALL_OK" >nul
    if errorlevel 1 (
        echo [ERROR] Still missing after install: !DEP_LINE:MISSING:=!
        echo         Check network / pip mirror, then run the install command manually.
        pause
        exit /b 1
    )
    echo         Dependencies installed and verified.
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
