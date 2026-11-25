"""BRAINRUN1: brain session command builder. Fake runner only; no process, network or model."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis import brain_session as bs
from jarvis.connectors.claude_code import PROHIBITED_BILLING_ENV_VARS
from tests.adversarial import HOSTILE_NUMBERS, HOSTILE_PATH_PARTS, HOSTILE_TEXT, each

TOKEN = "tok-SENTINEL-4242"
PACKET = "PACKET-SENTINEL-brain-question"
REPLY = json.dumps({"type": "result", "is_error": False, "result": "ok", "session_id": "sess-1"})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()  # resolved: TEMP may arrive as an 8.3 short name
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.environ = {"SYSTEMROOT": "C:\\Windows", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}

    def config(self, **over):
        base = dict(command=str(self.tmp / "claude.exe"), root=str(self.root), model="opus",
                    tool_server_command=str(self.tmp / "python.exe"), tool_server_args=("-m", "brain_tools"))
        base.update(over)
        return bs.BrainConfig(**base)

    def built(self, **over):
        command = bs.build(self.config(**over), self.environ)
        self.assertIs(type(command), bs.BrainCommand, command)
        return command

    def refused(self, result, code=None):
        self.assertIs(type(result), bs.Refused, result)
        self.assertTrue(result.code.isascii() and result.code, result)
        if code:
            self.assertEqual(result.code, code)

    @staticmethod
    def flag(argv, name):
        return argv[argv.index(name) + 1]


class AcceptanceTest(Base):
    def test_build_returns_argv_env_config_and_root_cwd(self):
        command = self.built()
        self.assertEqual(command.cwd, self.root)
        self.assertEqual(command.argv[0], str(self.tmp / "claude.exe"))
        self.assertEqual(self.flag(command.argv, "--mcp-config"), command.mcp_config)
        self.assertIsInstance(command.env, dict)

    def test_no_builtin_tools_only_our_four(self):
        argv = self.built().argv
        self.assertEqual(self.flag(argv, "--tools"), "")
        allowed = self.flag(argv, "--allowedTools").split(",")
        self.assertEqual(allowed, [f"mcp__jarvis_brain__{n}" for n in ("read_file", "list_dir", "search", "write_proposal")])
        self.assertEqual(argv.count("--allowedTools"), 1)
        self.assertEqual(self.flag(argv, "--permission-mode"), "dontAsk")
        for bad in ("--dangerously-skip-permissions", "--add-dir", "--disallowedTools", "--settings"):
            self.assertNotIn(bad, argv)

    def test_one_strict_tool_server_no_settings(self):
        command = self.built()
        servers = json.loads(command.mcp_config)["mcpServers"]
        self.assertEqual(list(servers), ["jarvis_brain"])
        self.assertEqual(servers["jarvis_brain"], {"command": str(self.tmp / "python.exe"), "args": ["-m", "brain_tools"]})
        for flag in ("--strict-mcp-config", "--safe-mode", "--disable-slash-commands"):
            self.assertEqual(command.argv.count(flag), 1, flag)
        self.assertEqual(command.argv.count("--mcp-config"), 1)
        self.assertEqual(command.env.get("ENABLE_CLAUDEAI_MCP_SERVERS"), "false")

    def test_env_is_allowlist_and_billing_vars_refused_even_empty(self):
        self.environ["SOME_RANDOM_VAR"] = "x"
        self.assertNotIn("SOME_RANDOM_VAR", self.built().env)
        for name in PROHIBITED_BILLING_ENV_VARS:
            for variant in (name, name.lower()):
                with self.subTest(variant=variant):
                    self.refused(bs.build(self.config(), {**self.environ, variant: ""}), "billing_env_present")

    def test_model_comes_from_config(self):
        for model in ("opus", "claude-sonnet-5-5"):
            self.assertEqual(self.flag(self.built(model=model).argv, "--model"), model)
        self.assertEqual(self.flag(self.built(effort="high").argv, "--effort"), "high")
        source = Path(bs.__file__).read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"claude-(opus|sonnet|haiku|fable)|\"(opus|sonnet|haiku)\"", source))
        for bad in ("-p", "", "a b", None, "--model"):
            self.refused(bs.build(self.config(model=bad), self.environ), "model_invalid")

    def test_resume_reuses_same_limits(self):
        fresh = self.built()
        again = bs.resume(self.config(), "abc-123_X", self.environ)
        self.assertIs(type(again), bs.BrainCommand, again)
        i = again.argv.index("--resume")
        self.assertEqual(again.argv[i + 1], "abc-123_X")
        self.assertEqual(again.argv[:i] + again.argv[i + 2:], fresh.argv)
        self.assertNotIn("--no-session-persistence", fresh.argv)  # the first session must be resumable
        self.assertEqual((again.env, again.mcp_config, again.cwd), (fresh.env, fresh.mcp_config, fresh.cwd))
        self.assertIs(type(bs.resume(self.config(), "x" * 64, self.environ)), bs.BrainCommand)


class RefusalTest(Base):
    def test_asks_for_builtin_tools_servers_dirs_or_bypass_refused(self):
        cases = {"builtin_tools": ("Bash",), "extra_tool_servers": ("other",), "add_dirs": ("C:\\",),
                 "extra_flags": ("--dangerously-skip-permissions",)}
        for field, value in cases.items():
            with self.subTest(field=field):
                self.refused(bs.build(self.config(**{field: value}), self.environ))
        self.refused(bs.build(self.config(extra_flags=("--permission-mode", "bypassPermissions")), self.environ))
        self.refused(bs.build("not a config", self.environ), "config_invalid")

    def test_root_must_be_existing_plain_directory_outside_data_and_jarvis(self):
        (self.tmp / "file.txt").write_text("x")
        for sub in ("data", "x/DATA/sub", "home/.jarvis", "home/.Jarvis/inner"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        roots = [self.tmp / "missing", self.tmp / "file.txt", self.tmp / "data", self.tmp / "x" / "DATA" / "sub",
                 self.tmp / "home" / ".jarvis", self.tmp / "home" / ".Jarvis" / "inner", "repo", 5, None,
                 str(self.root) + "\\.", str(self.root) + "\\", str(self.root) + "\\..\\repo",
                 "\\\\localhost\\C$\\dev", "\\\\host\\share\\repo", "//host/share", "\\\\?\\" + str(self.root)]
        each(self, roots, lambda root: self.refused(bs.build(self.config(root=root), self.environ)))
        each(self, HOSTILE_PATH_PARTS,
             lambda part: self.refused(bs.build(self.config(root=str(self.root) + "\\" + part), self.environ)))

    def test_session_id_must_be_opaque(self):
        bad = ["a/b", "a\\b", "a b", "x" * 65, "", "-p", "--resume", "abc\n", "a.b", "..", 7, None, *HOSTILE_TEXT]
        each(self, bad, lambda sid: self.refused(bs.resume(self.config(), sid, self.environ), "session_id_invalid"))

    def test_hostile_numbers_refused(self):
        for field in ("max_turns", "max_budget_usd", "timeout_seconds", "max_output_bytes"):
            each(self, (*HOSTILE_NUMBERS, 0, -1), lambda v: self.refused(bs.build(self.config(**{field: v}), self.environ)))
        self.refused(bs.build(self.config(max_budget_usd=0.00001), self.environ), "max_budget_invalid")  # would print 0.0000

    def test_real_repo_shape_root_accepted(self):
        # A repo holds hard-linked files (uv .venv) and nested dirs: the root itself is all that is checked.
        (self.root / "a.txt").write_text("x")
        os.link(self.root / "a.txt", self.root / "b.txt")
        (self.root / "sub" / "data").mkdir(parents=True)
        self.assertEqual(self.built().cwd, self.root)

    def test_proxies_and_other_credentials_not_passed(self):
        foreign = ["HTTP_PROXY", "https_proxy", "ALL_PROXY", "no_proxy", "NPM_CONFIG_PROXY", "GITHUB_TOKEN",
                   "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "CLAUDE_CODE_USE_FOUNDRY", "NODE_OPTIONS",
                   "CLAUDE_ENV_FILE", "GH_TOKEN"]
        self.environ.update({name: "http://u:p@proxy:1" for name in foreign})
        env = {key.upper() for key in self.built().env}
        for name in foreign:
            self.assertNotIn(name.upper(), env)

    def test_nothing_in_app_imports_module(self):
        package = Path(bs.__file__).parent
        users = [p for p in package.rglob("*.py") if p.name != "brain_session.py" and "brain_session" in p.read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(users, [])


class RunTest(Base):
    def fake(self, returncode=0, stdout=REPLY, raises=None):
        calls = []

        def runner(argv, *, cwd, env, input, timeout):
            calls.append(SimpleNamespace(argv=argv, cwd=cwd, env=env, input=input, timeout=timeout))
            if raises:
                raise raises
            return SimpleNamespace(returncode=returncode, stdout=stdout)
        return runner, calls

    def test_packet_by_stdin_never_argv_and_no_token_in_argv(self):
        runner, calls = self.fake()
        command = self.built()
        self.assertEqual(bs.run(command, PACKET, runner), bs.BrainReply("ok", "sess-1"))
        call = calls[0]
        self.assertEqual(json.loads(call.input)["message"]["content"], PACKET)
        self.assertEqual(call.env.get("CLAUDE_CODE_OAUTH_TOKEN"), TOKEN)
        for part in call.argv:
            self.assertNotIn(PACKET, part)
            self.assertNotIn(TOKEN, part)
        self.assertEqual((call.cwd, call.timeout), (str(self.root), command.timeout_seconds))

    def test_runner_failures_are_typed_and_carry_no_reply(self):
        command = self.built(max_output_bytes=200)
        cases = [(self.fake(returncode=1), "exit_nonzero"),
                 (self.fake(returncode=None), "exit_nonzero"),
                 (self.fake(raises=subprocess.TimeoutExpired("claude", 1)), "timeout"),
                 (self.fake(raises=RuntimeError("boom")), "runner_error"),
                 (self.fake(stdout=REPLY + "\n" + "x" * 300), "output_too_large"),
                 (self.fake(stdout=b"bytes"), "output_invalid"),
                 (self.fake(stdout="no json"), "reply_malformed"),
                 (self.fake(stdout=REPLY.replace("false", "true")), "reply_error"),
                 (self.fake(stdout=REPLY.replace("sess-1", "../x")), "reply_session_invalid")]
        for (runner, _), code in cases:
            with self.subTest(code=code):
                self.assertEqual(bs.run(command, PACKET, runner), bs.Failure(code))
        odd = json.dumps({"type": "result", "is_error": False, "result": "a b\u0085c", "session_id": "s"}, ensure_ascii=False)
        self.assertEqual(bs.run(command, PACKET, self.fake(stdout=odd)[0]), bs.BrainReply("a b\u0085c", "s"))
        runner, calls = self.fake()
        for packet in ("", None, "x" * (bs.MAX_PACKET_BYTES + 1)):
            self.assertEqual(bs.run(command, packet, runner), bs.Failure("packet_invalid"))
        self.assertEqual(bs.run(bs.Refused("x"), PACKET, runner), bs.Failure("command_invalid"))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
