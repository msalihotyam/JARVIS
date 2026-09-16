"""DEV TOOL - latency truth map of ONE General Chat turn through the REAL production path.

Not product code and never imported by JARVIS. It makes real subscription calls: run it deliberately.

Bridge.submitText (sync) -> Backend.submit_text -> JarvisApp.submit_text -> provider adapter.
Subprocess boundaries are timestamped by wrapping subprocess.run / Popen in the provider modules; the
provider process itself is the real CLI. Uses a TEMP copy of the real config (real grants -> real catalogue).

Usage: latency_bench.py <claude|codex> <model|-> <effort|-> <runs> <prompt>
Env BENCH_NOSTREAM=1 runs the same code without the streaming preview listener (A/B).
Marks: T0 submit, T2 provider spawn, T5 first answer text delta, T5b first complete answer event,
T6 first preview reaching the bridge (first visible text), T7 provider exit, T8 UI final.
"""
import json, os, shutil, statistics, subprocess, sys, tempfile, threading, time
from dataclasses import replace
from pathlib import Path

for k in list(os.environ):
    if k.upper().startswith(("ANTHROPIC", "CLAUDE")):
        del os.environ[k]
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtGui import QGuiApplication
import jarvis.codex as codex_mod
import jarvis.connectors.claude_code as claude_mod
import jarvis.intent as intent_mod
from jarvis.settings import ConfigStore
from jarvis.ui.backend import Backend
from jarvis.ui.bridge import Bridge
from jarvis.ui.preferences import UiPreferences

provider, model, effort, runs, prompt = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]
model = "" if model == "-" else model
effort = "" if effort == "-" else effort
T = {}
EVENTS = []
real_run, real_popen = subprocess.run, subprocess.Popen


def mark(name):
    T.setdefault(name, time.perf_counter())


def timed_run(argv, *a, **k):
    t0 = time.perf_counter()
    try:
        return real_run(argv, *a, **k)
    finally:
        EVENTS.append(("run", os.path.basename(str(argv[0])), " ".join(str(x) for x in argv[1:4]), t0, time.perf_counter()))


class Proc:
    """A real Popen whose communicate() reads stdout line by line so first-content time is observable."""

    def __init__(self, argv, **kw):
        self.argv = list(argv)
        is_model = ("-p" in self.argv and "--output-format" in self.argv) or (len(self.argv) > 1 and self.argv[1] == "exec")
        self.is_model = is_model
        self.t_spawn = time.perf_counter()
        self.p = real_popen(self.argv, **kw)
        if is_model:
            mark("T2_provider_spawn")

    def __getattr__(self, name):
        return getattr(self.p, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self.p.__exit__(*exc)

    def wait(self, timeout=None):
        result = self.p.wait(timeout)
        if self.is_model:
            mark("T7_provider_exit")
        return result

    @property
    def stdout(self):
        return self.p.stdout

    @property
    def stderr(self):
        return self.p.stderr

    def poll(self):
        return self.p.poll()

    def communicate(self, input=None, timeout=None):
        if not self.is_model:
            return self.p.communicate(input=input, timeout=timeout)
        if input is not None:
            self.p.stdin.write(input); self.p.stdin.close()
        out = []
        err = []
        te = threading.Thread(target=lambda: err.append(self.p.stderr.read()), daemon=True); te.start()
        for line in self.p.stdout:
            now = time.perf_counter()
            out.append(line)
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "system" and ev.get("subtype") == "init":
                mark("T3_cli_init")
            if kind == "stream_event":
                d = (ev.get("event") or {}).get("delta") or {}
                if d.get("type") == "text_delta":
                    mark("T5_first_text_delta")
            if kind == "assistant":
                for c in (ev.get("message") or {}).get("content") or []:
                    if c.get("type") == "text":
                        mark("T5b_first_assistant_text_event")
            if kind == "rate_limit_event":
                mark("T4_rate_limit_event")
            if kind == "turn.started":
                mark("T3_cli_init")
            item = ev.get("item") or {}
            if item.get("type") == "agent_message":
                mark("T5b_first_assistant_text_event")
            if "delta" in str(kind):
                mark("T5_first_text_delta")
        self.p.wait()
        te.join(5)
        mark("T7_provider_exit")
        self.returncode = self.p.returncode
        return "".join(out), "".join(err)


for mod in (claude_mod, codex_mod, intent_mod):
    mod.subprocess.run = timed_run
    mod.subprocess.Popen = Proc

# The streaming path: first provider text delta (T5) and first preview reaching the bridge (T6).
_real_delta = intent_mod._text_delta


def _delta(line):
    text = _real_delta(line)
    if text:
        mark("T5_first_text_delta")
    return text


intent_mod._text_delta = _delta
_real_preview = Bridge._on_preview


def _preview(self, request_id, text):
    _real_preview(self, request_id, text)
    if self.streamText:
        mark("T6_first_visible_text")


Bridge._on_preview = _preview
if os.environ.get("BENCH_NOSTREAM") == "1":
    Bridge._preview_relay = lambda self, request_id: None       # A/B: the same code without the streaming listener

app = QGuiApplication([])
results = []
for i in range(runs):
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-bench-"))
    shutil.copy(Path.home() / ".jarvis" / "config.json", tmp / "config.json")
    store = ConfigStore(tmp / "config.json")
    store.mutate(lambda s: replace(s, memory_database=tmp / "j.db", **({"claude_model": model or None, "claude_effort": effort or None} if provider == "claude" else {})))
    prefs = UiPreferences(tmp / "prefs.json")
    if provider == "codex":
        prefs.data["chat"] = {"provider": "codex", "codex_model": model, "codex_effort": effort}
    backend = Backend(store)
    real_app = backend._app

    def timed_app(settings, real_app=real_app):
        t = time.perf_counter(); a = real_app(settings); EVENTS.append(("app_build", "", "", t, time.perf_counter())); return a
    backend._app = timed_app
    bridge = Bridge(backend, prefs, sync=True)
    bridge.refreshConfig()
    T.clear(); EVENTS.clear()
    T["T0_submit"] = time.perf_counter()
    bridge.submitText(prompt)
    T["T8_ui_final"] = time.perf_counter()
    base = T["T0_submit"]
    rel = {k: round(v - base, 3) for k, v in sorted(T.items(), key=lambda kv: kv[1])}
    ev = [(k, n, a, round(s - base, 3), round(e - s, 3)) for k, n, a, s, e in EVENTS]
    usage = bridge.chatUsage
    entry = bridge.feed[-1]
    results.append({"rel": rel, "events": ev, "kind": entry["kind"], "text": entry["text"][:120],
                    "reported": usage.get("reported"), "chars": {k: usage.get(k) for k in ("request_chars", "conversation_chars", "context_chars")}})
    print(json.dumps(results[-1], ensure_ascii=False))

for key in ("T2_provider_spawn", "T3_cli_init", "T5_first_text_delta", "T5b_first_assistant_text_event", "T6_first_visible_text", "T7_provider_exit", "T8_ui_final"):
    vals = [r["rel"][key] for r in results if key in r["rel"]]
    if vals:
        print(f"SUMMARY {provider} {model or 'default'} {key}: median={statistics.median(vals):.2f}s runs={vals}")
