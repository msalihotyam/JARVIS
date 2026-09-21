# -*- mode: python ; coding: utf-8 -*-
"""JARVIS V1 Windows release build (PyInstaller, ``--onedir``).

Reproducible: run through ``scripts/build_release.ps1``, into a disposable
output directory, never by hand-editing after a build.

WHAT IS BUNDLED. Exactly what ``jarvis.ui`` reads from disk at runtime: its
own Python modules (found by PyInstaller's own import scan from
``launch_jarvis.py``), the one QML file, the bundled OFL fonts, the approved
icon/tray artwork, and the Turkish translation catalog - the same files
``jarvis.ui.typography.FONT_DIR``, ``jarvis.ui.resources.asset_dir`` and
``jarvis.ui.i18n.CATALOG_DIR`` already resolve relative to their own module,
so the packaged layout mirrors the source layout exactly and needs no
runtime-path branching beyond the ``sys._MEIPASS`` fallback those modules
already carry for a future ``--onefile`` build.

WHAT IS NEVER BUNDLED. This developer's own runtime state: ``data/`` (the
JARVIS-owned browser profile and any other local runtime data), any
``.jarvis`` profile directory, ``uv.lock``, and this repository's own git
history. None of those are referenced by ``datas`` below, and none live
under ``jarvis/`` in the first place - production config and the database
live under the OPERATOR's own home directory (``Path.home() / ".jarvis"``),
never inside the package, so there is nothing of the operator's to
accidentally sweep in.

``--onedir``, not ``--onefile``: a plain app folder is what V1 needs (no
installer yet), and it is also what keeps the existing ``Path(__file__)``-
relative resource lookups correct without the ``sys._MEIPASS`` branch ever
needing to run.
"""

from pathlib import Path

block_cipher = None

# ``SPECPATH`` is PyInstaller's own global, set to this file's directory.
ROOT = Path(SPECPATH).resolve().parent  # packaging/ -> the repository root
JARVIS_UI = ROOT / "jarvis" / "ui"

datas = [
    # The WHOLE tree, not a hand-picked subset: Main.qml imports sibling QML
    # modules (``theme``, ``components``, ``pages``, ``overlays``) that a
    # file-by-file list silently drops the day a new one is added - measured
    # here, not assumed: an earlier version of this spec bundled only
    # Main.qml + fonts + assets and the packaged build failed to load with
    # ``"theme": no such directory``.
    (str(JARVIS_UI / "qml"), "jarvis/ui/qml"),
    (str(JARVIS_UI / "i18n"), "jarvis/ui/i18n"),
]

a = Analysis(
    [str(ROOT / "packaging" / "launch_jarvis.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    # The QML engine loads these by string at runtime (QtQuick's own import
    # machinery, and the "Basic" Quick Controls style app.py selects), so
    # PyInstaller's static import scan cannot see the need for them from
    # ordinary Python imports alone.
    hiddenimports=[
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuickControls2",
        "PySide6.QtNetwork",
        "PySide6.QtSvg",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JARVIS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # GUI application: no console window on normal launch.
    console=False,
    icon=str(JARVIS_UI / "qml" / "assets" / "app.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="JARVIS",
)
