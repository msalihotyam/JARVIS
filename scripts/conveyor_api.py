"""The ONE place that touches the Anthropic API (APIBROKER).

call(model, system, messages, max_tokens, purpose) -> text. Guards, in order: model allowlist
(Haiku/Sonnet only), argument shape (text only: no streaming, tools or files), the money caps
(hard monthly cap, daily share toward the credit's expiry, both from the cost ledger), Windows,
then the key - read at call time from the Credential Manager generic credential
"konveyor-bekci" and nowhere else (never an env var, file or argv). The key is never stored,
logged or put in an exception. Stdlib only.
"""
import contextlib
import ctypes
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
TIMEOUT_S = 60
CRED_TARGET = "konveyor-bekci"
# USD per million tokens (input, output), from the API credit plan
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0)}
MONTHLY_CAP_USD = 90.0  # hard cap, UTC calendar month
CREDIT_END = datetime(2026, 11, 18, tzinfo=timezone.utc)  # credit expiry; refuse from then on
MAX_TOKENS_CAP = 8192
PURPOSE_MAX = 80
LEDGER_NAME = "api_ledger.jsonl"
CAPS_NAME = "api_caps.json"
KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]+")


def main_checkout(here):
    """The main checkout of `here`: a linked worktree's .git file points at <main>/.git/worktrees/<name>,
    so every worktree shares ONE ledger and lock (a per-checkout ledger would multiply the cap)."""
    g = Path(here) / ".git"
    if g.is_file():
        m = re.match(r"gitdir:\s*(.+)", g.read_text(encoding="utf-8").strip())
        if m:
            return (Path(here) / m.group(1).strip()).resolve().parents[2]
    return Path(here)


ROOT = main_checkout(Path(__file__).resolve().parents[1]) / "build" / "conveyor"


class ApiRefused(Exception):
    """A guard refused the call before any request was sent."""


class ApiError(Exception):
    """The request was sent and failed; carries the status and the API error type only."""


def scrub(text, key=""):
    """Mirrors conveyor.redacted's key rule: the literal key and anything sk-ant-shaped out."""
    text = str(text)
    if key:
        text = text.replace(key, "[key]")
    return KEY_RE.sub("[key]", text)


def read_credential(target=CRED_TARGET):
    """The generic credential's secret via CredReadW, or "" when unreadable / not Windows."""
    if sys.platform != "win32":
        return ""
    from ctypes import wintypes

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", wintypes.DWORD),
                    ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                    ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                 ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    advapi.CredFree.restype = None
    p = ctypes.POINTER(CREDENTIALW)()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(p)):  # 1 = CRED_TYPE_GENERIC
        return ""
    try:
        blob = ctypes.string_at(p.contents.CredentialBlob, p.contents.CredentialBlobSize)
    finally:
        advapi.CredFree(p)
    for enc in ("utf-16-le", "utf-8"):  # cmdkey/CredWrite store UTF-16LE; accept UTF-8 too
        try:
            k = blob.decode(enc).strip()
        except UnicodeDecodeError:
            continue
        if KEY_RE.fullmatch(k):
            return k
    return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # never follow a 30x: urllib would carry x-api-key to the new host


def https_post(url, headers, body, timeout):
    """POST body; returns (status, response bytes). Exceptions propagate without the key."""
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with opener.open(req, timeout=timeout) as r:  # nosec B310 - fixed https URL
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0


def _no_constant(c):
    raise ValueError(c)  # NaN / Infinity are not money


def caps(root):
    """(monthly, daily or None): the constants, only ever LOWERED by the optional override file."""
    p = Path(root) / CAPS_NAME
    if not p.exists():
        return MONTHLY_CAP_USD, None
    try:
        o = json.loads(p.read_text(encoding="utf-8"), parse_constant=_no_constant)
    except (OSError, ValueError):
        raise ApiRefused(f"{CAPS_NAME} unreadable") from None
    if (not isinstance(o, dict) or set(o) - {"monthly_usd", "daily_usd"}
            or not all(_num(v) for v in o.values())):
        raise ApiRefused(f"{CAPS_NAME} invalid")  # fail closed, never back to the higher constant
    return min(MONTHLY_CAP_USD, o.get("monthly_usd", MONTHLY_CAP_USD)), o.get("daily_usd")


def read_ledger(path):
    """Every ledger row; an unreadable row refuses (skipping it would undercount the spend)."""
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                r = json.loads(line, parse_constant=_no_constant)
            except ValueError:
                raise ApiRefused("ledger row unreadable") from None
            if (not isinstance(r, dict) or not _num(r.get("ts")) or not _num(r.get("usd"))
                    or not _num(r.get("release", 0))):
                raise ApiRefused("ledger row invalid")
            rows.append(r)
    return rows


