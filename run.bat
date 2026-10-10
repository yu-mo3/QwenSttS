@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Ayuan - M1 Environment Check

set "PY=.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo.
    echo   [!] Virtual environment .venv not found.
    echo       Please double-click setup.bat first.
    echo.
    pause
    exit /b 1
)

"%PY%" tools\check_env.py %*

echo.
pause
