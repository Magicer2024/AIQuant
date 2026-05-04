@echo off
setlocal EnableDelayedExpansion

title AIQuant Dependency Installer

echo ========================================
echo   AIQuant Dependency Installer
echo ========================================
echo.

cd /d "%~dp0"

python --version >nul
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.11+ first.
    pause
    exit /b 1
)

echo [1/2] Upgrading pip...
python -m pip install --upgrade pip

echo [2/2] Installing dependencies...
pip install -r requirements.txt flask flask-cors schedule requests pandas plotly

echo.
echo ========================================
echo   Installation complete!
echo   Now run start.bat to launch the system.
echo ========================================
pause
