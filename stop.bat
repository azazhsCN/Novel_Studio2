@echo off
title Novel_Studio2 - Stop

set "PIDFILE=%~dp0data\server.pid"
if not exist "%PIDFILE%" (
    echo [INFO] PID file not found: %PIDFILE%
    echo        The service may not be running, or was started by an older version.
    goto :clean
)

set /p PID=<"%PIDFILE%"
if "%PID%"=="" (
    echo [WARN] PID file is empty.
    goto :clean
)

echo [1] Killing process tree of PID %PID% ...
taskkill /F /T /PID %PID% >nul 2>&1
if errorlevel 1 (
    echo     Process %PID% not running or already stopped.
) else (
    echo     Stopped.
)
del "%PIDFILE%" >nul 2>&1

:clean
echo.
echo [2] Cleaning __pycache__ ...
cd /d "%~dp0"
set "cleaned=0"
for /d /r %%d in (__pycache__) do (
    rd /s /q "%%d" 2>nul
    set /a cleaned+=1
)
echo     Cleaned %cleaned% directories.

echo.
echo Done! You can close this window.
pause
