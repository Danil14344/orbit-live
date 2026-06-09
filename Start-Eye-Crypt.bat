@echo off
chcp 65001 >nul
title Eye Crypt Bot
cd /d "%~dp0"
echo.
echo ===========================================
echo            EYE CRYPT BOT
echo ===========================================
echo.

REM Find Python (3.11+)
set "PY="
for %%v in (3.14 3.13 3.12 3.11 3) do (
    py -%%v --version >nul 2>&1
    if not errorlevel 1 (
        set "PY=py -%%v"
        goto :found
    )
)
echo [ERROR] Python 3.11+ not found.
echo Install from: https://www.python.org/downloads/
echo.
pause
exit /b 1
:found
echo [OK] Python: %PY%
%PY% --version

REM Dependencies check
echo.
echo [1/3] Checking dependencies...
%PY% -c "import ccxt, fastapi, uvicorn, httpx, pyotp, webview, pystray, PIL" 2>nul
if errorlevel 1 (
    echo Installing dependencies (one-time setup, ~3 min)...
    %PY% -m pip install --quiet ccxt python-dotenv loguru rich fastapi "uvicorn[standard]" websockets aiohttp httpx pyotp pywebview pystray Pillow
)
echo [OK] Dependencies ready

REM Config check
echo.
echo [2/3] Checking config...
if not exist ".env" (
    if exist ".env.example" (
        copy /Y .env.example .env >nul
    ) else (
        echo EYECRYPT_LICENSE=paste-your-license-here> .env
        echo EYECRYPT_API=https://api.eyecryptbot.com>> .env
        echo MODE=paper>> .env
    )
    echo [WARN] Created .env — opening for license input...
    notepad .env
)
echo [OK] Config ready

REM Launch desktop app
echo.
echo [3/3] Starting Eye Crypt Bot (desktop window)...
%PY% eyecrypt_app.py