def check_budget(now, rows, monthly, daily, est):
    """Refuse when the month's spend + est passes the cap, or today's spend + est passes the share.

    share = (cap - spend this month before today) / days left to the credit end, today included:
    a day that spends less leaves more for every later day (carry-over). A row's spend is usd minus
    its "release" (a settled call releases the estimate its reserve row booked before sending)."""
    t = datetime.fromtimestamp(now, timezone.utc)
    if t >= CREDIT_END:
        raise ApiRefused("API credit period over")
    day0 = t.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = day0.replace(day=1).timestamp()
    month = sum(r["usd"] - r.get("release", 0) for r in rows if r["ts"] >= month0)
    before = sum(r["usd"] - r.get("release", 0) for r in rows if month0 <= r["ts"] < day0.timestamp())
    days = math.ceil((CREDIT_END - day0).total_seconds() / 86400)
    share = (monthly - before) / days
    if daily is not None:
        share = min(share, daily)
    if month + est > monthly:
        raise ApiRefused(f"monthly cap: spent {month:.4f} + est {est:.4f} > {monthly:.2f} USD")
    if (month - before) + est > share:
        raise ApiRefused(f"daily share: spent {month - before:.4f} + est {est:.4f} > {share:.4f} USD")


@contextlib.contextmanager
def _locked(path):
    """One caller at a time across processes, held over check -> call -> ledger append."""
    import msvcrt
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)  # retries ~10 s, then OSError
        except OSError:
            raise ApiRefused("ledger busy") from None
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


def _append(path, row):
    data = (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, data)  # one write per row
    finally:
        os.close(fd)


def _text_messages(messages):
    return isinstance(messages, list) and bool(messages) and all(
        isinstance(m, dict) and set(m) == {"role", "content"} and m["role"] in ("user", "assistant")
        and isinstance(m["content"], str) for m in messages)


def call(model, system, messages, max_tokens, purpose, *, root=ROOT, clock=None, reader=None, post=None):
    """Response text of one Messages API call, or ApiRefused (nothing sent) / ApiError (sent, failed)."""
    if not isinstance(model, str) or model not in PRICES:
        raise ApiRefused("model not allowed (Haiku / Sonnet only)")
    if (not isinstance(system, str) or not _text_messages(messages) or isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int) or not 0 < max_tokens <= MAX_TOKENS_CAP):
        raise ApiRefused("arguments must be text only: system str, role/content str messages, max_tokens")
    if sys.platform != "win32":
        raise ApiRefused("Windows only (Credential Manager)")
    body = {"model": model, "max_tokens": max_tokens, "messages": messages}
    if system:
        body["system"] = system
    raw = json.dumps(body).encode("utf-8")
    p_in, p_out = PRICES[model]
    est = (len(raw) * p_in + max_tokens * p_out) / 1e6  # a byte is never less than a token
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / LEDGER_NAME
    with _locked(root / (LEDGER_NAME + ".lock")):
        now = (clock or time.time)()
        monthly, daily = caps(root)
        check_budget(now, read_ledger(ledger), monthly, daily, est)
        try:
            key = (reader or read_credential)()
        except Exception:
            key = ""
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise ApiRefused("credential unreadable") from None
        reserve = math.ceil(est * 1e6) / 1e6
        row = {"ts": now, "purpose": scrub(purpose)[:PURPOSE_MAX], "model": model, "in": 0, "out": 0}
        # booked BEFORE sending: a kill, Ctrl+C or failed append after the request keeps the estimate
        _append(ledger, {**row, "usd": reserve, "status": "sent"})
        try:
            status, data = (post or https_post)(
                API_URL, {"x-api-key": key, "anthropic-version": API_VERSION,
                          "content-type": "application/json"}, raw, TIMEOUT_S)
            failed = ""
        except Exception as e:  # never str(e); raised below, outside this block, so no __context__
            status, data, failed = None, b"", type(e).__name__
        del key  # no key in this frame's locals for any later traceback
        if failed:
            raise ApiError(f"transport failed: {failed}")  # maybe billed: the reserve stays booked
        try:
            resp = json.loads(data)
        except (TypeError, ValueError):
            resp = None
        row["status"] = status if isinstance(status, int) else "?"
        if not (isinstance(status, int) and 200 <= status < 300):
            _append(ledger, {**row, "usd": 0.0, "release": reserve})  # not billed
            err = resp.get("error") if isinstance(resp, dict) else None
            kind = scrub(err.get("type", ""))[:40] if isinstance(err, dict) else ""
            raise ApiError(f"API status {row['status']} {kind}".strip())
        usage = resp.get("usage") if isinstance(resp, dict) else None
        content = resp.get("content") if isinstance(resp, dict) else None
        n_in = usage.get("input_tokens") if isinstance(usage, dict) else None
        n_out = usage.get("output_tokens") if isinstance(usage, dict) else None
        if not (isinstance(n_in, int) and isinstance(n_out, int) and _num(n_in) and _num(n_out)
                and isinstance(content, list)):
            _append(ledger, {**row, "usd": 0.0, "release": 0.0})  # answered, unpriceable: reserve stays
            raise ApiError("API response without usage/content")
        row.update({"in": n_in, "out": n_out})
        _append(ledger, {**row, "usd": round((n_in * p_in + n_out * p_out) / 1e6, 6), "release": reserve})
    return "".join(b["text"] for b in content
                   if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
