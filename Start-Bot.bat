@echo off
chcp 65001 >nul
title Eye Crypt Bot
REM Absolute path — works no matter where this file is launched from.
cd /d "C:\Users\danil\Downloads\Telegram Desktop\orbit"

set "PY="
for %%v in (3.14 3.13 3.12 3.11 3) do (
    py -%%v --version >nul 2>&1
    if not errorlevel 1 (
        set "PY=py -%%v"
        goto :found
    )
)
echo [ERROR] Python 3.11+ not found.
pause
exit /b 1
:found

echo ===========================================
echo            EYE CRYPT BOT
echo ===========================================
echo Folder: %CD%
echo.
if not exist ".env" (
    echo [ERROR] .env not found in this folder! Aborting so nothing gets overwritten.
    pause
    exit /b 1
)

echo Starting watchdog (scanner + dashboard + tunnel + Telegram bot)...
echo Keep this window open. Press Ctrl+C to stop.
echo.
%PY% watchdog.py
echo.
echo [watchdog exited]
pause
