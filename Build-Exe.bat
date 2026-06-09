@echo off
chcp 65001 >nul
title Eye Crypt — Build .exe
cd /d "%~dp0"

set "PY="
for %%v in (3.14 3.13 3.12 3.11 3) do (
    py -%%v --version >nul 2>&1
    if not errorlevel 1 (
        set "PY=py -%%v"
        goto :found
    )
)
echo [ERROR] Python 3.11+ not found
pause & exit /b 1
:found

echo Installing build dependencies (one-time)...
%PY% -m pip install --quiet pyinstaller pywebview pystray Pillow ^
    ccxt python-dotenv loguru rich fastapi "uvicorn[standard]" ^
    websockets aiohttp httpx pyotp

if not exist "eye_crypt.ico" (
    echo Generating icon...
    %PY% make_icon.py
)

echo.
echo Building eyecrypt-bot.exe (5-10 min, please wait)...
%PY% -m PyInstaller eyecrypt-bot.spec --clean --noconfirm

if errorlevel 1 (
    echo.
    echo [ERROR] Build failed. Check output above.
    pause & exit /b 1
)

echo.
echo === DONE ===
echo Output: dist\eyecrypt-bot.exe
for %%I in (dist\eyecrypt-bot.exe) do echo Size:   %%~zI bytes
echo.
echo Distribute this single .exe to users. Double-click to run.
echo.
pause
