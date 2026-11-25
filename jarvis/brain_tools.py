"""BRAINMCP1: the brain's only tools, a tiny stdio JSON-RPC 2.0 server (MCP style).

Exactly four tools: read_file, list_dir, search, propose. The three read tools
go through injected path functions (the BRAINFS1 path policy), propose goes
through the injected inbox validator (TALKINBOX1) and only an accepted record
reaches the injected store. The server opens no file, runs no process and
makes no network call. Nothing in the running app imports this module.

Injected contract (see Tools): a path function returns the text to show, or a
Refused(code); a validator returns True to accept, or a Refused(code); store
returns the record name. Anything else is a fixed-text tool error.
"""

import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Callable

MAX_LINE_BYTES = 256 * 1024  # one incoming message
CONTENT_CAP = 32_000  # characters of tool text per reply
# ensure_ascii escapes one character to at most 12 bytes (astral = surrogate pair)
MAX_REPLY_BYTES = CONTENT_CAP * 12 + 4096
MAX_DEPTH = 8  # whole message nesting
MAX_PROPOSALS = 20  # per session
MAX_REQUESTS = 10_000  # ids remembered per session
MAX_RECORD_BYTES = 64 * 1024
MAX_PATH_CHARS = 1024
MAX_QUERY_CHARS = 512
MAX_ID_CHARS = 128

TOOL_NAMES = ("read_file", "list_dir", "search", "propose")

# per tool: argument -> (type, required)
_ARGS = {
    "read_file": {"path": (str, True), "offset": (int, False)},
    "list_dir": {"path": (str, True), "offset": (int, False)},
    "search": {"query": (str, True), "path": (str, False), "offset": (int, False)},
    "propose": {"record": (str, True)},
}
_LIMITS = {"path": MAX_PATH_CHARS, "query": MAX_QUERY_CHARS}
_DESCRIPTIONS = {
    "read_file": "Read a text file under the project root.",
    "list_dir": "List a folder under the project root.",
    "search": "Literal text search under the project root.",
    "propose": "Put one proposal record into the inbox.",
}
_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_VERSION = re.compile(r"[\x21-\x7e]{1,32}")

PARSE_ERROR, INVALID_REQUEST, NO_METHOD, INVALID_PARAMS, INTERNAL = (
    -32700, -32600, -32601, -32602, -32603)
NOT_INITIALIZED = -32002


class Refused:
    """What an injected function returns (never raises) to refuse a call."""

    def __init__(self, code):
        self.code = code


@dataclass(frozen=True)
class Tools:
    read_file: Callable  # (path) -> str | Refused
    list_dir: Callable  # (path) -> str | Refused
    search: Callable  # (query, path) -> str | Refused
    validate: Callable  # (record bytes) -> True | Refused
    store: Callable  # (record bytes) -> record name str


def tool_list():
    """A fresh copy every time: there is no way to add a tool at run time."""
    tools = []
    for name in TOOL_NAMES:
        spec = _ARGS[name]
        props = {k: {"type": "string" if t is str else "integer"} for k, (t, _) in spec.items()}
        for k in props:
            if k == "offset":
                props[k]["minimum"] = 0
            elif k in _LIMITS:
                props[k]["maxLength"] = _LIMITS[k]
        tools.append({
            "name": name,
            "description": _DESCRIPTIONS[name],
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": [k for k, (_, req) in spec.items() if req],
                "additionalProperties": False,
            },
        })
    return tools


def _no_dupes(pairs):
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _no_constant(_name):
    raise ValueError("constant")


