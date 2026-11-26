"""TALKTG1: Telegram text in for the PC talk core (receive only, default off).

TgInbox is a raw inbox port (take(limit) / ack(handle)) over one injected
Telegram client: plain text from the one allowed private chat becomes an item
{"id": "tg.<update_id>", "kind": "text", "text": ..., "handle": update_id};
every other update is consumed and counted by kind. The stored offset is
contiguous: it never passes a text update the core has not acked, so a lost
ack or a failed save brings the same item (same id) again and the core's keyed
log keeps it exactly once. MergedInbox puts the PC and Telegram raw ports
behind the one raw port talk_boundary.wrap takes. No network, no secrets, no
logging; counters carry kinds only, never message text. Nothing raises.
"""

from __future__ import annotations

import json
import sys

from jarvis import telegram_offset as off
from jarvis import telegram_wire as wire
from jarvis.talk_boundary import _ack_value

MAX_TEXT = 4096  # Telegram's own cap; 4001..4096 reach the core and are logged refused there
MAX_AGE = 86400  # process_batch wants one; lateness is not used here
MAX_LIMIT = 100  # getUpdates accepts 1..100
_BIDI = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")
_FORWARD = ("forward_origin", "forward_from", "forward_from_chat", "forward_sender_name",
            "forward_date", "is_automatic_forward", "sender_chat", "via_bot")
_KINDS = {wire.VoiceNote: "voice", wire.Photo: "photo", wire.Document: "document",
          wire.ButtonCallback: "button"}


def _client_type(name):
    # Looked up, not imported: the client module carries the network stack.
    return getattr(sys.modules.get("jarvis.telegram_client"), name, None)


class TgInbox:
    def __init__(self, client, offset_store, allowed_chat, clock, enabled=False):
        self._client, self._store, self._chat, self._clock = client, offset_store, allowed_chat, clock
        # A bad allowed_chat would refuse every text and pass it for good: stay off instead.
        self._enabled = enabled is True and type(allowed_chat) is int and allowed_chat > 0
        self._offset = None  # last stored offset; None until loaded
        self._fetched = []  # (update_id, is_text) of the last take, ascending
        self._pending = set()  # text update_ids of the last take not yet acked
        self.counts = {}

    def _count(self, kind):
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def take(self, limit):
        """A list of items; [] when off or on any failure (counted)."""
        if not self._enabled:
            return []
        try:
            return self._take(limit)
        except Exception:
            self._count("internal")
            return []

    def _take(self, limit):
        if type(limit) is not int or limit < 1:
            self._count("bad_limit")
            return []
        offset = self._load()
        if offset is None:
            return []
        now = self._clock.now()
        if type(now) is float and now - now == 0:  # finite float seconds, as the raw clock allows
            now = int(now)
        if type(now) is not int:
            self._count("clock_bad")
            return []
        batch = self._fetch(offset, min(limit, MAX_LIMIT))
        if batch is None:
            return []
        res = off.process_batch(batch, offset, now=now, max_age=MAX_AGE)
        if isinstance(res, off.Refusal):
            self._count("batch_refused")
            return []
        for _ in res.refused:
            self._count("bad_update_id")
        self._fetched, self._pending, items = [], set(), []
        for handled in res.updates:
            uid = handled.update_id
            kind, text = self._classify(handled.update)
            self._count(kind)
            self._fetched.append((uid, text is not None))
            if text is not None:
                self._pending.add(uid)
                items.append({"id": "tg.%d" % uid, "kind": "text", "text": text, "handle": uid})
        self._advance()  # passes a leading run of non-text updates
        return items

    def _load(self):
        if self._offset is None:
            try:
                text = self._store.load()
            except Exception:
                self._count("store_load_failed")
                return None
            got = 0 if text is None else off.load_state(text)
            if isinstance(got, off.Refusal):
                self._count("offset_bad")
                return None
            self._offset = got
        return self._offset

    def _fetch(self, offset, limit):
        try:
            got = self._client.call("getUpdates", {"offset": offset, "limit": limit, "timeout": 0})
        except Exception:
            self._count("client_raised")
            return None
        if type(got) is _client_type("Ok") and type(got.value) is list:
            return got.value
        if type(got) is _client_type("Refused"):
            self._count("client_refused")
        elif type(got) is _client_type("Failed"):
            self._count("client_failed")
        else:
            self._count("client_bad")
        return None

    def _classify(self, update):
        """(kind, text) where text is None unless the update is an item."""
        try:
            rec = wire.parse_update(json.dumps(update).encode("utf-8"), {self._chat})
        except Exception:
            return "invalid_json", None
        if isinstance(rec, wire.Refusal):
            edited = rec is wire.Refusal.INVALID_UPDATE and "edited_message" in update
            return ("edited" if edited else rec.value), None
        if type(rec) is not wire.TextMessage:
            return _KINDS.get(type(rec), "unknown"), None
        msg = update["message"]
        sender, text = msg.get("from"), rec.text
        if msg["chat"].get("type") != "private":
            return "group", None
        if type(sender) is not dict or sender.get("is_bot") is not False:
            return "bot", None
        if type(sender.get("id")) is not int or sender["id"] != rec.chat_id:
            return "sender", None
        if any(key in msg for key in _FORWARD):
            return "forwarded", None
        if len(text) > MAX_TEXT:
            return "too_long", None
        if "\x00" in text:
            return "nul", None
        if any(ch in _BIDI for ch in text):
            return "bidi", None
        if not text.strip():
            return "empty", None
        return "text", text

    def ack(self, handle):
        """True when the offset was stored; never raises."""
        if not self._enabled:
            return False
        try:
            uid = handle
            if type(handle) is str and handle.startswith("tg.") and handle[3:].isascii() \
                    and handle[3:].isdigit():
                uid = int(handle[3:])
            if type(uid) is not int or uid not in self._pending:
                self._count("bad_ack")
                return False
            self._pending.discard(uid)
            return self._advance()
        except Exception:
            self._count("internal")
            return False

    def _advance(self):
        """Store 1 + the highest fetched id with nothing pending at or below it."""
        new = self._offset
        for uid, is_text in self._fetched:
            if is_text and uid in self._pending:
                break
            new = uid + 1
        if new == self._offset:
            return False
        try:
            text = off.dump_state(new)
            ok = not isinstance(text, off.Refusal) and self._store.save(text) is not False
        except Exception:
            ok = False
        if not ok:
            self._count("store_failed")  # offset kept: the next take brings the item again
            return False
        self._offset = new
        return True


