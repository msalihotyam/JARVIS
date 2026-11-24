"""Decision table for the pure worker router."""

import unittest

from jarvis.workers.route import route


def facts(**changes):
    result = dict(tier="T1", **{"class": "B"}, files=["jarvis/example.py"],
                  veto=[], linux_ok=True, new_project=False, worker="auto",
                  usage=80, headroom={"codex": 50, "jules": 5},
                  admitted={"codex": True, "jules": True}, subscription_only=True)
    result.update(changes)
    return result


class TestWorkersRoute(unittest.TestCase):
    def test_decision_table(self):
        rows = [
            ({}, ("codex", "more_headroom")),
            ({"tier": "T0", "class": "C"}, ("codex", "more_headroom")),
            ({"tier": "T2"}, ("claude", "tier")),
            ({"class": "A"}, ("claude", "class")),
            ({"files": []}, ("claude", "no_files")),
            ({"veto": ["JARVIS/example.py"]}, ("claude", "veto")),
            ({"usage": 50}, ("claude", "claude_headroom")),
            ({"usage": 0}, ("claude", "claude_headroom")),
            ({"usage": 100}, ("codex", "more_headroom")),
            ({"worker": "claude"}, ("claude", "explicit")),
            ({"worker": "codex", "usage": 0}, ("codex", "explicit")),
            ({"worker": "codex", "tier": "T2"}, ("claude", "tier")),
            ({"worker": "codex", "class": "A"}, ("claude", "class")),
            ({"worker": "jules"}, ("jules", "explicit")),
            ({"worker": "jules", "linux_ok": False}, ("claude", "linux_required")),
            ({"worker": "jules", "new_project": True}, ("claude", "new_project")),
            ({"new_project": True}, ("codex", "more_headroom")),
            ({"linux_ok": False}, ("codex", "more_headroom")),
            ({"subscription_only": False}, ("claude", "subscription_required")),
            ({"admitted": {"codex": False, "jules": True}},
             ("claude", "admission_refused")),
            ({"worker": "jules", "admitted": {"codex": True, "jules": False}},
             ("claude", "admission_refused")),
            ({"headroom": {"codex": 9, "jules": 5}}, ("claude", "low_headroom")),
            ({"usage": 95, "headroom": {"codex": 10, "jules": 5}},
             ("codex", "more_headroom")),
            ({"worker": "jules", "headroom": {"codex": 50, "jules": 0}},
             ("claude", "low_headroom")),
        ]
        for changes, expected in rows:
            with self.subTest(changes=changes):
                self.assertEqual(route(facts(**changes)), expected)

    def test_protected_path_table(self):
        paths = ["", " ", ".", "..", "../x", "/tmp/x", "C:\\x", "*.py",
                 "a?b", "a[b", "SCRIPTS/x", "scripts./x", "a /x", "a~1/x",
                 ".claude/x", ".github/x", "data/x", "a/AGENTS.md", "CLAUDE.md",
                 "uv.lock", ".env.test", "dev/dashboard/x", "a/.jarvis/x",
                 "a.key", "a.pem", "a.p12", "a.pfx", "a.crt", "a.cer",
                 "a.db", "a.sqlite", "a.sqlite3", "jarvis/../scripts/x"]
        for path in paths:
            for worker in ("auto", "codex", "jules"):
                with self.subTest(path=path, worker=worker):
                    self.assertEqual(route(facts(files=[path], worker=worker)),
                                     ("claude", "veto"))

    def test_invalid_input_table(self):
        for value in (None, [], "UNKNOWN", True, 42):
            with self.subTest(value=value):
                self.assertEqual(route(value), ("claude", "invalid_input"))
        for key in facts():
            missing = facts()
            del missing[key]
            with self.subTest(missing=key):
                self.assertEqual(route(missing), ("claude", "invalid_input"))
        for changes in ({"worker": "paid"}, {"tier": []}, {"class": None},
                        {"linux_ok": 1}, {"new_project": "false"},
                        {"subscription_only": 1}, {"files": "x"}, {"files": [None]},
                        {"veto": None}, {"veto": [1]}, {"admitted": {}},
                        {"admitted": {"codex": 1, "jules": True}}, {"headroom": []}):
            with self.subTest(changes=changes):
                self.assertEqual(route(facts(**changes)), ("claude", "invalid_input"))
        for value in (None, "UNKNOWN", True, -1, float("nan"), float("inf"), 101):
            with self.subTest(usage=value):
                self.assertEqual(route(facts(usage=value)), ("claude", "usage_unknown"))
        for worker in ("codex", "jules"):
            for value in (None, "UNKNOWN", True, -1, float("nan"), float("inf")):
                h = {"codex": 50, "jules": 5}
                h[worker] = value
                with self.subTest(worker=worker, headroom=value):
                    self.assertEqual(route(facts(headroom=h)),
                                     ("claude", "headroom_unknown"))
        self.assertEqual(route(facts(headroom={})), ("claude", "headroom_unknown"))
        self.assertEqual(route(facts(headroom={"codex": 101, "jules": 5})),
                         ("claude", "headroom_unknown"))