def _too_deep(obj):
    stack = [(obj, 1)]
    while stack:
        value, depth = stack.pop()
        if isinstance(value, (dict, list)):
            if depth > MAX_DEPTH:
                return True
            children = value.values() if isinstance(value, dict) else value
            stack.extend((c, depth + 1) for c in children)
    return False


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _result(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _tool_error(code):
    return {"content": [{"type": "text", "text": "refused: " + code}], "isError": True}


def _tool_text(text, offset):
    chunk = text[offset:offset + CONTENT_CAP]
    content = [{"type": "text", "text": chunk}]
    end = offset + len(chunk)
    if end < len(text):
        content.append({"type": "text", "text": "[truncated] next_offset=%d" % end})
    return {"content": content, "isError": False}


def encode(message):
    """One reply as one ASCII line; a newline inside any string is escaped."""
    line = json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    if len(line) > MAX_REPLY_BYTES:  # cannot happen with CONTENT_CAP; fail closed anyway
        line = json.dumps(_error(None, INTERNAL, "reply too large"), separators=(",", ":"))
    return line.encode("ascii") + b"\n"


class Session:
    """State of one stdio session: initialize flag, seen ids, counters."""

    def __init__(self, tools):
        self._tools = tools
        self._initialized = False
        self._seen = set()
        self._proposals = 0
        self._calls = Counter()
        self._refusals = 0
        self._errors = 0

    def stats(self):
        """Counters for the service log."""
        return {"calls": {n: self._calls[n] for n in TOOL_NAMES},
                "refusals": self._refusals, "errors": self._errors}

    def handle_line(self, raw):
        """One raw line (bytes) -> reply dict, or None for no reply."""
        try:
            return self._handle(raw)
        except Exception:  # fail closed: nothing escapes, no exception text
            return _error(None, INTERNAL, "internal error")

    def _handle(self, raw):
        try:
            text = raw.decode("utf-8").rstrip("\r\n")
            if not text.strip():
                return None
            msg = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_no_constant)
        except Exception:  # bad UTF-8, bad JSON, duplicate keys, NaN, RecursionError
            return _error(None, PARSE_ERROR, "parse error")
        if isinstance(msg, list):
            return _error(None, INVALID_REQUEST, "batch not supported")
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or _too_deep(msg):
            return _error(None, INVALID_REQUEST, "invalid request")
        if "method" not in msg:
            if "result" in msg or "error" in msg:
                return None  # reply-shaped message from the client: never answered
            return _error(None, INVALID_REQUEST, "invalid request")
        method = msg["method"]
        if type(method) is not str:
            return _error(None, INVALID_REQUEST, "invalid request")
        if "id" not in msg:
            return None  # notification: no reply, nothing runs
        rid = msg["id"]
        if not ((type(rid) is int and abs(rid) < 2**53)
                or (type(rid) is str and len(rid) <= MAX_ID_CHARS)):
            return _error(None, INVALID_REQUEST, "invalid id")
        key = (type(rid).__name__, rid)
        if key in self._seen:
            return _error(None, INVALID_REQUEST, "duplicate id")
        if len(self._seen) >= MAX_REQUESTS:
            return _error(None, INVALID_REQUEST, "session limit")
        self._seen.add(key)
        params = msg.get("params", {})
        if type(params) is not dict:
            return _error(rid, INVALID_PARAMS, "invalid params")
        if method == "ping":
            return _result(rid, {})
        if method == "initialize":
            return self._initialize(rid, params)
        if method not in ("tools/list", "tools/call"):
            return _error(rid, NO_METHOD, "method not found")
        if not self._initialized:
            return _error(rid, NOT_INITIALIZED, "not initialized")
        if method == "tools/list":
            return _result(rid, {"tools": tool_list()})
        return self._call(rid, params)

    def _initialize(self, rid, params):
        if self._initialized:
            return _error(rid, INVALID_REQUEST, "already initialized")
        version = params.get("protocolVersion")
        if type(version) is not str or not _VERSION.fullmatch(version):
            return _error(rid, INVALID_PARAMS, "invalid protocolVersion")
        self._initialized = True
        return _result(rid, {"protocolVersion": version,
                             "capabilities": {"tools": {"listChanged": False}},
                             "serverInfo": {"name": "jarvis-brain", "version": "1"}})

    def _call(self, rid, params):
        name = params.get("name")
        if (type(name) is not str or name not in TOOL_NAMES
                or not set(params) <= {"name", "arguments", "_meta"}):
            return _error(rid, INVALID_PARAMS, "unknown tool")
        self._calls[name] += 1
        args = params.get("arguments", {})
        spec = _ARGS[name]
        if type(args) is not dict or not set(args) <= set(spec):
            return _result(rid, self._fail("bad_arguments"))
        for key, (kind, required) in spec.items():
            if key not in args:
                if required:
                    return _result(rid, self._fail("bad_arguments"))
                continue
            value = args[key]
            if type(value) is not kind or (kind is int and not 0 <= value < 2**53):
                return _result(rid, self._fail("bad_arguments"))
            if kind is str and len(value) > _LIMITS.get(key, MAX_RECORD_BYTES):
                return _result(rid, self._fail("bad_arguments"))
        if name == "propose":
            return _result(rid, self._propose(args["record"]))
        try:
            if name == "search":
                out = self._tools.search(args["query"], args.get("path", ""))
            else:
                out = getattr(self._tools, name)(args["path"])
        except Exception:  # RecursionError included; no exception text or path echoed
            return _result(rid, self._fail("tool_failed"))
        if isinstance(out, Refused):
            return _result(rid, self._refuse(out))
        if type(out) is not str:
            return _result(rid, self._fail("bad_result"))
        return _result(rid, _tool_text(out, args.get("offset", 0)))

    def _propose(self, record):
        if self._proposals >= MAX_PROPOSALS:
            return self._refuse(Refused("propose_cap"))
        self._proposals += 1
        try:
            data = record.encode("utf-8")
        except UnicodeEncodeError:  # lone surrogate
            return self._fail("bad_arguments")
        try:
            verdict = self._tools.validate(data)
        except Exception:
            return self._fail("validator_failed")
        if isinstance(verdict, Refused):
            return self._refuse(verdict)
        if verdict is not True:
            return self._fail("validator_failed")
        try:
            stored = self._tools.store(data)
        except Exception:
            return self._fail("store_failed")
        if type(stored) is not str or not _CODE.fullmatch(stored):
            return self._fail("store_failed")
        return {"content": [{"type": "text", "text": "stored: " + stored}], "isError": False}

    def _refuse(self, refusal):
        self._refusals += 1
        code = refusal.code
        return _tool_error(code if type(code) is str and _CODE.fullmatch(code) else "refused")

    def _fail(self, code):
        self._errors += 1
        return _tool_error(code)


