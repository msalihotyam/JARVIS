"""The ONE place that touches the Anthropic API (APIBROKER).

call(model, system, messages, max_tokens, purpose) -> text. Guards, in order: model allowlist
(Haiku/Sonnet only), argument shape (text only: no streaming, tools or files), the money caps
(hard monthly cap, daily share toward the credit's expiry, both from the cost ledger), Windows,
then the key - read at call time from the Credential Manager generic credential
"konveyor-bekci" and nowhere else (never an env var, file or argv). The key is never stored,
logged or put in an exception. Stdlib only.
"""
import base64
import contextlib
import ctypes
import json
import math
import os
import re
import secrets
import stat
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
CREDIT_END = datetime(2026, 12, 18, tzinfo=timezone.utc)  # credit expiry; refuse from then on
MAX_TOKENS_CAP = 8192
PURPOSE_MAX = 80
LEDGER_NAME = "api_ledger.jsonl"
CAPS_NAME = "api_caps.json"
KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]+")
CALLER_CAPS_VERSION = 1  # bump on every change to the table below
# monthly USD cap per caller = the purpose's first word; the sum may pass MONTHLY_CAP_USD, which still binds
CALLER_CAPS = {"orch": 45.0, "paket": 10.0, "teshis": 10.0, "onkontrol": 10.0, "arastirma": 10.0,
               "ders": 8.0, "sinav": 60.0, "okuyucu": 5.0, "yedek": 7.0}
CALLER_CAP_MONTHS = {"sinav": (2026, 11)}  # (year, month) the cap holds in; 0 in every other month


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


CUT_NAME = "cut.json"  # CUTSWITCH1: the one cut switch (a jarvis/n6_breaker.py file); also read by conveyor hold()


def _breaker():
    """jarvis/n6_breaker.py by path: importing the jarvis package would pull in its heavy core."""
    mod = sys.modules.get("jarvis_n6_breaker")
    if mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "jarvis_n6_breaker", Path(__file__).resolve().parents[1] / "jarvis" / "n6_breaker.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["jarvis_n6_breaker"] = mod  # the dataclass resolves its module while executing
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop("jarvis_n6_breaker", None)
            raise
    return mod


def cut_path():
    """The ONE cut.json path: the broker and the conveyor's hold() both resolve it here, from the
    module ROOT read at call time; no caller (ledger) root redirects it."""
    return Path(ROOT) / CUT_NAME


def cut_set():
    """True when the cut switch is set; fail closed: unreadable switch or breaker = cut."""
    try:
        nb = _breaker()
        return nb.is_open(nb.load(cut_path()))
    except Exception:
        return True


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


def _override(root):
    """The optional override file as a validated dict ({} when absent); anything odd refuses."""
    p = Path(root) / CAPS_NAME
    if not p.exists():
        return {}
    try:
        o = json.loads(p.read_text(encoding="utf-8"), parse_constant=_no_constant)
    except (OSError, ValueError):
        raise ApiRefused(f"{CAPS_NAME} unreadable") from None
    cl = o.get("callers", {}) if isinstance(o, dict) else None
    if (not isinstance(o, dict) or set(o) - {"monthly_usd", "daily_usd", "callers"}
            or not all(_num(v) for k, v in o.items() if k != "callers")
            or not isinstance(cl, dict) or set(cl) - set(CALLER_CAPS) or not all(_num(v) for v in cl.values())):
        raise ApiRefused(f"{CAPS_NAME} invalid")  # fail closed, never back to the higher constant
    return o


def caps(root):
    """(monthly, daily or None): the constants, only ever LOWERED by the optional override file."""
    o = _override(root)
    return min(MONTHLY_CAP_USD, o.get("monthly_usd", MONTHLY_CAP_USD)), o.get("daily_usd")


def caller_of(purpose):
    """The purpose's first word (up to a space, ':' or '/') when it names a known caller, else ""."""
    name = re.split(r"[\s:/]", str(purpose), maxsplit=1)[0]
    return name if name in CALLER_CAPS else ""


