"""Google / Antigravity in V1: truthful status and READ-ONLY quota, nothing else.

The Antigravity CLI (``agy``) is not a JARVIS chat or research provider in V1:
its print mode gives the model shell, file and browser tools that no
JARVIS-controlled policy was proven to switch off, which breaks the
zero-authority General Chat contract. So JARVIS never sends it a prompt.

It only asks the CLI for the plan's quota through its supported read-only
command ``agy -p "/usage" --output-format json``, which the CLI answers
without an agent turn, a model call or quota spend. The two pools the
provider reports ("Gemini Models" and "Claude and GPT models") stay separate
and every number is exactly what the CLI returned.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

#: Variables that could point the CLI at a metered Google route. They are never
#: passed on; only their names are compared, never their values.
GOOGLE_METERED_ENV_PREFIXES = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT",
                               "GOOGLE_CLOUD_LOCATION", "GOOGLE_APPLICATION_CREDENTIALS")
USAGE_TIMEOUT_SECONDS = 30
_WINDOWS = ("weekly", "5h")


def agy_executable() -> str | None:
    found = shutil.which("agy")
    if found:
        return found
    default = Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe"
    return str(default) if os.environ.get("LOCALAPPDATA") and default.is_file() else None


def _environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.upper().startswith(GOOGLE_METERED_ENV_PREFIXES)}


def usage_groups(payload: Any) -> list[dict[str, Any]]:
    """The quota pools exactly as ``/usage`` reported them; malformed parts are dropped, never guessed."""
    data = ((payload or {}).get("command") or {}).get("data") if isinstance(payload, dict) else None
    groups = []
    for group in (data or {}).get("groups") or [] if isinstance(data, dict) else []:
        if not isinstance(group, dict) or type(group.get("name")) is not str:
            continue
        buckets = []
        for bucket in group.get("buckets") or []:
            if not isinstance(bucket, dict) or bucket.get("window") not in _WINDOWS:
                continue
            remaining, reset = bucket.get("remaining_fraction"), bucket.get("reset_time")
            if type(remaining) not in (int, float) or not 0 <= remaining <= 1 or type(reset) is not str or len(reset) > 40:
                continue
            buckets.append({"window": bucket["window"], "remaining_fraction": float(remaining), "reset_time": reset})
        if buckets:
            groups.append({"name": group["name"][:80], "description": str(group.get("description") or "")[:200], "buckets": buckets})
    return groups


def antigravity_state() -> dict[str, Any]:
    """Status for the provider panel: ``not_installed``, ``unavailable`` (the
    CLI could not report quota - e.g. not signed in) or ``chat_unavailable``
    (signed in and reporting quota; chat and research are off for safety)."""
    executable = agy_executable()
    base = {"id": "google", "vendor": "Google", "name": "Antigravity", "models": [], "research": False}
    if executable is None:
        return {**base, "status": "not_installed"}
    workdir = tempfile.mkdtemp(prefix="jarvis-agy-usage-")
    try:
        completed = subprocess.run([executable, "-p", "/usage", "--output-format", "json"], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=USAGE_TIMEOUT_SECONDS, env=_environment(),
                                   cwd=workdir, stdin=subprocess.DEVNULL, shell=False)
        payload = json.loads(completed.stdout) if completed.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        payload = None
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    # Only the read-only command's own answer counts: no agent turn may have run.
    if not isinstance(payload, dict) or payload.get("status") != "SUCCESS" or payload.get("num_turns") not in (0, None):
        return {**base, "status": "unavailable"}
    groups = usage_groups(payload)
    if not groups:
        return {**base, "status": "unavailable"}
    return {**base, "status": "chat_unavailable", "quota": {"groups": groups}}
