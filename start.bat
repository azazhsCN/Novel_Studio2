@echo off
title Novel_Studio2 - 小说写作工作台
echo.
echo ========================================
echo   Novel_Studio2 小说写作工作台
echo ========================================
echo.
echo 正在启动服务...
echo 启动后请访问: http://127.0.0.1:8000
echo 按 Ctrl+C 可停止服务
echo.
cd /d "%~dp0"
python run.py
pause