def caller_cap(root, name, now):
    """The caller's monthly cap at `now`, only ever LOWERED by the override file's "callers"."""
    t = datetime.fromtimestamp(now, timezone.utc)
    month = CALLER_CAP_MONTHS.get(name)
    cap = 0.0 if month and month != (t.year, t.month) else CALLER_CAPS[name]
    return min(cap, _override(root).get("callers", {}).get(name, cap))


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
                    or not _num(r.get("release", 0)) or not _num(r.get("ceiling", 0))
                    or not isinstance(r.get("envelope", ""), str) or not isinstance(r.get("purpose", ""), str)):
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


def _guard(now, rows, monthly, daily, ccap, caller, need, envelope=None):
    """check_budget and the caller's cap, with every envelope opened today holding its unspent
    remainder: a held balance is never available twice. A call inside an envelope spends that
    envelope's hold instead, and is refused once `need` passes the envelope's remainder."""
    t = datetime.fromtimestamp(now, timezone.utc)
    day0 = t.replace(hour=0, minute=0, second=0, microsecond=0)
    month0 = day0.replace(day=1).timestamp()
    env = {}  # id -> [remainder, caller]; an envelope lives until the end of its UTC day
    for r in rows:
        if "ceiling" in r and r["ts"] >= day0.timestamp():
            env[r["envelope"]] = [r["ceiling"], caller_of(r.get("purpose", ""))]
    for r in rows:
        if "ceiling" not in r and r.get("envelope") in env:
            env[r["envelope"]][0] -= r["usd"] - r.get("release", 0)
    own = 0.0
    if envelope is not None:
        if not isinstance(envelope, str) or envelope not in env:
            raise ApiRefused("envelope unknown or expired")
        own, owner = env[envelope]
        if owner != caller:
            raise ApiRefused("envelope belongs to another caller")
        if need > own:
            raise ApiRefused(f"envelope: remaining {own:.4f} < est {need:.4f} USD")
    extra = max(need - own, 0)  # 0 inside an envelope: its hold is already counted below
    check_budget(now, rows, monthly, daily, sum(max(v[0], 0) for v in env.values()) + extra)
    spent = sum(r["usd"] - r.get("release", 0) for r in rows
                if r["ts"] >= month0 and caller_of(r.get("purpose", "")) == caller)
    held = sum(max(v[0], 0) for v in env.values() if v[1] == caller)
    if spent + held + extra > ccap:
        raise ApiRefused(f"caller cap {caller}: spent {spent:.4f} + held {held:.4f} + est {extra:.4f} "
                         f"> {ccap:.2f} USD")


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


def _caller(purpose):
    name = caller_of(purpose) if isinstance(purpose, str) else ""
    if not name:
        raise ApiRefused(f"unknown caller: purpose must start with one of {', '.join(CALLER_CAPS)}")
    return name


def open_envelope(ceiling_usd, purpose, *, root=None, clock=None):
    """Id of a new batch envelope: `ceiling_usd` held for the purpose's caller until the end of
    today (UTC), booked in the ledger so it survives a restart; refused when the remaining
    monthly / daily / caller budget cannot cover the whole ceiling now."""
    if cut_set():
        raise ApiRefused("cut switch set: every API call and new lane dispatch is held")
    name = _caller(purpose)
    if not _num(ceiling_usd) or not ceiling_usd > 0:
        raise ApiRefused("envelope ceiling must be a positive USD number")
    root = Path(ROOT if root is None else root)
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / LEDGER_NAME
    with _locked(root / (LEDGER_NAME + ".lock")):
        now = (clock or time.time)()
        monthly, daily = caps(root)
        _guard(now, read_ledger(ledger), monthly, daily, caller_cap(root, name, now), name, ceiling_usd)
        eid = secrets.token_hex(8)
        _append(ledger, {"ts": now, "purpose": scrub(purpose)[:PURPOSE_MAX], "envelope": eid,
                         "ceiling": ceiling_usd, "usd": 0.0, "status": "envelope"})
    return eid


