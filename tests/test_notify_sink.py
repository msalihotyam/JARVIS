"""PM28: project-mode outbox rows reach Salih as a throttled tray toast and a recent list.

A fake outbox and a fake clock: no database, no wall clock.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.test_ui import _APP  # noqa: F401 - the offscreen platform and the application

from PySide6.QtCore import Signal  # noqa: E402
from PySide6.QtWidgets import QSystemTrayIcon  # noqa: E402

from jarvis import notify_sink  # noqa: E402
from jarvis.notify_sink import NotifySink  # noqa: E402
from jarvis.ui import tray  # noqa: E402
from tests.test_dftray import FakeBridge, FakeMenu, FakeTray  # noqa: E402


def row(outbox_id, kind="done", severity="normal", **extra):
    return {"outbox_id": outbox_id, "run_id": "r1", "event_seq": outbox_id, "kind": kind,
            "severity": severity, "created_at": "2026-10-28T10:00:00", **extra}


class Outbox:
    """notify_outbox_rows(after_id=...) over a list, recording each after_id."""

    def __init__(self, rows=()) -> None:
        self.rows, self.asked = list(rows), []

    def notify_outbox_rows(self, *, after_id=0):
        self.asked.append(after_id)
        return [r for r in self.rows if r["outbox_id"] > after_id]


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def sink(outbox, clock=None):
    s = NotifySink(lambda after_id: outbox.notify_outbox_rows(after_id=after_id), clock=clock or Clock())
    s.poll()                                   # the first poll seeds the cursor, never toasts
    return s


class SinkTests(unittest.TestCase):
    def test_first_poll_seeds_without_toast_and_keeps_rows_in_recent(self) -> None:
        outbox = Outbox([row(1), row(2, "plan_ready", "high")])
        s = sink(outbox)
        self.assertIsNone(s.toast())
        self.assertEqual([1, 2], [r["outbox_id"] for r in s.recent()])
        self.assertEqual(2, s.cursor)

    def test_order_cursor_and_dedupe(self) -> None:
        outbox = Outbox()
        s = sink(outbox)
        outbox.rows = [row(5), row(3), row(4), row(3)]      # out of order, one duplicate
        fresh = s.poll()
        self.assertEqual([3, 4, 5], [r["outbox_id"] for r in fresh])
        self.assertEqual(5, s.cursor)
        self.assertEqual([], s.poll())                      # nothing is delivered twice
        self.assertEqual([0, 0, 5], outbox.asked)

    def test_malformed_ids_are_skipped(self) -> None:
        outbox = Outbox()
        s = sink(outbox)
        s._fetch = lambda after_id: [row(None), row(True), row("7"), row(2)]
        self.assertEqual([2], [r["outbox_id"] for r in s.poll()])

    def test_importance_filter(self) -> None:
        outbox = Outbox()
        s = sink(outbox)
        outbox.rows = [row(1, "slice_started", "low"), row(2, "progress", "low")]
        s.poll()
        self.assertIsNone(s.toast())                        # low: list only
        self.assertEqual(["low", "low"], [r["importance"] for r in s.recent()])
        outbox.rows.append(row(3, "needs_input", "high"))
        s.poll()
        self.assertEqual(("Bilgi gerekiyor", "Devam etmek için yanıtınız gerekiyor.", "high"), s.toast())

    def test_throttle_collapses_the_rest(self) -> None:
        outbox, clock = Outbox(), Clock()
        s = sink(outbox, clock)
        outbox.rows = [row(1, "questions_ready")]
        s.poll()
        self.assertEqual(("Sorular hazır", "Proje sorularını yanıtlayın.", "normal"), s.toast())
        outbox.rows += [row(2), row(3, "approval_required", "high"), row(4)]
        s.poll()
        clock.now += 1
        self.assertIsNone(s.toast())                        # within the throttle window
        clock.now += 5
        self.assertEqual(("JARVIS", "3 yeni bildirim", "high"), s.toast())
        self.assertIsNone(s.toast())

    def test_row_text_is_never_rendered(self) -> None:
        outbox = Outbox()
        s = sink(outbox)
        outbox.rows = [row(1, "<b>x</b>", "<script>", title="<img src=x>", text="<a href=x>y</a>")]
        s.poll()
        title, text, importance = s.toast()
        self.assertEqual(("Yeni bildirim", "Yeni bir proje bildirimi var.", "normal"), (title, text, importance))
        for item in s.recent():
            self.assertNotIn("<", "".join(str(v) for v in item.values()))


class TrayHookTests(unittest.TestCase):
    def install(self, outbox):
        class ClickTray(FakeTray):
            messageClicked = Signal()

        bridge, self.window = FakeBridge(), MagicMock()
        bridge.backend = SimpleNamespace(conversations=lambda: outbox)
        with patch("PySide6.QtWidgets.QSystemTrayIcon", ClickTray), patch("PySide6.QtWidgets.QMenu", FakeMenu):
            icon = tray.install_tray(_APP, bridge, self.window)
        self.addCleanup(icon.deleteLater)
        self.addCleanup(delattr, _APP, "_jarvis_notify")
        return icon, _APP._jarvis_notify

    def test_toast_icon_and_click_opens_the_window(self) -> None:
        outbox = Outbox([row(1)])
        icon, s = self.install(outbox)
        tray.deliver(icon, s)
        self.assertEqual([], icon.messages)                 # the backlog seeds silently
        outbox.rows.append(row(2, "plan_ready", "high"))
        tray.deliver(icon, s)
        self.assertEqual(("Plan hazır", "Proje planını onaylayın.", QSystemTrayIcon.MessageIcon.Warning, tray._TOAST_MS),
                         icon.messages[0])
        s._last_toast = None
        outbox.rows.append(row(3, "done"))
        tray.deliver(icon, s)
        self.assertEqual(QSystemTrayIcon.MessageIcon.Information, icon.messages[1][2])
        icon.messageClicked.emit()
        self.window.raise_.assert_called_once()

    def test_store_failure_reads_as_nothing_new(self) -> None:
        def broken():
            raise OSError("db")
        fetch = tray.outbox_fetch(SimpleNamespace(backend=SimpleNamespace(conversations=broken)))
        self.assertEqual([], fetch(0))
        self.assertEqual([], tray.outbox_fetch(SimpleNamespace(backend=SimpleNamespace(conversations=lambda: None)))(0))
        self.assertIsNone(tray.outbox_fetch(SimpleNamespace()))


class SinkProtocolTests(unittest.TestCase):
    """AUDNOTIFY1: the tray as a Sink makes exactly the calls tray.deliver makes."""

    class Icon:
        def __init__(self) -> None:
            self.messages = []

        def showMessage(self, *args) -> None:
            self.messages.append(args)

    def run_both(self, rows_per_poll):
        via_tray, via_sink = self.Icon(), self.Icon()
        outbox_a, outbox_b = Outbox(), Outbox()
        a, b = sink(outbox_a), sink(outbox_b)
        for rows in rows_per_poll:
            outbox_a.rows += rows
            outbox_b.rows += rows
            a._last_toast = b._last_toast = None
            tray.deliver(via_tray, a)
            notify_sink.deliver(b, notify_sink.TraySink(via_sink, tray._TOAST_MS))
        return via_tray.messages, via_sink.messages

    def test_tray_sink_matches_tray_deliver(self) -> None:
        old, new = self.run_both([[row(1, "plan_ready", "high")], [row(2, "done")], [row(3, "progress", "low")],
                                  [row(4), row(5, "needs_input", "high")]])
        self.assertEqual(old, new)
        self.assertEqual([QSystemTrayIcon.MessageIcon.Warning, QSystemTrayIcon.MessageIcon.Information,
                          QSystemTrayIcon.MessageIcon.Warning], [m[2] for m in new])
        self.assertEqual(("JARVIS", "2 yeni bildirim"), new[2][:2])
        self.assertTrue(all(m[3] == tray._TOAST_MS for m in new))

    def test_tray_deliver_goes_through_the_sink(self) -> None:
        # AUDNOTIFY1FIX: the live tray path is TraySink.show, not a copy of its mapping.
        icon, outbox = self.Icon(), Outbox()
        s = sink(outbox)
        real_show = notify_sink.TraySink.show
        with patch.object(notify_sink.TraySink, "show", autospec=True, side_effect=real_show) as show:
            tray.deliver(icon, s)                           # seeding poll: no toast, no call
            show.assert_not_called()
            outbox.rows.append(row(1, "plan_ready", "high"))
            tray.deliver(icon, s)
        show.assert_called_once()
        self.assertEqual(("Plan hazır", "Proje planını onaylayın.", "high"), show.call_args.args[1:])
        self.assertEqual([("Plan hazır", "Proje planını onaylayın.", QSystemTrayIcon.MessageIcon.Warning,
                           tray._TOAST_MS)], icon.messages)

    def test_a_new_channel_is_one_class(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.shown = []

            def show(self, title, text, importance) -> None:
                self.shown.append((title, text, importance))

        outbox, out = Outbox(), Recorder()
        s = sink(outbox)
        notify_sink.deliver(s, out)
        self.assertEqual([], out.shown)                     # nothing new: no call
        outbox.rows.append(row(1, "plan_ready", "high"))
        notify_sink.deliver(s, out)
        self.assertEqual([("Plan hazır", "Proje planını onaylayın.", "high")], out.shown)


if __name__ == "__main__":
    unittest.main()
