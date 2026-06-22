# PyInstaller spec for Eye Crypt Bot desktop .exe
# Run: py -3 -m PyInstaller eyecrypt-bot.spec --clean --noconfirm
# Output: dist/eyecrypt-bot.exe (~80-120 MB, single file)
# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(os.getcwd())

EXCHANGES = ["mexc", "kucoin", "bitget", "htx", "bingx", "bitmart"]
ccxt_hidden = []
for ex in EXCHANGES:
    ccxt_hidden += [f"ccxt.{ex}", f"ccxt.async_support.{ex}", f"ccxt.pro.{ex}"]

# mexc ccxt.pro decodes its WS feed via protobuf (ccxt.protobuf.mexc.*_pb2 +
# google.protobuf). These are imported lazily by the ccxt base, so PyInstaller
# misses them — without bundling, mexc raises NotSupported("requires protobuf")
# at runtime and the whole mexc venue goes dark for every client.
protobuf_hidden = collect_submodules("google.protobuf") + [
    "google.protobuf.json_format",
    "ccxt.protobuf.mexc.PushDataV3ApiWrapper_pb2",
    "ccxt.protobuf.mexc.PublicAggreDealsV3Api_pb2",
]

a = Analysis(
    ['eyecrypt_main.py'],                # unified entry — dispatches via --mode flag
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=ccxt_hidden + protobuf_hidden + [
        "ccxt", "ccxt.async_support", "ccxt.pro",
        "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
        "uvicorn.protocols", "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto", "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto", "uvicorn.lifespan",
        "uvicorn.lifespan.on", "websockets",
        "webview", "webview.platforms.winforms",
        "pystray", "pystray._win32", "PIL", "PIL.Image", "PIL.ImageDraw",
        "eyecrypt_app", "dashboard", "watchdog", "ws_scanner",
        "executor", "inventory", "hedge", "license", "rebalancer",
        "scanner", "bidir", "balances", "depth", "currencies", "logsetup", "appdir",
        "telegram_bot", "tunnel",
        "pyotp", "loguru", "rich", "httpx", "dotenv", "updater",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "scipy", "pandas", "numpy.testing", "tkinter"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='eyecrypt-bot',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # no console — UI is the desktop window
    icon='eye_crypt.ico',
)
