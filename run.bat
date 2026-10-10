@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Ayuan - Environment Check

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
set "RC=%ERRORLEVEL%"

echo.
pause
REM Propagate the checker's exit code. Without this the batch always
REM exits 0 (pause's status), so CI / shortcuts / the future packager
REM can never tell whether the self-check actually passed.
exit /b %RC%
