"""Google / Antigravity in V1: truthful install status, nothing else.

The Antigravity CLI (``agy``) is not a JARVIS chat or research provider in V1:
its print mode gives the model shell, file and browser tools that no
JARVIS-controlled policy was proven to switch off, which breaks the
zero-authority General Chat contract. So JARVIS never starts it at all.

Its quota is not read either. ``agy -p "/usage"`` was once believed to be a
local read-only command; agy 1.2.9 answers it with a real agent turn (one
turn, ~15k tokens, a denied ``RunCommand``, ~9 s). No other local, turn-free
source of the plan's quota is known, so the quota is left unknown - never
estimated - and the provider panel reports only what a file-system check
proves: whether the CLI is installed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from jarvis.trusted_exec import resolve_trusted_executable


def agy_executable() -> str | None:
    """The Antigravity CLI by provenance: its known install root first, then
    PATH enumerated by ``jarvis.trusted_exec``. Never a search that can reach
    the current directory - an ``agy.exe`` sitting in the workspace JARVIS was
    pointed at is not the Antigravity CLI (DB-001)."""
    local = os.environ.get("LOCALAPPDATA")
    roots = (Path(local) / "agy" / "bin" / "agy.exe",) if local else ()
    found = resolve_trusted_executable("agy", install_paths=roots)
    return None if found is None else found.path


def antigravity_state() -> dict[str, Any]:
    """Status for the provider panel from a local install check only - no
    process is started, so no agent turn and no quota spend: ``not_installed``,
    or ``unavailable`` (installed; sign-in and quota are unproven, chat and
    research stay off for safety). No quota is ever reported."""
    status = "not_installed" if agy_executable() is None else "unavailable"
    return {"id": "google", "vendor": "Google", "name": "Antigravity", "models": [], "research": False, "status": status}
