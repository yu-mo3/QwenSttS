@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title Ayuan - Build

echo ============================================================
echo   Ayuan - Build a standalone package
echo ============================================================
echo.

set "PY=.venv\Scripts\python.exe"
set "PYSIDE=.venv\Scripts\pyinstaller.exe"

if not exist "%PY%" (
    echo   [!] Virtual environment .venv not found.
    echo       Please run setup.bat first.
    pause
    exit /b 1
)

if not exist "%PYSIDE%" (
    echo   [1/2] Installing PyInstaller ...
    "%PY%" -m pip install "pyinstaller>=6.10"
    if errorlevel 1 (
        echo   [!] Failed to install PyInstaller.
        pause
        exit /b 1
    )
)

echo   [2/2] Building ...
REM The spec file does the real work; see ayuan.spec for why some
REM pieces are collected explicitly instead of relying on auto-detection.
"%PYSIDE%" ayuan.spec --noconfirm
if errorlevel 1 (
    echo.
    echo   [!] Build failed. Read the messages above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Done: dist\Ayuan\Ayuan.exe
echo.
echo   Next steps for whoever receives this folder:
echo     1. Copy _internal\.env.example to .env (next to Ayuan.exe)
echo     2. Fill in DASHSCOPE_API_KEY
echo     3. Run Ayuan.exe
echo.
echo   If it will not start, check data\ayuan.log and data\crash.log
echo   next to the exe - the app writes them there on purpose.
echo ============================================================
pause
