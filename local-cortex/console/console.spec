# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec - Kaidera OS Console -> native macOS `.app` (R6).

Build:   pyinstaller console.spec
Output:  dist/Kaidera OS Console.app   (windowed; double-click or `open` to launch)

Entry point is bootstrap.py (uvicorn-on-a-daemon-thread + a pywebview WKWebView
window). app/main.py stays shell-agnostic — the SAME ASGI app runs under plain
`uvicorn app.main:app` in dev and inside this packaged window.

Risk mitigations from research/2026-06-01-desktop-packaging.md baked in here:
  * `excludes` drops the GUI backends pywebview probes for (PyQt*/PySide6/gi/
    tkinter) so they are NOT dragged into the bundle (risk #2, size).
  * `hiddenimports` pins the macOS Cocoa webview backend (pywebview picks it
    dynamically, so PyInstaller can't see it) + uvicorn's lazily-imported
    loop/protocol impls (risk #4-adjacent: dynamic imports).
  * `datas` ships app/templates + app/static + the built SPA bundle (`spa/dist`)
    so Jinja, StaticFiles, and the refined `/app` console resolve at runtime under
    `sys._MEIPASS` (app/main.py uses Path(__file__).parent, which PyInstaller
    rewrites to the frozen app/ dir — risk #4, frozen paths).
  * `console=False` → windowed `.app` (no terminal). BUNDLE sets the dock name
    + bundle id + Info.plist keys. (multiprocessing.freeze_support() — risk #1 —
    lives in bootstrap.main(); see that file.)

NOTE: no app icon is bundled. The only brand asset is a WIDE WHITE wordmark SVG
(the existing app/static brand logo) - wrong aspect ratio for an app
icon and invisible on light backgrounds, so per the research ("an icon if a
.icns/logo is available, else omit") `icon=` is omitted and the `.app` uses the
default macOS app icon. Drop a square `app.icns` next to this spec and set
`ICON = "app.icns"` below to brand it later.
"""

import os

# Resolve paths relative to this spec (SPECPATH is the spec's own directory).
APP_DIR = os.path.join(SPECPATH, "app")
SPA_DIST_DIR = os.path.join(SPECPATH, "spa", "dist")
if not os.path.isfile(os.path.join(SPA_DIST_DIR, "index.html")):
    raise SystemExit(
        "spa/dist/index.html is missing. Run `cd spa && npm run build` before "
        "`pyinstaller console.spec` so the packaged app can serve /app/."
    )

DATAS = [
    # (source_on_disk, dest_dir_in_bundle) — keep the app/ package layout so
    # main.py's Path(__file__).parent / "templates" | "static" resolve.
    (os.path.join(APP_DIR, "templates"), "app/templates"),
    (os.path.join(APP_DIR, "static"), "app/static"),
    # The refined SPA bundle served by app/main.py at /app. This is deliberately a
    # hard package prerequisite: a native redistributable that omits the SPA would
    # silently open the wrong surface.
    (SPA_DIST_DIR, "spa/dist"),
]

# App icon: the Kaidera AI hexagon mark, built as app.icns from the brand icon.png.
ICON = os.path.join(SPECPATH, "app.icns")

block_cipher = None


a = Analysis(
    ["bootstrap.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=DATAS,
    hiddenimports=[
        # pywebview chooses its platform backend at runtime — pin the macOS one.
        "webview.platforms.cocoa",
        # uvicorn lazily imports its event-loop + http/websocket protocol impls
        # by string; name them so the frozen bundle includes them.
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.websockets_impl",
        "uvicorn.lifespan.on",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # pywebview probes for EVERY GUI backend it can find; without these it
        # drags Qt/GTK/Tk into the bundle and bloats it. We only use Cocoa.
        "PyQt5",
        "PySide6",
        "PyQt6",
        "gi",
        "tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Kaidera OS Console",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed app — no terminal window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,  # build for the host arch (arm64 on Apple Silicon)
    codesign_identity=None,  # PyInstaller ad-hoc-signs Mach-O by default
    entitlements_file=None,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Kaidera OS Console",
)

app = BUNDLE(
    coll,
    name="Kaidera OS Console.app",
    icon=ICON,
    bundle_identifier="ai.kaidera.kaidera-os.console",
    info_plist={
        "CFBundleName": "Kaidera OS Console",
        "CFBundleDisplayName": "Kaidera OS Console",
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
    },
)