class MergedInbox:
    """One raw port over the PC and Telegram raw ports; ack goes back to the source."""

    def __init__(self, pc_raw, tg_raw):
        self._src = (pc_raw, tg_raw)
        self._first = 0  # alternates so a limit of 1 starves neither source
        self._routes = []  # (ack value as handed out, source index, value for the source)
        self.counts = {}

    def _count(self, kind):
        self.counts[kind] = self.counts.get(kind, 0) + 1

    def take(self, limit):
        """A list of at most limit raw items; a failing source gives nothing."""
        self._routes = []  # PcInbox forgets its handles on every take too
        if type(limit) is not int or limit < 1:
            return []
        order = (self._first, 1 - self._first)
        self._first = 1 - self._first
        out = []
        for index, share in zip(order, ((limit + 1) // 2, limit // 2)):
            for raw in self._take(index, share):
                fwd = key = _ack_value(raw)
                if index == 0 and type(raw) is dict and type(key) is str and key.startswith("tg."):
                    raw = dict(raw, id="!" + key)  # a PC id must not take a Telegram log key
                    key = raw["id"]  # fails ID_RE: the core logs it refused, then acks it
                    self._count("pc_tg_id")
                self._routes.append((key, index, fwd))
                out.append(raw)
        return out

    def _take(self, index, share):
        if share < 1:
            return []
        try:
            got = self._src[index].take(share)
        except Exception:
            got = None
        if type(got) is not list:
            self._count(("pc", "tg")[index] + "_failed")
            return []
        return got[:share]

    def ack(self, value):
        """The source's answer, or False; never raises."""
        for pos, (key, index, fwd) in enumerate(self._routes):
            if key is value:
                del self._routes[pos]
                try:
                    return self._src[index].ack(fwd)
                except Exception:
                    self._count(("pc", "tg")[index] + "_ack_failed")
                    return False
        self._count("bad_ack")
        return False
