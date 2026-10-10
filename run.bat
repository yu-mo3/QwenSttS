@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title 阿源 · M1 环境自检

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [!] 未找到虚拟环境 .venv
    echo     请先双击运行 setup.bat 完成初始化
    echo.
    pause
    exit /b 1
)

"%PY%" tools\check_env.py %*

echo.
pause