def call(model, system, messages, max_tokens, purpose, *, root=None, clock=None, reader=None, post=None,
         envelope=None):
    """Response text of one Messages API call, or ApiRefused (nothing sent) / ApiError (sent, failed).
    With `envelope` (from open_envelope, same caller) the call spends that envelope's remainder."""
    if cut_set():
        raise ApiRefused("cut switch set: every API call and new lane dispatch is held")
    name = _caller(purpose)
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
    reserve = math.ceil(est * 1e6) / 1e6
    root = Path(ROOT if root is None else root)
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / LEDGER_NAME
    with _locked(root / (LEDGER_NAME + ".lock")):
        now = (clock or time.time)()
        monthly, daily = caps(root)
        _guard(now, read_ledger(ledger), monthly, daily, caller_cap(root, name, now), name, reserve, envelope)
        try:
            key = (reader or read_credential)()
        except Exception:
            key = ""
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise ApiRefused("credential unreadable") from None
        row = {"ts": now, "purpose": scrub(purpose)[:PURPOSE_MAX], "model": model, "in": 0, "out": 0}
        if envelope is not None:
            row["envelope"] = envelope  # every row of this call debits the envelope
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


# --- Per-purpose monthly envelopes, Batch and image input (APIBROKER2). The one ledger, its lock,
# the hard cap, the daily share and the key reader above are reused unchanged; call() is untouched.
ENVELOPES_NAME = "api_envelopes.json"  # optional override under ROOT; any fault refuses every call
ENVELOPE_LIMITS = {"prescreen": 60.0, "night": 60.0, "visual": 10.0, "watch": 10.0}
# the plan total the envelopes split; overflow gets the rest (0 until the config names a larger total)
ENVELOPES_DEFAULT = {"monthly_usd": sum(ENVELOPE_LIMITS.values()), **ENVELOPE_LIMITS}
PURPOSES = (*ENVELOPE_LIMITS, "overflow")
BATCH_PURPOSES = ("prescreen", "night", "overflow")
IMAGE_PURPOSES = ("visual",)
BATCH_RATE = 0.5  # Message Batches bill half the list price
BATCH_MAX_REQUESTS = 100
BATCH_MAX_BYTES = 1 << 20
BATCH_EXPIRE_S = 48 * 3600
BATCH_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
CUSTOM_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
REF_RE = re.compile(r"[0-9a-f]{16}")
IMAGE_MAX_BYTES = 5 << 20
IMAGE_MAX_COUNT = 4
IMAGE_ROOT = Path(ROOT).parent  # build/ of the main checkout: the only place an image is read from
# worst-case input tokens of one image, whatever its byte size: the server downsizes to a long edge
# of 1568 px (about 1600 tokens, w*h/750) and high-resolution models to 2576 px (about 4800 tokens)
IMAGE_MAX_TOKENS = 5000
BATCH_RESULT_TYPES = ("succeeded", "errored", "canceled", "expired")
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}  # start-of-frame markers


def _up(usd):
    """usd rounded UP to the ledger's 1e-6: a booked charge is never below the true cost."""
    return math.ceil(round(usd * 1e6, 6)) / 1e6


