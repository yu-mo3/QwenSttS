@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Ayuan - Setup

echo ============================================================
echo   Ayuan - First-time Setup
echo ============================================================
echo.

set "BASEPY="
for %%P in (
    "D:\Anaconda\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
) do (
    if not defined BASEPY if exist %%~P set "BASEPY=%%~P"
)

if not defined BASEPY (
    where py >nul 2>&1 && set "BASEPY=py -3.12"
)

if not defined BASEPY (
    echo   [!] Python 3.11 / 3.12 not found.
    echo       Python 3.13+ is NOT supported - no wheels for vosk / webrtcvad.
    echo       Please install Python 3.12 and run this script again.
    echo.
    pause
    exit /b 1
)

echo   [1/3] Interpreter : %BASEPY%
%BASEPY% --version
echo.

echo   [2/3] Creating virtual environment .venv ...
if exist ".venv\Scripts\python.exe" (
    echo         Already exists, skipping.
) else (
    %BASEPY% -m venv .venv
    if errorlevel 1 (
        echo   [!] Failed to create virtual environment.
        pause
        exit /b 1
    )
)
echo.

echo   [3/3] Installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo   [!] Dependency installation failed.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Setup complete.
echo.
echo   Next steps:
echo     1. Copy  .env.example  to  .env  and fill in DASHSCOPE_API_KEY
echo     2. Double-click  run.bat
echo ============================================================
echo.
pause
