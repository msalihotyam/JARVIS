"""TALKPHONE1: send-to-phone decision (pure; no I/O, no network, no clock)."""

import ast
import unittest
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

from jarvis import talk_phone_send as tps
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, HOSTILE_TEXT, each

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "jarvis" / "talk_phone_send.py"
DIGEST = "ab" * 32
_DEFAULT = object()


def clean(text):
    return SimpleNamespace(flagged=False, text=text)


def flagged(text):
    return SimpleNamespace(flagged=True, text="[redacted]")


def refusal(text):  # TALKREDACT2 Refusal shape: no .flagged; callers treat as flagged
    return SimpleNamespace(reason="redaction_failed")


def boom(text):
    raise RecursionError


def request(**over):
    return {"id": "req-1", "source": "pc_view", "by": "salih", "command": "send_to_phone",
            "attachment_id": "att-1", "at": "2026-11-25T17:31:00+03:00", **over}


def attachment(**over):
    return {"id": "att-1", "origin": "pc", "on_phone": False, "kind": "pdf", "bytes": 1000,
            "path": DIGEST + ".pdf", "display_name": "rapor.pdf", **over}


def config(**over):
    return {"telegram_enabled": True, "chat_id": 12345, "kinds": frozenset({"pdf", "png", "txt"}),
            "max_bytes": 20 * 1024 * 1024, **over}


def run(req=_DEFAULT, att=_DEFAULT, cfg=_DEFAULT, redact=clean, used=()):
    return tps.decide(request() if req is _DEFAULT else req, attachment() if att is _DEFAULT else att,
                      config() if cfg is _DEFAULT else cfg, redact=redact, used=used)


class _Hostile(Mapping):
    def __init__(self, exc):
        self.exc = exc

    def __getitem__(self, key):
        raise self.exc

    def __iter__(self):
        raise self.exc

    def __len__(self):
        return 1


class _Base(unittest.TestCase):
    def refused(self, reason, **kw):
        out = run(**kw)
        self.assertFalse(out["ok"], out)
        self.assertEqual(out["reason"], reason)
        self.assertEqual(out["log"]["decision"], "refused")
        return out


class AcceptanceTests(_Base):
    def test_upload_one_attachment_to_salih_chat(self):
        out = run()
        self.assertTrue(out["ok"])
        self.assertEqual(out["action"], {"kind": "upload", "chat_id": 12345, "attachment_id": "att-1",
                                         "path": DIGEST + ".pdf", "file_name": "rapor.pdf",
                                         "name_replaced": False})

    def test_decision_is_a_log_record(self):
        self.assertEqual(run()["log"], {"event": "send_to_phone", "by": "salih", "request_id": "req-1",
                                        "attachment_id": "att-1", "at": "2026-11-25T17:31:00+03:00",
                                        "decision": "upload", "reason": None})
        log = self.refused("too_large", att=attachment(bytes=20 * 1024 * 1024 + 1))["log"]
        self.assertEqual((log["by"], log["attachment_id"], log["at"]),
                         ("salih", "att-1", "2026-11-25T17:31:00+03:00"))

    def test_request_is_single_use(self):
        first = run()
        self.assertTrue(first["ok"])
        log = self.refused("request_used", used={first["log"]["request_id"]})["log"]
        self.assertEqual((log["by"], log["request_id"], log["attachment_id"], log["at"]),
                         ("salih", "req-1", "att-1", "2026-11-25T17:31:00+03:00"))
        self.assertTrue(run(req=request(id="req-2"), used={"req-1"})["ok"])

    def test_flagged_name_is_neutral_and_file_still_goes(self):
        for redact in (flagged, refusal, boom, lambda text: None, lambda text: SimpleNamespace(flagged=0)):
            with self.subTest(redact=redact):
                out = run(redact=redact)
                self.assertTrue(out["ok"])
                self.assertEqual((out["action"]["file_name"], out["action"]["name_replaced"]),
                                 ("attachment.pdf", True))

    def test_redact_sees_the_name(self):
        seen = []
        run(redact=lambda text: seen.append(text) or clean(text))
        self.assertEqual(seen, ["rapor.pdf"])

    def test_channel_off_refused_with_note(self):
        out = self.refused("channel_off", cfg=config(telegram_enabled=False))
        self.assertEqual(out["note"], "kanal kapalı")
        self.assertEqual(out["log"]["request_id"], "req-1")
        each(self, (None, 1, 0, "true", "false"),
             lambda v: self.refused("bad_config", cfg=config(telegram_enabled=v)))

    def test_size_cap_and_kind_allowlist(self):
        self.assertTrue(run(att=attachment(bytes=20 * 1024 * 1024))["ok"])
        self.refused("too_large", att=attachment(bytes=20 * 1024 * 1024 + 1))
        self.refused("kind_not_allowed", att=attachment(kind="exe", path=DIGEST + ".exe"))
        self.refused("kind_not_allowed", att=attachment(kind="docx", path=DIGEST + ".docx"))
        self.refused("kind_mismatch", att=attachment(path=DIGEST + ".png"))


