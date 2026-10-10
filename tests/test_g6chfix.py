"""G6CHFIX: the command preset's scope chip reserves exactly its own pad after the name,
so a name that fits is never elided (and its tip stays inactive)."""
from __future__ import annotations

import unittest

from tests.test_g6f9_header_menu import HeaderMenuCase
from tests.test_scheduler_runner import setUpModule, tearDownModule  # noqa: F401
from tests.test_ui import pump


class ChipNameTests(HeaderMenuCase):
    def chip_for(self, name: str):
        store = self.db()
        project = store.create_project(name)
        bridge = self.bridge()
        root = self.window(bridge)
        bridge.setTheme("command")
        pump(400)
        bridge.openProjectScope(project.id)
        pump(400)
        return self.item(root, "chatHeaderScopeName"), self.item(root, "chatHeaderScopeTip")

    def test_short_names_are_not_truncated_in_the_non_link_chip(self) -> None:
        for name in ("Çok", "A", "Tez"):
            with self.subTest(name=name):
                label, tip = self.chip_for(name)
                self.assertFalse(label.property("truncated"))
                self.assertGreaterEqual(label.property("width"), label.property("implicitWidth"))
                self.assertFalse(tip.property("active"))

    def test_a_name_longer_than_the_chip_still_elides(self) -> None:
        label, tip = self.chip_for("Çok uzun bir proje adı " * 5)
        self.assertTrue(label.property("truncated"))
        self.assertTrue(tip.property("active"))


if __name__ == "__main__":
    unittest.main()
