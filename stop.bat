@echo off
:: Self-elevate to admin if not already
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

title Novel_Studio2 - Stop
echo.
echo ========================================
echo   Novel_Studio2 - Stop Service
echo ========================================
echo.

echo [1] Killing processes on port 8000 ...
set "found=0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000 "') do (
    if not "%%p"=="0" (
        tasklist /FI "PID eq %%p" 2>nul | findstr /I "python" >nul
        if not errorlevel 1 (
            set "found=1"
            echo    Killing python PID %%p ...
            taskkill /F /PID %%p
        )
    )
)
if "%found%"=="0" (
    echo    No python process found on port 8000.
)

echo.
echo [2] Cleaning __pycache__ ...
cd /d "%~dp0"
set "cleaned=0"
for /d /r %%d in (__pycache__) do (
    rd /s /q "%%d" 2>nul
    set /a cleaned+=1
)
echo    Cleaned %cleaned% directories.

echo.
echo ========================================
echo   Done!
echo ========================================
echo.
pause