class NegativeTests(_Base):
    def test_only_pc_view_source(self):
        each(self, ("brain", "brain_reply", "telegram", "PC_VIEW", "pc_view ", "", None, 1),
             lambda v: self.refused("not_from_pc_view", req=request(source=v)))
        req = request()
        del req["source"]
        self.refused("not_from_pc_view", req=req)
        each(self, ("brain", "Salih", None), lambda v: self.refused("not_from_owner", req=request(by=v)))
        self.refused("bad_command", req=request(command="send_all"))

    def test_one_attachment_id_only(self):
        each(self, ("all", "ALL", " hepsi ", "tümü", "*", ["att-1"], ("att-1", "att-2"), {"att-1"}),
             lambda v: self.refused("one_attachment_only", req=request(attachment_id=v)))
        each(self, ("att-1,att-2", "att-1 att-2", "", None, 1, {"id": "att-1"}),
             lambda v: self.refused("bad_attachment_id", req=request(attachment_id=v)))

    def test_id_not_in_log(self):
        self.refused("not_in_log", att=None)
        self.refused("not_in_log", att=attachment(id="att-2"))

    def test_from_telegram_or_on_phone_is_pointless(self):
        for att in (attachment(origin="telegram"), attachment(on_phone=True)):
            out = self.refused("already_on_phone", att=att)
            self.assertTrue(out["pointless"])
        self.assertFalse(self.refused("too_large", att=attachment(bytes=10**9))["pointless"])
        each(self, ("brain", None, "PC"), lambda v: self.refused("bad_attachment", att=attachment(origin=v)))
        each(self, (None, 0, "false"), lambda v: self.refused("bad_attachment", att=attachment(on_phone=v)))

    def test_path_outside_root(self):
        bad = [*HOSTILE_PATH_PARTS, "../" + DIGEST + ".pdf", "..\\" + DIGEST + ".pdf",
               "sub/" + DIGEST + ".pdf", DIGEST + ".pdf:x", DIGEST + ".pdf::$DATA", "PROGRA~1.pdf",
               DIGEST.upper() + ".pdf", DIGEST + ".pdf.", DIGEST + ".pdf ", "C:" + DIGEST + ".pdf",
               "/" + DIGEST + ".pdf", "a" * 63 + ".pdf", None, 1, b"x.pdf"]
        each(self, bad, lambda v: self.refused("path_outside_root", att=attachment(path=v)))

    def test_wrong_types(self):
        for value in (None, [], "x", 1, SimpleNamespace()):
            with self.subTest(value=value):
                self.refused("bad_request", req=value)
                self.refused("bad_config", cfg=value)
                self.refused("bad_attachment" if value is not None else "not_in_log", att=value)
        each(self, HOSTILE_NUMBERS, lambda v: self.assertFalse(run(att=attachment(bytes=v))["ok"]))
        each(self, (*HOSTILE_NUMBERS, 0, -1, 2**53),
             lambda v: self.refused("bad_config", cfg=config(chat_id=v)))
        each(self, (*HOSTILE_NUMBERS, 0, 50 * 1024 * 1024 + 1),
             lambda v: self.refused("bad_config", cfg=config(max_bytes=v)))
        each(self, ("pdf", None, {"pdf": 1}, [1]), lambda v: self.refused("bad_config", cfg=config(kinds=v)))
        each(self, ("req-1", b"req-1", None, 1), lambda v: self.refused("bad_used", used=v))
        each(self, ("", None, 1, "x" * 65, "yesterday", "2026-11-25T17:31:00 "),
             lambda v: self.refused("bad_time", req=request(at=v)))
        self.refused("bad_redact", redact=None)
        self.assertEqual(tps.decide(request(), attachment(), config())["reason"], "bad_used")
        self.assertEqual(tps.decide(request(), attachment(), config(), used=())["reason"], "bad_redact")

    def test_hostile_text_ids_refused(self):
        each(self, HOSTILE_TEXT, lambda v: self.refused("bad_request", req=request(id=v)))
        each(self, HOSTILE_TEXT, lambda v: self.assertFalse(run(req=request(attachment_id=v))["ok"]))

    def test_hostile_names_never_pass_raw(self):
        def check(value):
            for name in (value, value + ".pdf") if isinstance(value, str) else (value,):
                out = run(att=attachment(display_name=name))
                self.assertTrue(out["ok"])
                shown = out["action"]["file_name"]
                self.assertTrue(shown.isprintable() and len(shown) <= 120 and shown.endswith(".pdf"))
        each(self, (*HOSTILE_TEXT, None, 1, "rapor.exe", "rapor.pdf.exe"), check)

    def test_hostile_mappings_and_recursion_never_escape(self):
        for exc in (RecursionError(), ValueError(), TypeError()):
            with self.subTest(exc=exc):
                self.refused("internal_error", req=_Hostile(exc))
                self.refused("internal_error", att=_Hostile(exc))
                self.refused("internal_error", cfg=_Hostile(exc))
        self.assertTrue(run(redact=boom)["ok"])

    def test_deterministic_and_ascii_reasons(self):
        self.assertEqual(run(), run())
        outs = [run(req=request(source="brain")), run(att=None), run(cfg=config(telegram_enabled=False)),
                run(req=_Hostile(RecursionError())), run(att=attachment(path="..")), run(used={"req-1"})]
        for out in outs:
            self.assertTrue(out["reason"].isascii())
            self.assertIn(out["note"], (None, "kanal kapalı"))


class GuardTests(unittest.TestCase):
    def test_module_imports_no_io(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        banned = {"os", "io", "pathlib", "socket", "urllib", "http", "subprocess", "shutil", "sqlite3",
                  "requests", "ssl", "asyncio", "telegram", "time"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                self.assertNotIn(name.split(".")[0], banned)
                self.assertFalse(name.startswith("jarvis"))
        calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertFalse(calls & {"open", "__import__", "eval", "exec"})
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse(attrs & {"now", "utcnow", "today", "time", "monotonic"})  # no wall clock

    def test_source_guards(self):
        source = MODULE.read_text(encoding="utf-8")
        self.assertNotIn("def " + "heartbeat", source)
        self.assertNotIn("project_" + "folders", source)

    def test_nothing_in_the_app_imports_it(self):
        for folder in ("jarvis", "scripts"):
            for path in (ROOT / folder).rglob("*.py"):
                if path != MODULE:
                    self.assertNotIn("talk_phone_send", path.read_text(encoding="utf-8", errors="replace"), path)


if __name__ == "__main__":
    unittest.main()
