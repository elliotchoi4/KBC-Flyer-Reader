# PyInstaller spec — produces a single "KBC Flyer Reader" executable.
#
# Build:
#     pip install pyinstaller
#     pyinstaller flyer_reader.spec
#
# Output appears in dist/"KBC Flyer Reader"/. On Windows ship the whole
# dist/"KBC Flyer Reader" folder (or use --onefile by uncommenting the marked
# block below). The launcher inside is "KBC Flyer Reader.exe" with the book icon.
#
# Important: we bundle templates/ as data so the .exe is self-contained.

# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

datas = [
    ("templates/KBC_Template__Building_Survey_Locked.xlsx", "templates"),
    ("templates/KBC_Template__Land_Survey_Locked.xlsx", "templates"),
]
# Ship the app icon so the window/taskbar icon works in the frozen build
# (it's also used as the EXE icon below).
import os as _os
if _os.path.exists("assets/icon.ico"):
    datas += [("assets/icon.ico", "assets")]
# Ship the default output folder (with its readme) so it exists next to the
# app on first run.
if _os.path.exists("Flyer Reader Output/README.txt"):
    datas += [("Flyer Reader Output/README.txt", "Flyer Reader Output")]
# tkinterdnd2 ships TCL files that need to come along.
try:
    datas += collect_data_files("tkinterdnd2")
except Exception:
    pass
# customtkinter ships theme JSON + assets (fonts/icons) that must be
# bundled, or the app crashes at launch with a missing-theme error.
try:
    datas += collect_data_files("customtkinter")
except Exception:
    pass

hiddenimports = (
    collect_submodules("pydantic")
    + collect_submodules("instructor")
    + collect_submodules("openai")
    + collect_submodules("pdfplumber")
    + collect_submodules("fitz")
    + collect_submodules("customtkinter")
    + ["PIL._tkinter_finder"]
    + ["src.credential_store"]
    + ["src.field_hints"]
    + ["src.version", "src.update_checker"]
)

a = Analysis(
    ["src/main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "scipy", "torch", "tensorflow", "cv2"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---- ONEDIR build (recommended): faster startup, smaller failure surface ----
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="KBC Flyer Reader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,            # GUI app, no console window
    icon="assets/icon.ico" if __import__("os").path.exists("assets/icon.ico") else None,
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=True, upx_exclude=[],
    name="KBC Flyer Reader",
)

# ---- ONEFILE build (single .exe, slower startup). Uncomment to use, and
#      comment out the EXE/COLLECT block above.
#
# exe = EXE(
#     pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
#     name="KBC Flyer Reader",
#     debug=False, strip=False, upx=True, console=False,
#     icon="assets/icon.ico" if __import__("os").path.exists("assets/icon.ico") else None,
# )