def _usd(x):
    """x as a finite float >= 0, or None (bool, str, NaN, inf, negative and 10**400 all refuse)."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        x = float(x)
    except OverflowError:
        return None
    return x if math.isfinite(x) and x >= 0 else None


def envelopes(config=None):
    """{purpose: monthly USD}; overflow = monthly_usd minus the named envelopes. Any fault refuses."""
    c = ENVELOPES_DEFAULT if config is None else config
    if not isinstance(c, dict) or set(c) != set(ENVELOPES_DEFAULT):
        raise ApiRefused("envelope config: keys must be exactly " + ", ".join(ENVELOPES_DEFAULT))
    v = {k: _usd(c[k]) for k in ENVELOPES_DEFAULT}
    if None in v.values():
        raise ApiRefused("envelope config: every value must be a finite USD number >= 0")
    rest = round(v.pop("monthly_usd") - sum(v.values()), 6)
    if rest < 0:
        raise ApiRefused("envelope config: envelopes sum above the monthly total")
    return {**v, "overflow": rest}


def _envelope_config(root):
    p = Path(root) / ENVELOPES_NAME
    if not p.exists():
        return envelopes()
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"), parse_constant=_no_constant)
    except (OSError, ValueError):
        raise ApiRefused("envelope config unreadable") from None
    return envelopes(cfg)


def _purpose(purpose, allowed, what):
    """The exact purpose name: case, padding and prefixes refuse, like an unknown name."""
    if not isinstance(purpose, str) or purpose not in PURPOSES:
        raise ApiRefused("unknown purpose: must be exactly one of " + ", ".join(PURPOSES))
    if purpose not in allowed:
        raise ApiRefused(f"purpose {purpose} not allowed for {what}")
    return purpose


def _model(model):
    """(input, output) list price per Mtok; Fable is refused by name, anything unpriced after it."""
    if not isinstance(model, str) or "fable" in model.casefold():
        raise ApiRefused("model not allowed (Haiku / Sonnet only): Fable is never used over the API")
    if model not in PRICES:
        raise ApiRefused("model not allowed (Haiku / Sonnet only): no price row")
    return PRICES[model]


def _pool_guard(root, now, rows, purpose, need):
    """The purpose's monthly envelope, then _guard: today's holds, the hard cap and the daily share."""
    limit = _envelope_config(root)[purpose]
    t = datetime.fromtimestamp(now, timezone.utc)
    month0 = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    spent = sum(r["usd"] - r.get("release", 0) for r in rows
                if r["ts"] >= month0 and "ceiling" not in r and r.get("purpose") == purpose)
    if spent + need > limit:
        raise ApiRefused(f"envelope {purpose}: spent {spent:.4f} + est {need:.4f} > {limit:.2f} USD")
    monthly, daily = caps(root)
    _guard(now, rows, monthly, daily, math.inf, "", need)  # "" is no caller: the caller caps do not apply


def _key(reader):
    """The key from the same reader as call(), read now; never stored, never in a message."""
    try:
        key = (reader or read_credential)()
    except Exception:
        key = ""
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise ApiRefused("credential unreadable") from None
    return key


def _exchange(transport, url, key, raw):
    """(status, body bytes, failed exception type name); never str(e), so no key in any message."""
    try:
        status, data = transport(url, {"x-api-key": key, "anthropic-version": API_VERSION,
                                       "content-type": "application/json"}, raw, TIMEOUT_S)
        return status, data, ""
    except Exception as e:
        return None, b"", type(e).__name__


def _ok(status):
    return isinstance(status, int) and not isinstance(status, bool) and 200 <= status < 300


def _json(data):
    try:
        return json.loads(data, parse_constant=_no_constant)
    except (TypeError, ValueError, RecursionError):  # a deeply nested answer is malformed, not a crash
        return None


def _usage(msg):
    """((input tokens, output tokens), text) of one message, or None when either is missing."""
    usage = msg.get("usage") if isinstance(msg, dict) else None
    content = msg.get("content") if isinstance(msg, dict) else None
    n = [usage.get(k) if isinstance(usage, dict) else None for k in ("input_tokens", "output_tokens")]
    # bounded ints: a 10**400 count would overflow, an inf price would make the ledger unreadable
    if not (all(isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 10**12 for x in n)
            and isinstance(content, list)):
        return None
    return tuple(n), "".join(b["text"] for b in content if isinstance(b, dict)
                             and b.get("type") == "text" and isinstance(b.get("text"), str))


def https_get(url, headers, body, timeout):
    """GET on the same no-redirect, no-proxy opener as https_post; `body` is always None."""
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(req, timeout=timeout) as r:  # nosec B310 - fixed https URL
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _handle_path(f):
    """The final path of an open file from its handle (Windows): what was opened, not what was named."""
    import msvcrt
    fn = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
    fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    fn.restype = ctypes.c_uint32
    buf = ctypes.create_unicode_buffer(32768)
    n = fn(msvcrt.get_osfhandle(f.fileno()), buf, 32768, 0)
    s = buf.value
    if not 0 < n < 32768 or not s.startswith("\\\\?\\") or s.startswith("\\\\?\\UNC\\"):
        raise OSError("final path unavailable")
    return Path(s[4:])


def _jpeg(data):
    """SOI, then length-carrying marker segments up to a whole start-of-frame segment before any scan."""
    i = 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        m, n = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
        if m == 0xFF:  # fill byte
            i += 1
            continue
        if n < 2 or m == 0x01 or 0xD0 <= m <= 0xDA:  # standalone, SOI, EOI or scan before a frame
            return False
        if m in _JPEG_SOF:
            return n >= 8 and i + 2 + n <= len(data)
        i += 2 + n
    return False


def _image(path):
    """(media type, bytes) of a regular PNG / JPEG file under IMAGE_ROOT; any doubt refuses."""
    s = os.fspath(path) if isinstance(path, (str, os.PathLike)) else None
    if not isinstance(s, str) or not s or "\x00" in s or s[:2].replace("/", "\\") == "\\\\" or ":" in s[2:]:
        raise ApiRefused("image path refused")  # NUL, UNC / device paths, alternate data streams
    try:
        base = Path(IMAGE_ROOT).resolve(strict=True)
        p = Path(s).resolve(strict=True)  # a link out of build/ resolves out of it, then refuses
        inside, regular = p.is_relative_to(base), p.is_file()
    except (OSError, ValueError, RuntimeError):
        raise ApiRefused("image path refused") from None
    if not inside or not regular:
        raise ApiRefused("image path must be a file inside build/")
    try:
        f = open(p, "rb")
    except OSError:
        raise ApiRefused("image unreadable") from None
    with f:
        # confinement again on the opened handle: a swap between the check and open() refuses
        try:
            st = os.fstat(f.fileno())
            ok = stat.S_ISREG(st.st_mode) and _handle_path(f).is_relative_to(base)
        except (OSError, ValueError, RuntimeError):
            ok = False
        if not ok:
            raise ApiRefused("image path must be a file inside build/")
        if st.st_size > IMAGE_MAX_BYTES:
            raise ApiRefused(f"image over {IMAGE_MAX_BYTES} bytes")
        try:
            data = f.read(IMAGE_MAX_BYTES + 1)
        except OSError:
            raise ApiRefused("image unreadable") from None
    if len(data) > IMAGE_MAX_BYTES:
        raise ApiRefused(f"image over {IMAGE_MAX_BYTES} bytes")
    if data.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR") and len(data) >= 33:  # sig + IHDR + CRC
        return "image/png", data
    if data.startswith(b"\xff\xd8") and _jpeg(data):
        return "image/jpeg", data
    raise ApiRefused("image must be PNG or JPEG")


def _text_args(system, messages, max_tokens):
    if (not isinstance(system, str) or not _text_messages(messages) or isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int) or not 0 < max_tokens <= MAX_TOKENS_CAP):
        raise ApiRefused("arguments must be text only: system str, role/content str messages, max_tokens")


def _booked(purpose, model, url, raw, reserve, settle, *, root, clock, reader, post, extra=None):
    """call()'s shape for an envelope purpose: check + reserve as ONE step under the ledger lock,
    then send, then the settle row. A transport failure leaves the reserve booked as spent."""
    root = Path(ROOT if root is None else root)
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / LEDGER_NAME
    with _locked(root / (LEDGER_NAME + ".lock")):
        now = (clock or time.time)()
        _pool_guard(root, now, read_ledger(ledger), purpose, reserve)
        key = _key(reader)
        row = {"ts": now, "purpose": purpose, "model": model, "in": 0, "out": 0, **(extra or {})}
        _append(ledger, {**row, "usd": reserve, "status": "sent"})
        status, data, failed = _exchange(post or https_post, url, key, raw)
        del key
        if failed:
            raise ApiError(f"transport failed: {failed}")
        resp = _json(data)
        row["status"] = status if isinstance(status, int) and not isinstance(status, bool) else "?"
        if not _ok(status):
            _append(ledger, {**row, "usd": 0.0, "release": reserve})
            err = resp.get("error") if isinstance(resp, dict) else None
            kind = err.get("type") if isinstance(err, dict) and isinstance(err.get("type"), str) else ""
            raise ApiError(f"API status {row['status']} {scrub(kind)[:40]}".strip())
        fields, result = settle(resp, reserve)
        _append(ledger, {**row, **fields})
    if isinstance(result, ApiError):
        raise result
    return result


def send(purpose, model, system, messages, max_tokens, *, images=(), root=None,
         clock=None, reader=None, post=None):
    """Response text of one Messages API call charged to a monthly envelope (`purpose` is exactly
    one of PURPOSES); `images` (PNG / JPEG paths under build/) only for purpose visual. Refused
    before sending when the worst case passes the envelope, the daily share or the hard cap."""
    if cut_set():
        raise ApiRefused("cut switch set: every API call and new lane dispatch is held")
    _purpose(purpose, PURPOSES, "calls")
    p_in, p_out = _model(model)
    _text_args(system, messages, max_tokens)
    if not isinstance(images, (list, tuple)):
        raise ApiRefused("images must be a list of paths")
    if images:
        _purpose(purpose, IMAGE_PURPOSES, "images")
        if len(images) > IMAGE_MAX_COUNT:
            raise ApiRefused(f"at most {IMAGE_MAX_COUNT} images")
        if messages[-1]["role"] != "user":
            raise ApiRefused("images go with the last user message")
    if sys.platform != "win32":
        raise ApiRefused("Windows only (Credential Manager)")
    msgs = list(messages)
    if images:
        blocks = []
        for p in images:
            kind, data = _image(p)
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": kind,
                                                        "data": base64.b64encode(data).decode("ascii")}})
        msgs[-1] = {"role": "user", "content": [*blocks, {"type": "text", "text": msgs[-1]["content"]}]}
    body = {"model": model, "max_tokens": max_tokens, "messages": msgs}
    if system:
        body["system"] = system
    raw = json.dumps(body).encode("utf-8")
    # worst case at list price: a request byte is never less than a token, and an image is billed by
    # its pixels, not its bytes, so each one adds IMAGE_MAX_TOKENS
    reserve = _up((len(raw) + len(images) * IMAGE_MAX_TOKENS) * p_in / 1e6 + max_tokens * p_out / 1e6)

    def settle(resp, reserve):
        got = _usage(resp)
        if got is None:  # the reserve stays booked as spent
            return {"usd": 0.0, "release": 0.0}, ApiError("API response without usage/content")
        (n_in, n_out), text = got
        return {"in": n_in, "out": n_out, "usd": _up((n_in * p_in + n_out * p_out) / 1e6),
                "release": reserve}, text
    return _booked(purpose, model, API_URL, raw, reserve, settle,
                   root=root, clock=clock, reader=reader, post=post)


def submit(purpose, requests, *, root=None, clock=None, reader=None, post=None):
    """Local id of one Message Batch. The worst case at the batch rate is reserved now and
    settled by collect(id); each request is {custom_id, model, messages, max_tokens[, system]}."""
    if cut_set():
        raise ApiRefused("cut switch set: every API call and new lane dispatch is held")
    _purpose(purpose, BATCH_PURPOSES, "batch")
    if not isinstance(requests, list) or not 0 < len(requests) <= BATCH_MAX_REQUESTS:
        raise ApiRefused(f"batch: 1 to {BATCH_MAX_REQUESTS} requests")
    need = {"custom_id", "model", "messages", "max_tokens"}
    items, seen, models, worst = [], set(), set(), 0.0
    for r in requests:
        if not isinstance(r, dict) or not need <= set(r) <= need | {"system"}:
            raise ApiRefused("batch request: custom_id, model, messages, max_tokens[, system]")
        cid, mt, system = r["custom_id"], r["max_tokens"], r.get("system", "")
        if not isinstance(cid, str) or not CUSTOM_ID_RE.fullmatch(cid) or cid in seen:
            raise ApiRefused("batch request: custom_id must be unique [A-Za-z0-9_-]{1,64}")
        p_in, p_out = _model(r["model"])
        _text_args(system, r["messages"], mt)
        seen.add(cid)
        models.add(r["model"])
        params = {"model": r["model"], "max_tokens": mt, "messages": r["messages"]}
        if system:
            params["system"] = system
        worst += (len(json.dumps(params).encode("utf-8")) * p_in + mt * p_out) * BATCH_RATE / 1e6
        items.append({"custom_id": cid, "params": params})
    raw = json.dumps({"requests": items}).encode("utf-8")
    if len(raw) > BATCH_MAX_BYTES:
        raise ApiRefused(f"batch over {BATCH_MAX_BYTES} bytes")
    if sys.platform != "win32":
        raise ApiRefused("Windows only (Credential Manager)")
    ref = secrets.token_hex(8)

    def settle(resp, reserve):
        bid = resp.get("id") if isinstance(resp, dict) else None
        if isinstance(bid, str) and BATCH_ID_RE.fullmatch(bid):
            return {"usd": 0.0, "server": bid, "status": "batch submitted"}, ref
        return ({"usd": 0.0, "status": "batch unaccepted", "final": True},
                ApiError("API batch response without an id"))
    return _booked(purpose, ",".join(sorted(models)), API_URL + "/batches", raw,
                   math.ceil(worst * 1e6) / 1e6, settle, root=root, clock=clock, reader=reader, post=post,
                   extra={"batch": ref, "n": len(items),
                          "models": {i["custom_id"]: i["params"]["model"] for i in items}})


def _batch_results(data, models):
    """({custom_id: text, or None when not succeeded}, USD at the batch rate) or None on any fault.
    `models` is {custom_id: model} as submitted: the answer must name exactly those ids, and a
    succeeded one the submitted model, priced from the submission, never from the answer."""
    if not isinstance(models, dict) or not models or not all(m in PRICES for m in models.values()):
        return None
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else None
    except UnicodeDecodeError:
        return None
    if text is None:
        return None
    out, usd = {}, 0.0
    for line in text.split("\n"):  # not splitlines: U+2028 / U+0085 are legal inside a JSON string
        if not line.strip():
            continue
        r = _json(line)
        cid = r.get("custom_id") if isinstance(r, dict) else None
        res = r.get("result") if isinstance(r, dict) else None
        if (not isinstance(cid, str) or cid not in models or cid in out or not isinstance(res, dict)
                or res.get("type") not in BATCH_RESULT_TYPES):  # an unknown type may still bill
            return None
        if res.get("type") != "succeeded":
            out[cid] = None
            continue
        msg = res.get("message")
        got = _usage(msg)
        if not isinstance(msg, dict) or msg.get("model") != models[cid] or got is None:
            return None
        (n_in, n_out), out[cid] = got
        p_in, p_out = PRICES[models[cid]]
        usd += (n_in * p_in + n_out * p_out) * BATCH_RATE / 1e6
    return (out, usd) if set(out) == set(models) else None


def collect(ref, *, root=None, clock=None, reader=None, get=None):
    """{"status": settled | pending | expired | unpriced | unaccepted | failed[, "results": ...]}.
    Settles once under the ledger lock; a missing, repeated or malformed answer never raises and
    leaves the submit reserve booked as spent (after 48 h uncollected: expired at that amount)."""
    if not isinstance(ref, str) or not REF_RE.fullmatch(ref):
        raise ApiRefused("unknown batch")
    root = Path(ROOT if root is None else root)
    root.mkdir(parents=True, exist_ok=True)
    ledger = root / LEDGER_NAME
    with _locked(root / (LEDGER_NAME + ".lock")):
        rows = [r for r in read_ledger(ledger) if r.get("batch") == ref]
        if not rows:
            raise ApiRefused("unknown batch")
        for r in rows:
            if r.get("final"):
                s = r.get("status")
                return {"status": s[6:] if isinstance(s, str) and s.startswith("batch ") else "failed"}
        sent = rows[0]
        server = next((r["server"] for r in rows if isinstance(r.get("server"), str)
                       and BATCH_ID_RE.fullmatch(r["server"])), None)
        if server is None:
            return {"status": "failed"}
        now = (clock or time.time)()

        def final(status, usd, release, results=None):
            # ts of the submit: the spend stays on the day (and month) it was reserved
            _append(ledger, {"ts": sent["ts"], "at": now, "purpose": sent.get("purpose", ""),
                             "model": sent.get("model", ""), "in": 0, "out": 0, "batch": ref,
                             "usd": usd, "release": release, "status": "batch " + status, "final": True})
            return {"status": status, **({} if results is None else {"results": results})}
        if now - sent["ts"] > BATCH_EXPIRE_S:
            return final("expired", 0.0, 0.0)
        if cut_set():
            return {"status": "pending"}
        try:
            key = _key(reader)
        except ApiRefused:
            return {"status": "pending"}
        url = API_URL + "/batches/" + server  # never a URL from the answer
        status, data, _ = _exchange(get or https_get, url, key, None)
        info = _json(data) if _ok(status) else None
        if isinstance(info, dict) and info.get("processing_status") == "ended":
            status, data, _ = _exchange(get or https_get, url + "/results", key, None)
        else:
            status = None
        del key
        if not _ok(status):
            return {"status": "pending"}
        parsed = _batch_results(data, sent.get("models"))
        # a short (truncated) answer must not release the reserve of the missing requests
        if parsed is None or len(parsed[0]) != sent.get("n"):
            return final("unpriced", 0.0, 0.0)
        return final("settled", _up(parsed[1]), sent["usd"], parsed[0])


def report(month, *, root=None):
    """{"by_purpose": {name: USD}, "by_day": {YYYY-MM-DD: USD}, "total": USD} for one UTC month."""
    if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ApiRefused("month must be YYYY-MM")
    by_p, by_d = {}, {}
    for r in read_ledger(Path(ROOT if root is None else root) / LEDGER_NAME):
        if "ceiling" in r:
            continue
        try:
            t = datetime.fromtimestamp(r["ts"], timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise ApiRefused("ledger row invalid") from None
        if t.strftime("%Y-%m") != month:
            continue
        p = r.get("purpose", "")
        p = p if p in PURPOSES else (caller_of(p) or "other")
        net = r["usd"] - r.get("release", 0)
        by_p[p] = by_p.get(p, 0.0) + net
        by_d[t.strftime("%Y-%m-%d")] = by_d.get(t.strftime("%Y-%m-%d"), 0.0) + net
    return {"by_purpose": {k: round(v, 6) for k, v in by_p.items()},
            "by_day": {k: round(by_d[k], 6) for k in sorted(by_d)}, "total": round(sum(by_p.values()), 6)}


def main(argv=None):
    """`conveyor_api.py report YYYY-MM`: the weekly review table (purpose, limit, spent, left; by day)."""
    a = sys.argv[1:] if argv is None else list(argv)
    if len(a) != 2 or a[0] != "report":
        print("usage: conveyor_api.py report YYYY-MM", file=sys.stderr)
        return 2
    try:
        rep = report(a[1])
        limits = _envelope_config(Path(ROOT))
        monthly, _ = caps(Path(ROOT))
    except ApiRefused as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(f"{'purpose':<10} {'limit':>8} {'spent':>10} {'left':>10}")
    for p in (*PURPOSES, *sorted(set(rep["by_purpose"]) - set(PURPOSES))):
        spent, lim = rep["by_purpose"].get(p, 0.0), limits.get(p)
        left = f"{lim - spent:>10.4f}" if lim is not None else f"{'-':>10}"
        print(f"{p:<10} {(f'{lim:.2f}' if lim is not None else '-'):>8} {spent:>10.4f} {left}")
    for d, v in rep["by_day"].items():
        print(f"{d}  {v:.4f}")
    print(f"total {rep['total']:.4f} of hard cap {monthly:.2f} USD")
    return 0


if __name__ == "__main__":
    sys.exit(main())
