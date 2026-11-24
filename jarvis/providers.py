"""The one provider table: every provider line JARVIS knows, in display order.

Chat providers, attachment support, probed providers and the line labels are
all read from here, so adding a provider is one row. No imports from jarvis:
anything may read this module without a cycle."""

from typing import NamedTuple


class Provider(NamedTuple):
    id: str
    vendor: str
    name: str
    #: What a person may attach for this provider (see ``jarvis.ui.backend.ATTACHMENT_SUPPORT``).
    attachments: dict
    #: A person may choose it for General Chat / understanding.
    chat: bool
    #: It has a live (local, no-spend) probe; otherwise it is a fixed line.
    probed: bool


PROVIDERS = (
    Provider("claude", "Anthropic", "Claude", {"image": True, "text": True}, chat=True, probed=True),
    Provider("codex", "OpenAI", "Codex", {"image": True, "text": True}, chat=True, probed=True),
    Provider("google", "Google", "Antigravity", {"image": False, "text": False}, chat=False, probed=True),
    Provider("qwen", "Alibaba Cloud", "Qwen", {"image": False, "text": False}, chat=False, probed=False),
)
BY_ID = {p.id: p for p in PROVIDERS}
CHAT_PROVIDERS = tuple(p.id for p in PROVIDERS if p.chat)
PROBED = tuple(p.id for p in PROVIDERS if p.probed)
