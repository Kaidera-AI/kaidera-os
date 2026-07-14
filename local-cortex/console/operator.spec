# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Kaidera OS macOS menu-bar operator app.

Build from ``local-cortex/console``:

    pyinstaller operator.spec

Output:

    dist/Kaidera OS Operator.app

This app is only a native shell. It imports ``app.native_operator`` and controls
the installed Kaidera OS service; it does not bundle the console UI or Cortex.
"""

import os

ICON = os.path.join(SPECPATH, "app.icns")

a = Analysis(
    ["operator_menubar.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=[],
    hiddenimports=[
        "AppKit",
        "Foundation",
        "objc",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt5",
        "PyQt6",
        "PySide6",
        "gi",
        "tkinter",
        "webview",
        "uvicorn",
        "fastapi",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Kaidera OS Operator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
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
    name="Kaidera OS Operator",
)

app = BUNDLE(
    coll,
    name="Kaidera OS Operator.app",
    icon=ICON,
    bundle_identifier="ai.kaidera.kaidera-os",
    info_plist={
        "CFBundleName": "Kaidera OS Operator",
        "CFBundleDisplayName": "Kaidera OS Operator",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
    },
)
