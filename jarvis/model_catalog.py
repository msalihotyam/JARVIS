"""The provider/model catalog the model selector reads: last-known-good, local.

WHY. Every provider line used to be established by live CLI probes (Claude
auth + model list, Codex app-server + model list) run one after another on the
UI's single shared worker, each time the selector opened. So the first open
waited seconds, and every open spawned the whole set again.

NOW. The catalog is read from a small local file at startup and served from
memory. Refresh runs in the background, one thread per provider (so Claude and
Codex overlap), never twice at once for the same provider. JARVIS refreshes
once at every launch; opening the selector refreshes only what is older than
``TTL_SECONDS``. A refresh that fails or comes back empty never replaces what
was known to be good.

DISPLAY ONLY. What is stored is exactly what the selector shows - status
words, model ids and names, efforts, plan quota windows - never a credential,
email or organisation. A cached "available" grants nothing: every real call
still verifies its own route. The file is safe to delete; the next start
simply discovers again.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: How old a provider line may be before opening the selector re-checks it.
TTL_SECONDS = 300.0
#: After a failed probe: try again this soon, not on every open.
FAILURE_RETRY_SECONDS = 60.0

CACHE_VERSION = 1
#: Providers with a live probe, in display order; Qwen is a fixed "deferred" line.
PROBED = ("claude", "codex", "google")

_ENTRY_KEYS = ("id", "vendor", "name", "status", "evidence", "plan", "models", "quota", "research", "attachments")
_MODEL_KEYS = ("id", "name", "resolved", "description", "efforts", "default_effort")
#: A failed probe says this; it establishes nothing, so it never replaces a good entry.
_UNREADABLE = "unavailable"


def default_catalog_path(preferences_path: Path) -> Path:
    return Path(preferences_path).parent / "provider-catalog.json"


# -- persistence --------------------------------------------------------------

def _clean_entry(entry: dict[str, Any]) -> dict[str, Any]:
    clean = {k: entry[k] for k in _ENTRY_KEYS if k in entry}
    clean["models"] = [{k: m[k] for k in _MODEL_KEYS if k in m} for m in clean.get("models") or [] if isinstance(m, dict)]
    return clean


def load(path: Path) -> dict[str, dict[str, Any]]:
    """``{provider id: {"entry", "checked_at"}}``; ``{}`` for a missing,
    unreadable or foreign file (the next refresh rebuilds it)."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION or not isinstance(raw.get("providers"), dict):
        return {}
    out = {}
    for pid, record in raw["providers"].items():
        if (pid in PROBED and isinstance(record, dict) and isinstance(record.get("entry"), dict)
                and record["entry"].get("id") == pid and isinstance(record["entry"].get("status"), str)
                and isinstance(record.get("checked_at"), (int, float))):
            out[pid] = {"entry": _clean_entry(record["entry"]), "checked_at": float(record["checked_at"])}
    return out


def save(path: Path, records: dict[str, dict[str, Any]]) -> bool:
    """Atomic: a reader sees the old file or the new one, never half of one."""
    path = Path(path)
    document = {"version": CACHE_VERSION, "providers": {
        pid: {"entry": _clean_entry(r["entry"]), "checked_at": r["checked_at"]} for pid, r in records.items()}}
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(document, indent=1), encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return False


# -- the catalog ------------------------------------------------------------------

class ProviderCatalog:
    """Last-known-good provider lines in memory, refreshed in the background.

    ``probe(provider_id)`` returns that provider's line. ``on_update()`` is
    called after a refresh changed what is known - from the refreshing thread
    unless ``sync`` - and the caller reads ``snapshot()`` itself, so it always
    shows the newest whole state.
    """

    def __init__(self, path: Path, probe: Callable[[str], dict[str, Any]],
                 on_update: Callable[[], None] | None = None, *, sync: bool = False,
                 clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.probe = probe
        self.on_update = on_update
        self.sync = sync
        self.clock = clock
        self._lock = threading.Lock()
        self._records = load(self.path)
        self._inflight: set[str] = set()
        #: Probes started, per provider (instrumentation and tests).
        self.probes_started: dict[str, int] = {}

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return [json.loads(json.dumps(self._records[pid]["entry"])) for pid in PROBED if pid in self._records]

    def _due(self, pid: str) -> bool:
        record = self._records.get(pid)
        return record is None or self.clock() - record["checked_at"] >= TTL_SECONDS

    def refresh(self, force: bool = False) -> list[str]:
        """Start every provider that is due (all of them when ``force``);
        returns the providers started. One refresh per provider at a time."""
        with self._lock:
            started = [pid for pid in PROBED if pid not in self._inflight and (force or self._due(pid))]
            for pid in started:
                self._inflight.add(pid)
                self.probes_started[pid] = self.probes_started.get(pid, 0) + 1
        for pid in started:
            if self.sync:
                self._refresh_one(pid)
            else:
                threading.Thread(target=self._refresh_one, args=(pid,), daemon=True, name=f"jarvis-catalog-{pid}").start()
        return started

    def busy(self) -> bool:
        with self._lock:
            return bool(self._inflight)

    def _refresh_one(self, pid: str) -> None:
        try:
            line = self.probe(pid)
        except Exception:  # noqa: BLE001 - an unreadable probe establishes nothing
            line = None
        with self._lock:
            try:
                changed = self._merge(pid, line)
                save(self.path, self._records)       # the check time counts too: a fresh check is not repeated
            finally:
                self._inflight.discard(pid)
        if changed and self.on_update is not None:
            self.on_update()

    def _merge(self, pid: str, line: Any) -> bool:
        now = self.clock()
        previous = self._records.get(pid)
        if not isinstance(line, dict) or line.get("id") != pid or not isinstance(line.get("status"), str):
            line = {"id": pid, "status": _UNREADABLE}
        if line["status"] == _UNREADABLE and previous is not None and previous["entry"].get("status") == "available":
            # A failed check never replaces a good line; it is retried soon, not on every open.
            previous["checked_at"] = min(previous["checked_at"], now - TTL_SECONDS + FAILURE_RETRY_SECONDS)
            return False
        entry = _clean_entry(line)
        if previous is not None:
            before = previous["entry"]
            if not entry["models"] and before.get("models"):
                entry["models"] = before["models"]             # an empty list never blanks a good one
            if not entry.get("quota") and before.get("quota") and entry["status"] == before.get("status"):
                entry["quota"] = before["quota"]
        self._records[pid] = {"entry": entry, "checked_at": now}
        return previous is None or previous["entry"] != entry