# Every path the route veto refused before AUDROUTE1; it must never be narrowed.
_VETOED_BEFORE = (
    "", ".", "..", "a*.py", "a?.py", "a[1].py", "progra~1/x.py", "c:/x.py",
    "x.py:s", "/abs.py", "../up.py", "scripts./x.py", "jarvis /x.py",
    "scripts/x.py", "Scripts\conveyor.py", ".claude/settings.json",
    ".github/workflows/ci.yml", "data/jarvis.db", "data", "CLAUDE.md",
    "docs/AGENTS.md", "uv.lock", "sub/uv.lock", ".env", ".env.local",
    "dev/dashboard/app.py", "home/.jarvis/x", ".jarvis/x", "certs/a.pem",
    "a.key", "a.p12", "a.pfx", "a.crt", "a.cer", "a.db", "a.sqlite",
    "a.sqlite3",
)
_ALLOWED = ("jarvis/example.py", "tests/test_x.py", "docs/notes.md",
            "jarvis/workers/route.py", "build/reports/x.md")


def _conveyor():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "conveyor", Path(__file__).resolve().parents[1] / "scripts" / "conveyor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestRouteVetoParity(unittest.TestCase):
    """AUDROUTE1: route._veto stays in step with conveyor.codex_veto."""

    @classmethod
    def setUpClass(cls):
        cls.conveyor = _conveyor()

    def corpus(self):
        c = self.conveyor
        derived = [d + "x.py" for d in (*c.CODEX_VETO, *c.PROTECTED_DIRS)]
        derived += sorted(c.PROTECTED_FILES)
        return (*_VETOED_BEFORE, *_ALLOWED, *derived, "riot/x.py",
                "games/RiotClient/x.exe", "jarvis/riot.py")

    def test_never_narrowed(self):
        from jarvis.workers.route import _veto
        for path in _VETOED_BEFORE:
            with self.subTest(path=path):
                self.assertTrue(_veto(path))
                self.assertEqual(route(facts(files=[path])), ("claude", "veto"))

    def test_matches_conveyor(self):
        from jarvis.workers.route import _veto
        for path in self.corpus():
            with self.subTest(path=path):
                self.assertEqual(_veto(path), self.conveyor.codex_veto(path))
        for path in _ALLOWED:
            self.assertFalse(_veto(path), path)

    def test_inline_literals_match_conveyor(self):
        """AUDROUTE1FIX: every inline string in conveyor.protected_path/codex_veto
        (suffix, prefix, basename, segment rule) is known here and probed for parity."""
        import ast
        from pathlib import Path
        from jarvis.workers.route import _veto
        tree = ast.parse((Path(self.conveyor.__file__)).read_text(encoding="utf-8"))
        found, names, other = set(), set(), set()
        for f in tree.body:
            if isinstance(f, ast.FunctionDef) and f.name in ("protected_path", "codex_veto"):
                body = f.body[1:] if ast.get_docstring(f) else f.body
                consts = [n.value for s in body for n in ast.walk(s) if isinstance(n, ast.Constant)]
                found |= {v for v in consts if isinstance(v, str)}
                other |= {repr(v) for v in consts if not isinstance(v, str)}
                names |= {n.id for n in ast.walk(f) if isinstance(n, ast.Name)}
                names |= {"." + n.attr for n in ast.walk(f) if isinstance(n, ast.Attribute)}
        # Non-str constants (bytes, slice/count ints) and methods (.isascii, .casefold) pinned too.
        self.assertEqual(other, {"1"})  # rsplit("/", 1)
        # A new constant or helper the rules call (a .kdbx tuple, a regex) fails here too:
        # PROTECTED_DIRS/CODEX_VETO/PROTECTED_FILES are probed by corpus() already.
        self.assertEqual(names, {"PROTECTED_DIRS", "CODEX_VETO", "any", "isinstance", "str",
                                 "posixpath", "protected_path", "base", "p", "q", "s", "c",
                                 ".endswith", ".lower", ".replace", ".rsplit", ".split",
                                 ".startswith", ".normpath", ".strip"})
        # A new conveyor literal fails here: add it to this set and make route._veto match.
        self.assertEqual(found, {
            ".cer", ".crt", ".db", ".env", ".key", ".p12", ".pem", ".pfx", ".sqlite",
            ".sqlite3", "/", "/.jarvis/", "\\", "data/", "dev/dashboard/", "riot",
            "uv.lock", "", " ", "*?[~", ".", "..", "../", ":", "agents.md", "claude.md"})
        # AUDROUTE1FIX2: a known literal reused in a new position (base.startswith(" "))
        # must show too: probe every segment position (alone, leading, trailing, middle,
        # leading with suffix) at depth 0/1/2, as basename and as a directory segment.
        segs = lambda lit: (lit, lit + "x", "x" + lit, "x" + lit + "y", lit + "x.py")  # noqa: E731
        for lit in sorted(found | set("".join(found))):  # char-set rules ("*?[~") per char
            for path in {d + s + t for d in ("", "a/", "a/b/") for s in segs(lit)
                         for t in ("", "/b.py")}:
                for variant in (path, path.upper(), path.replace("/", "\\")):
                    with self.subTest(literal=lit, path=variant):
                        self.assertEqual(_veto(variant), self.conveyor.codex_veto(variant))

    def test_conveyor_rules_unchanged(self):
        """AUDROUTE1FIX2: any edit to conveyor.protected_path/codex_veto bodies (a rule
        that reuses a pinned literal, a reordered strip/lower) fails here. Re-check
        route._veto against the new rule, then update the digest."""
        import ast
        import hashlib
        from pathlib import Path
        tree = ast.parse(Path(self.conveyor.__file__).read_text(encoding="utf-8"))
        digests = {}
        for f in tree.body:
            if isinstance(f, ast.FunctionDef) and f.name in ("protected_path", "codex_veto"):
                if ast.get_docstring(f):
                    f.body = f.body[1:]  # docstring edits do not change the rule
                # shortcut: ast.dump text can shift across Python minors (CI and local are
                # 3.13), re-pin the digest when the CI Python moves.
                digests[f.name] = hashlib.sha256(ast.dump(f).encode()).hexdigest()
                # A later rebinding (wrapper, second def) would run unpinned code.
                self.assertEqual(getattr(self.conveyor, f.name).__code__.co_firstlineno,
                                 f.lineno - len(f.decorator_list), f.name)
        self.assertEqual(digests, {
            "protected_path": "1670b45d134b96ed608d0c6ebee328324af48637eb3b9506714069c024336295",
            "codex_veto": "576ae0cf855cd850638861cbd8cac74a726e464708e054700b9bfbf1fc35b215"})
