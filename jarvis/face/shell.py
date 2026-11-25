"""FACESHELL1: clickable empty shell of the new PC window, for Salih to look at.

Sample data only: no backend, no file, network, microphone, clipboard or
screen access; buttons change visual state only. The old JARVIS window
(jarvis.ui) is neither imported nor touched.

Run: python -m jarvis.face.shell        (--check loads, reports and exits)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

QML = Path(__file__).with_name("qml") / "FaceWindow.qml"


def build(engine=None):
    """Load FaceWindow.qml into ``engine`` (a new QQmlApplicationEngine when None)."""
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")  # same style as the old window; customizable controls
    from PySide6.QtQml import QQmlApplicationEngine

    engine = engine or QQmlApplicationEngine()
    engine.load(str(QML))
    return engine


def main(argv: list[str] | None = None) -> int:
    from PySide6.QtGui import QGuiApplication

    argv = sys.argv if argv is None else argv
    app = QGuiApplication.instance() or QGuiApplication(argv)
    engine = build()
    if not engine.rootObjects():
        print("FaceWindow.qml did not load", file=sys.stderr)
        return 1
    if "--check" in argv:
        return 0
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
