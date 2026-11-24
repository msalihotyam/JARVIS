"""AUDPROV1: the provider facts every site reads. Golden literals pin today's
values at the consumer sites, so the provider table cannot drift them."""

import unittest
from types import SimpleNamespace

from jarvis import model_catalog
from jarvis.ui import backend as backend_module, preferences
from jarvis.ui.backend import ATTACHMENT_SUPPORT, Backend

GOLDEN_ATTACHMENTS = {
    "claude": {"image": True, "text": True},
    "codex": {"image": True, "text": True},
    "google": {"image": False, "text": False},
    "qwen": {"image": False, "text": False},
}
GOLDEN_LABELS = {
    "claude": ("Anthropic", "Claude"),
    "codex": ("OpenAI", "Codex"),
    "google": ("Google", "Antigravity"),
    "qwen": ("Alibaba Cloud", "Qwen"),
}


def _raise(*_args, **_kwargs):
    raise RuntimeError("no probe in tests")


def bare_backend() -> Backend:
    """A Backend with no store, no connector and no CLI: every probe fails closed."""
    backend = object.__new__(Backend)
    backend.store = SimpleNamespace(load=lambda: SimpleNamespace(usable=False))
    backend.codex_state = _raise
    backend.codex_verified = {}
    backend.google_state = _raise
    return backend


class ProviderGoldenTests(unittest.TestCase):
    def test_chat_providers(self):
        self.assertEqual(Backend.CHAT_PROVIDERS, ("claude", "codex"))
        self.assertEqual(preferences.CHAT_PROVIDERS, ("claude", "codex"))

    def test_probed(self):
        self.assertEqual(model_catalog.PROBED, ("claude", "codex", "google"))

    def test_attachment_support(self):
        self.assertEqual(ATTACHMENT_SUPPORT, GOLDEN_ATTACHMENTS)
        self.assertEqual(list(ATTACHMENT_SUPPORT), ["claude", "codex", "google", "qwen"])
        self.assertIs(ATTACHMENT_SUPPORT, backend_module.ATTACHMENT_SUPPORT)

    def test_provider_order(self):
        backend = bare_backend()
        backend.provider_line = lambda pid: {"id": pid}
        self.assertEqual([line["id"] for line in backend.providers()], ["claude", "codex", "google", "qwen"])

    def test_lines_labels_and_attachment_copies(self):
        backend = bare_backend()
        lines = {pid: backend.provider_line(pid) for pid in ("claude", "codex", "google")}
        lines["qwen"] = Backend.qwen_line()
        for pid, (vendor, name) in GOLDEN_LABELS.items():
            with self.subTest(pid=pid):
                line = lines[pid]
                self.assertEqual((line["id"], line["vendor"], line["name"]), (pid, vendor, name))
                self.assertEqual(line["attachments"], GOLDEN_ATTACHMENTS[pid])
                self.assertIsNot(line["attachments"], ATTACHMENT_SUPPORT[pid])
                self.assertFalse(line["research"])
        self.assertEqual([lines[p]["status"] for p in GOLDEN_LABELS], ["unavailable", "unavailable", "unavailable", "deferred"])


if __name__ == "__main__":
    unittest.main()