def _read_message(reader):
    """Next line as bytes, b"" at EOF, or None when over the cap (rest drained, bounded)."""
    line = reader.readline(MAX_LINE_BYTES + 1)
    if len(line) <= MAX_LINE_BYTES or line.endswith(b"\n"):
        return line
    while line and not line.endswith(b"\n"):
        line = reader.readline(MAX_LINE_BYTES + 1)
    return None


def serve(reader, writer, tools):
    """Serve binary streams (sys.stdin.buffer / sys.stdout.buffer) until EOF; returns the Session."""
    session = Session(tools)
    while True:
        line = _read_message(reader)
        if line == b"":
            return session
        reply = (_error(None, INVALID_REQUEST, "message too large") if line is None
                 else session.handle_line(line))
        if reply is not None:
            writer.write(encode(reply))
            writer.flush()


def real_tools(root, store):
    """The one real wiring: BRAINFS1 path policy + TALKINBOX1 validator, imported lazily."""
    from jarvis import brain_fs, talk_inbox

    files = brain_fs.BrainFiles(root)

    def checked(result, show):
        return Refused(result.code) if isinstance(result, brain_fs.Refusal) else show(result)

    def more(flag):
        return "\n[truncated]" if flag else ""

    def read_file(path):
        # shortcut: one brain_fs window (its MAX_LINES), add line paging when the brain needs it
        return checked(files.read_file(path), lambda r: r.text + more(r.truncated))

    def list_dir(path):
        return checked(files.list_dir(path), lambda r: "\n".join(
            e.name + ("/" if e.kind == "dir" else "") for e in r.entries) + more(r.truncated))

    def search(query, path):
        return checked(files.search(query, path), lambda r: "\n".join(
            "%s:%d: %s" % (m.path, m.line, m.text) for m in r.matches) + more(r.stopped))

    def validate(data):
        verdict = talk_inbox.validate_record(data)
        if isinstance(verdict, talk_inbox.InboxRecord):
            return True
        return Refused(getattr(verdict, "reason", "refused"))

    return Tools(read_file, list_dir, search, validate, store)
