"""V2.1a M4 'Hatırla': a mouse action on what the person wrote and on replies.

The action names the STORED turn through bridge.rememberMessage; the app
decides (filter, status). A success says so briefly and honestly; a refusal
says why. Real app over a throwaway TEMP database; no model or connector.
"""
from __future__ import annotations

import unittest

import tests.test_ui as ui
from tests.test_ui import _APP, UiTestCase, pump  # noqa: F401 - _APP: the application

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402


class RememberActionTests(UiTestCase):
    item = ui.PresentationTests.item
    window = ui.PresentationTests.window

    def click(self, root, name) -> None:
        target = self.item(root, name)
        QTest.mouseClick(root, Qt.LeftButton, Qt.NoModifier,
                         target.mapToScene(QPointF(target.width() / 2, target.height() / 2)).toPoint())
        pump(0)

    def check(self, theme: str) -> None:
        bridge = self.bridge()
        bridge.setTheme(theme)
        app = self.factory(self.store.load().settings)
        project = app.memory.create_project(f"Alpha {theme}").id
        conversation = app.memory.create_conversation(project).id
        bridge.openConversation(conversation)
        pump(100)
        answers: list[dict] = []
        bridge.memoryRecordActionDone.connect(answers.append)
        bridge._say("user", "Deploys run on Fridays")
        bridge._say("jarvis", "Noted: Fridays.", chat=True)
        root = self.window(bridge)
        self.assertEqual(theme, root.property("themeName"))
        feed = bridge.feed
        user, reply = len(feed) - 2, len(feed) - 1
        turns = [t.id for t in app.memory.load_conversation_state(conversation).turns]
        self.assertEqual(turns, [feed[user]["turn"], feed[reply]["turn"]])
        for index in range(user):                    # a notice is never rememberable
            self.assertFalse(self.item(root, f"messageActions_{index}").property("visible"))
        for index in (user, reply):
            action = self.item(root, f"messageRemember_{index}")
            self.assertEqual(("Remember", True), (action.property("text"), action.property("visible")))

        self.click(root, f"messageRemember_{user}")
        answer = answers[-1]
        self.assertTrue(answer["ok"], answer)
        self.assertEqual((conversation, feed[user]["turn"]), (answer["conversation_id"], answer["message_id"]))
        listed = app.memory_records_payload(project)["records"]
        self.assertEqual(["Deploys run on Fridays"], [r["text"] for r in listed])
        note = self.item(root, f"messageRememberNote_{user}")
        self.assertEqual(("Remembered", True), (note.property("text"), note.isVisible()))
        self.assertFalse(self.item(root, f"messageRememberNote_{reply}").property("visible"))

        bridge._say("user", "my password is hunter2")
        pump(60)
        secret = len(bridge.feed) - 1
        self.click(root, f"messageRemember_{secret}")
        self.assertFalse(answers[-1]["ok"])
        self.assertEqual("credential", answers[-1]["refused"])
        pump(60)
        note = self.item(root, f"messageRememberNote_{secret}")      # the refusal's notice rebuilt the rows
        self.assertEqual(("Not remembered: it contains a secret, key or password", True),
                         (note.property("text"), note.isVisible()))
        self.assertEqual(1, len(app.memory_records_payload(project)["records"]))

        # A reopened conversation's turns can be remembered too: they keep their stored ids.
        bridge._restore_feed(app.memory.load_conversation_state(conversation))
        self.assertEqual(turns[:2], [e["turn"] for e in bridge.feed if e["kind"] in ("user", "jarvis")][:2])
        root.close()

    def test_remember_action_space_theme(self) -> None:
        self.check("space")

    def test_remember_action_dark_theme(self) -> None:
        self.check("dark")


if __name__ == "__main__":
    unittest.main()
