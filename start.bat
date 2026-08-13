@echo off
title Novel_Studio2 - Start
cd /d "%~dp0"

echo ========================================
echo   Novel_Studio2 - Start
echo ========================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo   Please install Python 3.10+ from https://www.python.org/downloads/
    echo   IMPORTANT: check "Add python.exe to PATH" during installation.
    echo.
    pause
    exit /b 1
)

python -c "import fastapi, uvicorn, httpx, pydantic, yaml, jinja2, multipart" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing dependencies, first run only, please wait...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Dependency installation failed. Check your network and retry.
        echo.
        pause
        exit /b 1
    )
)

if not exist config.yaml (
    echo [INFO] config.yaml not found. Creating from config.example.yaml ...
    copy config.example.yaml config.yaml >nul
    echo [INFO] Notepad will open. Please fill in your API key in config.yaml,
    echo        save and close it, then run start.bat again.
    start notepad config.yaml
    pause
    exit /b 0
)

echo Starting server at http://127.0.0.1:8000
echo To stop: press Ctrl+C in this window, or double-click stop.bat
echo.
python run.py
pause
