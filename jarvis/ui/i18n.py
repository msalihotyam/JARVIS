"""THE localization system of the JARVIS interface. One path, for QML and Python.

Source strings are English and are marked where they are written:

* QML: ``qsTr("...")`` (the context is the file name), ``.arg()`` for values;
* Python: every literal is marked ``QT_TRANSLATE_NOOP("Context", "...")`` (the
  one form lupdate reliably extracts) and rendered with ``tx(context, source,
  *args)`` now, or kept as a message ``(context, source, args)`` and rendered
  by ``render`` whenever it is shown - so a switch reaches old notices too.

The Turkish catalog is the standard Qt Linguist file ``i18n/jarvis_tr-TR.ts``,
compiled by lrelease to ``i18n/jarvis_tr-TR.qm`` and served by a stock
``QTranslator``, so ``qsTr``, ``QCoreApplication.translate`` and ``tx`` all read
one catalog. (Not a Python ``QTranslator`` override: Qt calls translators under
its own lock from any thread, and a Python callback there deadlocks against the
GIL.) A string without a finished translation falls back to its English
source: never empty, never a key.

Keeping the catalog complete (``tests.test_ui.LocalizationTests`` fails when a
marked string has no Turkish translation or the ``.qm`` is stale)::

    python -m jarvis.ui.i18n        # re-extract marked strings into the catalog

then translate the new ``unfinished`` entries (Qt Linguist or any editor) and
run it again to compile.

Only presentation is localized. Persisted preferences store the stable locale
id (``en-US`` / ``tr-TR``); domain states, codes, ids and user or model content
are never translated.
"""

from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from PySide6.QtCore import Property, QCoreApplication, QLocale, QObject, QTranslator, Signal, Slot

LANGUAGES = ("en-US", "tr-TR")
DEFAULT_LANGUAGE = "en-US"
#: The visible option names - each in its own language, never translated.
LANGUAGE_NAMES = {"en-US": "English", "tr-TR": "Türkçe"}

UI_DIR = Path(__file__).resolve().parent
CATALOG_DIR = UI_DIR / "i18n"


def catalog_path(language: str) -> Path:
    return CATALOG_DIR / f"jarvis_{language}.ts"


def compiled_path(language: str) -> Path:
    return CATALOG_DIR / f"jarvis_{language}.qm"


def linguist_tool(name: str) -> Path:
    """``lupdate`` / ``lrelease`` as PySide6 installs them: next to the running
    interpreter itself on a venv (``python.exe``'s own directory IS ``Scripts``
    on Windows - appending it again looked here twice and never found either)."""
    tool = Path(sys.executable).parent / f"pyside6-{name}.exe"
    return tool if tool.exists() else Path(f"pyside6-{name}")


def QT_TRANSLATE_NOOP(context: str, source: str) -> str:  # noqa: N802 - the name lupdate looks for
    """Mark a string for the catalog without translating it yet."""
    return source


def load_catalog(path: Path) -> dict[tuple[str, str], str]:
    """(context, source) -> translation, finished entries only."""
    try:
        root = ElementTree.parse(path).getroot()  # nosec B314 - app-shipped translation catalog
    except (OSError, ElementTree.ParseError):
        return {}
    table: dict[tuple[str, str], str] = {}
    for context in root.iter("context"):
        name = context.findtext("name") or ""
        for message in context.iter("message"):
            source = message.findtext("source")
            translation = message.find("translation")
            if source is None or translation is None or translation.get("type") in ("unfinished", "vanished", "obsolete"):
                continue
            if translation.text:
                table[(name, source)] = translation.text
    return table


def fill(text: str, args: tuple | list = ()) -> str:
    """Qt-style ``%1``..``%9`` placeholders, as QML's ``.arg()`` uses."""
    return re.sub(r"%([1-9])", lambda m: str(args[int(m.group(1)) - 1]) if int(m.group(1)) <= len(args) else m.group(0), text)


def tx(context: str, source: str, *args) -> str:
    """Translate now (English when no Turkish entry exists), then fill ``%n``."""
    return fill(QCoreApplication.translate(context, source), args)


def render(message) -> str:
    """A stored message: plain text, or (context, source, args) rendered now."""
    if isinstance(message, (tuple, list)) and len(message) == 3:
        context, source, args = message
        return tx(context, source, *[render(a) for a in args])
    return str(message)


class Localization(QObject):
    """Exposed to QML as ``i18n``. Holds the running language; the bridge
    persists the choice. One instance and one translator per application."""

    languageChanged = Signal()

    def __init__(self, app: QCoreApplication, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._app = app
        self._language = DEFAULT_LANGUAGE
        self._translators: dict[str, QTranslator] = {}
        self._installed: QTranslator | None = None
        self._engines: list = []

    def attach(self, engine) -> None:
        """Retranslate this QML engine whenever the language changes."""
        self._engines.append(engine)

    def apply(self, language: str) -> None:
        language = language if language in LANGUAGES else DEFAULT_LANGUAGE
        if language != DEFAULT_LANGUAGE and language not in self._translators:
            translator = QTranslator(self)
            # A missing or unreadable catalog leaves the translator out: English.
            self._translators[language] = translator if translator.load(str(compiled_path(language))) else None
        translator = self._translators.get(language)
        if translator is not self._installed:
            if self._installed is not None:
                self._app.removeTranslator(self._installed)
            if translator is not None:
                self._app.installTranslator(translator)
            self._installed = translator
        changed = language != self._language
        self._language = language
        QLocale.setDefault(QLocale(self.qtLocale))
        for engine in list(self._engines):
            try:
                engine.retranslate()
            except RuntimeError:        # an engine already gone
                self._engines.remove(engine)
        if changed:
            self.languageChanged.emit()

    @Property(str, notify=languageChanged)
    def language(self) -> str:
        return self._language

    @Property(str, notify=languageChanged)
    def qtLocale(self) -> str:
        """The Qt locale name for formatting (``en_US`` / ``tr_TR``)."""
        return self._language.replace("-", "_")

    @Property("QVariantList", constant=True)
    def languages(self) -> list:
        return [{"id": code, "name": LANGUAGE_NAMES[code]} for code in LANGUAGES]

    @Slot(str, result=str)
    def upper(self, text: str) -> str:
        """Locale-correct uppercase (Turkish i -> İ), for tracked headings."""
        return QLocale(self.qtLocale).toUpper(str(text))


_localization: Localization | None = None


def localization(app: QCoreApplication) -> Localization:
    """The application's one Localization (created on first use)."""
    global _localization
    if _localization is None or _localization._app is not app:
        _localization = Localization(app)
    return _localization


def update_catalogs() -> int:
    """Re-extract every marked string into each non-English catalog and compile it."""
    sources = [str(p) for p in sorted(UI_DIR.rglob("*.qml"))] + [
        str(p) for p in sorted(UI_DIR.glob("*.py")) if p.name != "i18n.py"]
    code = 0
    for language in LANGUAGES[1:]:
        code |= subprocess.run([str(linguist_tool("lupdate")), *sources, "-ts", str(catalog_path(language)),
                                "-locations", "none", "-no-obsolete", "-target-language", language.replace("-", "_")]).returncode
        code |= subprocess.run([str(linguist_tool("lrelease")), str(catalog_path(language)),
                                "-qm", str(compiled_path(language))]).returncode
    return code


if __name__ == "__main__":  # pragma: no cover - maintenance entry point
    raise SystemExit(update_catalogs())
