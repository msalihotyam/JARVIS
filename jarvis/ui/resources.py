"""Where JARVIS's own bundled non-Python files live, in source or packaged runs.

``Path(__file__).resolve().parent`` - the same resolution ``typography.FONT_DIR``
already uses for the bundled fonts - is enough for source runs and for a
PyInstaller ``--onedir`` build: in both, this module's own file sits at
``jarvis/ui/resources.py`` and the packaged data next to it at the same
relative layout, so no path needs to change between the two.

The one build shape that DOES move the root is ``--onefile``: PyInstaller then
extracts everything to a temporary directory named in ``sys._MEIPASS``, and a
frozen module's ``__file__`` does not point inside it. This build uses
``--onedir`` (a plain app folder, per the V1 release scope), so the fallback
below is not exercised today - it exists because "production packaging may
change filesystem layout" is exactly the failure this lookup must survive.
"""

from __future__ import annotations

import sys
from pathlib import Path


def asset_dir() -> Path:
    """The ``jarvis/ui/qml/assets`` directory, wherever this run's files are."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "jarvis" / "ui" / "qml" / "assets"
    return Path(__file__).resolve().parent / "qml" / "assets"


def asset_path(name: str) -> Path:
    """The full path to one bundled asset by file name."""
    return asset_dir() / name
