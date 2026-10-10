@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
title 阿源 · 环境初始化

echo ============================================================
echo   阿源 · 首次环境初始化
echo ============================================================
echo.

REM ---- 定位 Python 3.11/3.12 ----
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
    echo [X] 未找到 Python 3.11/3.12
    echo     本项目不支持 Python 3.13+（vosk / webrtcvad 无对应 wheel）
    echo     请先安装 Python 3.12 后重试
    echo.
    pause
    exit /b 1
)

echo [1/3] 使用解释器: %BASEPY%
%BASEPY% --version
echo.

echo [2/3] 创建虚拟环境 .venv ...
if exist ".venv\Scripts\python.exe" (
    echo     已存在，跳过
) else (
    %BASEPY% -m venv .venv
    if errorlevel 1 (
        echo [X] 虚拟环境创建失败
        pause
        exit /b 1
    )
)
echo.

echo [3/3] 安装依赖 ...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [X] 依赖安装失败
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   ✅ 初始化完成
echo   下一步：复制 .env.example 为 .env 并填入百炼 API Key
echo           然后双击 run.bat 运行 M1 自检
echo ============================================================
echo.
pause
