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
    echo       Please install Python 3.12 and run this script again.
    echo.
    pause
    exit /b 1
)

echo   [1/3] Interpreter : %BASEPY%
%BASEPY% --version

REM Must verify the version, not just that the file exists: an Anaconda
REM 3.13 on D: would silently produce a venv that cannot install
REM vosk / webrtcvad / pyaudio, and the error would only surface later
REM at "pip install" with a confusing message.
%BASEPY% -c "import sys; sys.exit(0 if sys.version_info[:2] in ((3,11),(3,12)) else 1)"
if errorlevel 1 (
    echo.
    echo   [!] Unsupported Python version.
    echo       This project needs Python 3.11 or 3.12.
    echo       Python 3.13+ has no wheels for vosk / webrtcvad / pyaudio.
    echo.
    pause
    exit /b 1
)
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
echo     2. Double-click  run.bat  to run the environment self-check
echo     3. Run  python tools/m2_chat.py  to talk to her
echo ============================================================
echo.
pause
