"""GitHub v1 READ: the closed vocabulary, the process seam and the connector.

NOTHING HERE TOUCHES THE NETWORK OR THE CREDENTIAL STORE. Every test either
exercises a pure grammar or injects a fake runner and a fake identity
observer, so the suite proves the boundary's decisions rather than GitHub's
availability. A test that needed a real session would be testing the
operator's machine.

The three properties under test are the three the phase is about: the request
that reaches the CLI is exactly the one the closed builder derives, the
identity a read executes under is Core's rather than the connector's, and
content that comes back is bound to the request that asked for it.
"""

from __future__ import annotations

import subprocess
import unittest

from jarvis.app import build_envelope, decode_task_json, github_authority
from jarvis.connectors.github_cli import GitHubCliConnector
from jarvis.contracts import (
    EXTERNAL_READ_RESULT_KEY,
    ActionScope,
    AgentRun,
    RunStatus,
    OutcomeStatus,
    SessionState,
    Task,
    VerificationStatus,
)
from jarvis.github import (
    EXTERNAL_READ_MAX_CHARS,
    GITHUB_CONNECTOR,
    GITHUB_CAPABILITY_RESOURCE,
    GITHUB_CREDENTIAL_ROUTE,
    GITHUB_READ_CAPABILITIES,
    GITHUB_READ_OPERATION,
    GITHUB_SEARCH_MAX_ITEMS,
    GITHUB_WRITE_CAPABILITIES,
    GitHubExecutionUnavailable,
    GitHubIdentityBinding,
    GitHubOperationPlan,
    GitHubRequestFrame,
    GitHubResource,
    GitHubResourceKind,
    github_operation_plan,
    github_operation_plan_refusal,
    github_resource_from_dict,
    github_resource_refusal,
)
from jarvis.github_cli import (
    GH_AUTH_STATUS_ARGS,
    GitHubCommandFailed,
    _run,
    observed_github_identity,
    run_operation_plan,
)
from jarvis.settings import Settings

OWNER = "acme"
REPOSITORY = "widgets"
DESTINATION = f"github/{OWNER}/{REPOSITORY}"
PRINCIPAL = "operator"
BINDING = GitHubIdentityBinding(principal=PRINCIPAL)
SHA = "a" * 40


def resource(kind: GitHubResourceKind, **fields) -> GitHubResource:
    return GitHubResource(kind=kind, owner=OWNER, repository=REPOSITORY, **fields)


def frame_for(capability: str, res: GitHubResource) -> GitHubRequestFrame:
    return GitHubRequestFrame(
        connector=GITHUB_CONNECTOR,
        capability=capability,
        destination=DESTINATION,
        resource=res,
        principal=PRINCIPAL,
        credential_route=GITHUB_CREDENTIAL_ROUTE,
        operation=GITHUB_READ_OPERATION,
    )


#: One valid resource per read capability, so every row of the closed table is
#: exercised by identity rather than by a sample of it.
READ_RESOURCES = {
    "github_repository_read": resource(GitHubResourceKind.REPOSITORY),
    "github_file_read": resource(
        GitHubResourceKind.FILE, ref="main", path="docs/README.md"
    ),
    "github_issue_list": resource(
        GitHubResourceKind.ISSUE_LIST, state="open", limit=10
    ),
    "github_issue_read": resource(GitHubResourceKind.ISSUE, number=7),
    "github_pull_request_list": resource(
        GitHubResourceKind.PULL_REQUEST_LIST, state="all", limit=5
    ),
    "github_pull_request_read": resource(GitHubResourceKind.PULL_REQUEST, number=9),
    "github_commit_list": resource(GitHubResourceKind.COMMIT_LIST, limit=5),
    "github_commit_read": resource(GitHubResourceKind.COMMIT, ref=SHA),
    "github_pull_request_files": resource(
        GitHubResourceKind.PULL_REQUEST_FILES, number=9, limit=5
    ),
    "github_code_search": resource(
        GitHubResourceKind.CODE_SEARCH, query="parse_token", limit=3
    ),
}


class FakeOutput:
    def __init__(self, stdout: str, truncated: bool = False) -> None:
        self.stdout = stdout
        self.truncated = truncated


class FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_runner(stdout: bytes = b"{}", returncode: int = 0, stderr: bytes = b""):
    """A ``subprocess.run`` stand-in that records exactly how it was called."""
    calls: list[dict] = []

    def run(argv, **kwargs):
        calls.append({"argv": list(argv), **kwargs})
        return FakeCompleted(returncode, stdout, stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def fake_path_lookup(name: str) -> str:
    return "C:/tools/gh.exe"


# --------------------------------------------------------------------------
# The closed vocabulary
# --------------------------------------------------------------------------


class VocabularyTests(unittest.TestCase):
    def test_every_read_capability_derives_exactly_one_plan(self) -> None:
        self.assertEqual(10, len(GITHUB_READ_CAPABILITIES))
        for capability in GITHUB_READ_CAPABILITIES:
            res = READ_RESOURCES[capability]
            self.assertIsNone(github_resource_refusal(res), capability)
            self.assertIs(GITHUB_CAPABILITY_RESOURCE[capability], res.kind)
            plan = github_operation_plan(frame_for(capability, res))
            self.assertIsNone(github_operation_plan_refusal(plan), capability)
            # Nothing a caller supplied ever becomes a subcommand or a flag.
            self.assertIn(plan.arguments[0], ("api", "repo", "issue", "pr", "search"))

    def test_the_repository_reaches_argv_only_as_the_validated_slug(self) -> None:
        for capability in GITHUB_READ_CAPABILITIES:
            plan = github_operation_plan(
                frame_for(capability, READ_RESOURCES[capability])
            )
            joined = " ".join(plan.arguments)
            self.assertIn(f"{OWNER}/{REPOSITORY}", joined, capability)
            # One repository per vector, never a second one bolted on.
            self.assertEqual(1, joined.count(f"{OWNER}/{REPOSITORY}"), capability)

    def test_no_write_capability_has_an_argument_builder(self) -> None:
        for capability in GITHUB_WRITE_CAPABILITIES:
            res = resource(GITHUB_CAPABILITY_RESOURCE[capability])
            with self.assertRaises(GitHubExecutionUnavailable):
                github_operation_plan(frame_for(capability, res))
            self.assertNotIn(capability, GitHubCliConnector.manifest.capabilities)

    def test_a_forged_plan_is_refused_by_re_derivation(self) -> None:
        good = github_operation_plan(
            frame_for("github_repository_read", READ_RESOURCES["github_repository_read"])
        )
        forgeries = (
            good.arguments + ("--jq", ".token"),
            ("repo", "delete", f"{OWNER}/{REPOSITORY}"),
            good.arguments[:-1],
            ("api", "--hostname", "ghe.internal", "user"),
        )
        for arguments in forgeries:
            forged = GitHubOperationPlan(
                capability=good.capability,
                destination=good.destination,
                resource=good.resource,
                arguments=arguments,
            )
            self.assertIsNotNone(github_operation_plan_refusal(forged), arguments)


class ResourceGrammarTests(unittest.TestCase):
    def test_each_new_kind_requires_exactly_its_own_fields(self) -> None:
        # A field a kind does not use may not be carried, in both directions.
        self.assertIsNotNone(
            github_resource_refusal(resource(GitHubResourceKind.COMMIT_LIST))
        )
        self.assertIsNotNone(
            github_resource_refusal(
                resource(GitHubResourceKind.COMMIT_LIST, limit=5, number=3)
            )
        )
        self.assertIsNotNone(
            github_resource_refusal(resource(GitHubResourceKind.COMMIT, number=3))
        )
        self.assertIsNotNone(
            github_resource_refusal(
                resource(GitHubResourceKind.PULL_REQUEST_FILES, number=3)
            )
        )
        self.assertIsNotNone(
            github_resource_refusal(
                resource(GitHubResourceKind.CODE_SEARCH, query="x")
            )
        )

    def test_the_search_term_is_a_literal_never_a_query_language(self) -> None:
        for query in (
            "repo:other/secret",
            "org:acme password",
            "-oh-no",
            'token "quoted"',
            "a/b",
            "tok*",
            " leading",
            "x" * 200,
        ):
            self.assertIsNotNone(
                github_resource_refusal(
                    resource(GitHubResourceKind.CODE_SEARCH, query=query, limit=3)
                ),
                query,
            )
        for query in ("parse_token", "retry.count", "two words", "kebab-case"):
            self.assertIsNone(
                github_resource_refusal(
                    resource(GitHubResourceKind.CODE_SEARCH, query=query, limit=3)
                ),
                query,
            )

    def test_a_search_is_bounded_more_tightly_than_a_listing(self) -> None:
        self.assertIsNone(
            github_resource_refusal(
                resource(
                    GitHubResourceKind.CODE_SEARCH,
                    query="term",
                    limit=GITHUB_SEARCH_MAX_ITEMS,
                )
            )
        )
        self.assertIsNotNone(
            github_resource_refusal(
                resource(
                    GitHubResourceKind.CODE_SEARCH,
                    query="term",
                    limit=GITHUB_SEARCH_MAX_ITEMS + 1,
                )
            )
        )
        # The listing bound is unchanged and is not the search bound.
        self.assertIsNone(
            github_resource_refusal(
                resource(GitHubResourceKind.ISSUE_LIST, state="open", limit=50)
            )
        )

    def test_a_serialized_resource_rebuilds_or_fails_closed(self) -> None:
        original = READ_RESOURCES["github_file_read"]
        self.assertEqual(original, github_resource_from_dict(original.to_dict()))
        for broken in (
            {"kind": "file"},
            dict(original.to_dict(), unexpected=1),
            dict(original.to_dict(), kind="nonexistent"),
            dict(original.to_dict(), number="7"),
            dict(original.to_dict(), path="../../etc/passwd"),
        ):
            with self.assertRaises(GitHubExecutionUnavailable):
                github_resource_from_dict(broken)


# --------------------------------------------------------------------------
# The process seam
# --------------------------------------------------------------------------


class ProcessSeamTests(unittest.TestCase):
    def test_the_child_is_started_with_a_list_no_shell_and_no_stdin(self) -> None:
        runner = fake_runner(b'{"ok":true}')
        _run(("repo", "view"), path_lookup=fake_path_lookup, runner=runner, environ={})
        call = runner.calls[0]
        self.assertEqual(["C:/tools/gh.exe", "repo", "view"], call["argv"])
        self.assertFalse(call["shell"])
        self.assertEqual(b"", call["input"])
        self.assertTrue(call["capture_output"])
        self.assertFalse(call["check"])
        self.assertGreater(call["timeout"], 0)

    def test_the_child_environment_carries_no_token_or_redirection(self) -> None:
        runner = fake_runner()
        hostile = {
            "GH_TOKEN": "ghp_secret",
            "GITHUB_TOKEN": "ghp_secret",
            "GH_HOST": "ghe.internal",
            "GIT_CONFIG": "/tmp/evil",
            "HTTPS_PROXY": "http://mitm",
            "XDG_CONFIG_HOME": "/tmp/conf",
            "APPDATA": "C:/Users/o/AppData/Roaming",
            "PATH": "C:/tools",
        }
        _run(
            ("repo", "view"),
            path_lookup=fake_path_lookup,
            runner=runner,
            environ=hostile,
        )
        child = runner.calls[0]["env"]
        for removed in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_HOST",
            "GIT_CONFIG",
            "HTTPS_PROXY",
            "XDG_CONFIG_HOME",
        ):
            self.assertNotIn(removed, child, removed)
        # What the credential store needs to be findable SURVIVES.
        self.assertEqual("C:/Users/o/AppData/Roaming", child["APPDATA"])
        self.assertEqual("1", child["GH_PROMPT_DISABLED"])
        # And the child runs outside any repository on this machine.
        self.assertNotIn(".git", runner.calls[0]["cwd"])

    def test_a_failing_call_refuses_without_echoing_its_diagnostics(self) -> None:
        runner = fake_runner(b"", returncode=1, stderr=b"token ghp_leak is invalid")
        with self.assertRaises(GitHubCommandFailed) as caught:
            _run(("repo", "view"), path_lookup=fake_path_lookup, runner=runner, environ={})
        self.assertNotIn("ghp_leak", str(caught.exception))
        self.assertIn("exit 1", str(caught.exception))

    def test_a_timeout_is_a_refusal(self) -> None:
        def timing_out(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

        with self.assertRaises(GitHubCommandFailed):
            _run(
                ("repo", "view"),
                path_lookup=fake_path_lookup,
                runner=timing_out,
                environ={},
            )

    def test_a_missing_cli_is_a_refusal(self) -> None:
        with self.assertRaises(GitHubCommandFailed):
            _run(
                ("repo", "view"),
                path_lookup=lambda name: None,
                runner=fake_runner(),
                environ={},
            )

    def test_a_forged_plan_never_reaches_the_process(self) -> None:
        runner = fake_runner()
        forged = GitHubOperationPlan(
            capability="github_repository_read",
            destination=DESTINATION,
            resource=READ_RESOURCES["github_repository_read"],
            arguments=("repo", "delete", f"{OWNER}/{REPOSITORY}"),
        )
        with self.assertRaises(GitHubCommandFailed):
            run_operation_plan(
                forged, path_lookup=fake_path_lookup, runner=runner, environ={}
            )
        self.assertEqual([], runner.calls)


class IdentityObservationTests(unittest.TestCase):
    def observe(self, stdout: str):
        return observed_github_identity(
            path_lookup=fake_path_lookup,
            runner=fake_runner(stdout.encode()),
            environ={},
        )

    def test_a_keyring_session_reports_its_login_and_route(self) -> None:
        observed = self.observe(
            "github.com\n"
            "  x Logged in to github.com account operator (keyring)\n"
            "  - Token: gho_************\n"
        )
        self.assertEqual((PRINCIPAL, GITHUB_CREDENTIAL_ROUTE), observed)

    def test_the_status_call_is_a_constant_naming_the_one_host(self) -> None:
        runner = fake_runner(
            b"  Logged in to github.com account operator (keyring)\n"
        )
        observed_github_identity(
            path_lookup=fake_path_lookup, runner=runner, environ={}
        )
        self.assertEqual(
            ["C:/tools/gh.exe", *GH_AUTH_STATUS_ARGS], runner.calls[0]["argv"]
        )
        self.assertIn("--hostname", GH_AUTH_STATUS_ARGS)

    def test_every_uncertain_session_fails_closed(self) -> None:
        for stdout in (
            "",
            "You are not logged into any GitHub hosts.\n",
            # Not the OS credential store: a config-file or environment token.
            "  Logged in to github.com account operator (oauth_token)\n",
            # Two accounts: which one a later call would use is exactly the
            # ambiguity that must not be guessed.
            "  Logged in to github.com account operator (keyring)\n"
            "  Logged in to github.com account other (keyring)\n",
        ):
            with self.assertRaises(GitHubCommandFailed, msg=stdout):
                self.observe(stdout)

    def test_a_failing_status_call_is_an_unusable_session(self) -> None:
        with self.assertRaises(GitHubCommandFailed):
            observed_github_identity(
                path_lookup=fake_path_lookup,
                runner=fake_runner(b"", returncode=1),
                environ={},
            )


# --------------------------------------------------------------------------
# The connector
# --------------------------------------------------------------------------


def github_task(capability: str = "github_file_read", **overrides) -> Task:
    res = READ_RESOURCES[capability]
    task = Task(
        objective="Read the documentation",
        capability=capability,
        scope=ActionScope(action="read", destination=DESTINATION),
        github=res.to_dict(),
    )
    for name, value in overrides.items():
        setattr(task, name, value)
    return task


class ConnectorTests(unittest.TestCase):
    def connector(
        self, *, observed=(PRINCIPAL, GITHUB_CREDENTIAL_ROUTE), stdout="{}"
    ) -> GitHubCliConnector:
        self.ran: list[GitHubOperationPlan] = []

        def runner(plan, **kwargs):
            self.ran.append(plan)
            return FakeOutput(stdout)

        def observer():
            if isinstance(observed, Exception):
                raise observed
            return observed

        return GitHubCliConnector(
            BINDING, identity_observer=observer, plan_runner=runner
        )

    def test_the_manifest_is_read_only(self) -> None:
        self.assertEqual(
            set(GITHUB_READ_CAPABILITIES),
            set(GitHubCliConnector.manifest.capabilities),
        )
        self.assertTrue(
            GitHubCliConnector.manifest.default_side_effect.always_requires_approval
        )

    def test_the_attested_bytes_are_the_request_frame(self) -> None:
        connector = self.connector()
        task = github_task()
        expected = frame_for("github_file_read", READ_RESOURCES["github_file_read"])
        self.assertEqual(expected.canonical_bytes(), connector.canonical_outbound(task))
        # One character of the path changes what an approval binds.
        moved = github_task()
        moved.github = dict(moved.github, path="docs/OTHER.md")
        self.assertNotEqual(
            connector.canonical_outbound(task), connector.canonical_outbound(moved)
        )

    def test_a_read_returns_a_snapshot_bound_to_its_request(self) -> None:
        connector = self.connector(stdout='{"name":"widgets"}')
        task = github_task()
        result = connector.execute(task, SessionState(id="s-1"))
        self.assertIs(OutcomeStatus.SUCCESS, result.outcome)
        snapshot = result.data[EXTERNAL_READ_RESULT_KEY]
        self.assertEqual(DESTINATION, snapshot["destination"])
        self.assertEqual("file", snapshot["resource_kind"])
        self.assertEqual(task.id, snapshot["source_task_id"])
        self.assertEqual('{"name":"widgets"}', snapshot["content"])
        self.assertFalse(snapshot["truncated"])
        # The unbilled route, reported exactly as the estimate classified it.
        self.assertIsNone(result.usage.monetary_cost_usd)

    def test_oversized_content_is_truncated_rather_than_refused(self) -> None:
        connector = self.connector(stdout="x" * (EXTERNAL_READ_MAX_CHARS + 500))
        result = connector.execute(github_task(), SessionState(id="s-1"))
        snapshot = result.data[EXTERNAL_READ_RESULT_KEY]
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(EXTERNAL_READ_MAX_CHARS, len(snapshot["content"]))

    def test_a_changed_account_refuses_before_the_call(self) -> None:
        connector = self.connector(observed=("someone-else", GITHUB_CREDENTIAL_ROUTE))
        result = connector.execute(github_task(), SessionState(id="s-1"))
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertFalse(result.error.retryable)
        self.assertEqual([], self.ran)

    def test_an_unobservable_identity_refuses_before_the_call(self) -> None:
        connector = self.connector(observed=GitHubCommandFailed("no session"))
        result = connector.execute(github_task(), SessionState(id="s-1"))
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertEqual([], self.ran)

    def test_a_task_whose_resource_is_unusable_never_reaches_the_cli(self) -> None:
        connector = self.connector()
        task = github_task()
        task.github = dict(task.github, path="../secrets.env")
        result = connector.execute(task, SessionState(id="s-1"))
        self.assertIs(OutcomeStatus.ERROR, result.outcome)
        self.assertEqual([], self.ran)

    def test_verification_reads_the_bound_task_state(self) -> None:
        connector = self.connector(stdout='{"name":"widgets"}')
        task = github_task()
        result = connector.execute(task, SessionState(id="s-1"))
        # Nothing bound yet: AgentCore binds the snapshot, so VERIFY fails
        # closed on a task that never received one.
        self.assertIs(
            VerificationStatus.FAILED,
            connector.verify(task, result, SessionState(id="s-1")).status,
        )
        task.external_read = result.data[EXTERNAL_READ_RESULT_KEY]
        self.assertIs(
            VerificationStatus.PASSED,
            connector.verify(task, result, SessionState(id="s-1")).status,
        )
        # A tampered digest is not smoothed over.
        task.external_read = dict(task.external_read, sha256="b" * 64)
        self.assertIs(
            VerificationStatus.FAILED,
            connector.verify(task, result, SessionState(id="s-1")).status,
        )


# --------------------------------------------------------------------------
# Configured authority and the task input boundary
# --------------------------------------------------------------------------


class ConfiguredAuthorityTests(unittest.TestCase):
    def test_an_unconfigured_installation_holds_no_github_authority(self) -> None:
        identity, policy = github_authority(Settings())
        self.assertIsNone(identity)
        self.assertEqual(frozenset(), policy)

    def test_configuration_becomes_exact_grants_for_each_repository(self) -> None:
        settings = Settings(
            github_principal=PRINCIPAL,
            github_repositories=(DESTINATION, "github/acme/other"),
        )
        identity, policy = github_authority(settings)
        self.assertEqual(PRINCIPAL, identity.principal)
        self.assertEqual(GITHUB_CREDENTIAL_ROUTE, identity.credential_route)
        self.assertEqual(2 * len(GITHUB_READ_CAPABILITIES), len(policy))
        self.assertTrue(
            all(grant.connector == GITHUB_CONNECTOR for grant in policy)
        )
        # No grant for a repository nobody configured, and none for a write.
        destinations = {grant.destination for grant in policy}
        self.assertNotIn("github/acme/unlisted", destinations)
        capabilities = {grant.capability for grant in policy}
        self.assertEqual(set(), capabilities & set(GITHUB_WRITE_CAPABILITIES))

    def test_unusable_configuration_is_refused_at_construction(self) -> None:
        for broken in (
            {"github_principal": "not a login", "github_repositories": ()},
            {"github_principal": PRINCIPAL, "github_repositories": ("acme/widgets",)},
            {"github_principal": PRINCIPAL, "github_repositories": ("github/acme",)},
            {"github_principal": None, "github_repositories": (DESTINATION,)},
        ):
            with self.assertRaises(ValueError, msg=broken):
                Settings(**broken)


class TaskInputTests(unittest.TestCase):
    def decode(self, **overrides):
        document = {
            "objective": "Read the documentation",
            "capability": "github_file_read",
            "scope": {"action": "read", "destination": DESTINATION},
            "github": {
                "kind": "file",
                "owner": OWNER,
                "repository": REPOSITORY,
                "ref": "main",
                "path": "docs/README.md",
            },
        }
        document.update(overrides)
        import json

        return decode_task_json(json.dumps(document).encode())

    def test_a_valid_section_is_stored_in_canonical_form(self) -> None:
        task = self.decode()
        self.assertEqual(READ_RESOURCES["github_file_read"].to_dict(), task.github)

    def test_the_section_and_the_destination_must_agree(self) -> None:
        from jarvis.app import TaskInputError

        with self.assertRaises(TaskInputError):
            self.decode(scope={"action": "read", "destination": "github/other/repo"})

    def test_a_read_capability_without_a_section_is_refused(self) -> None:
        from jarvis.app import TaskInputError

        document_without = {
            "objective": "Read the documentation",
            "capability": "github_file_read",
            "scope": {"action": "read", "destination": DESTINATION},
        }
        import json

        with self.assertRaises(TaskInputError):
            decode_task_json(json.dumps(document_without).encode())

    def test_a_non_github_capability_may_not_carry_a_section(self) -> None:
        from jarvis.app import TaskInputError

        with self.assertRaises(TaskInputError):
            self.decode(capability="code_analysis")

    def test_no_free_form_locator_is_expressible(self) -> None:
        from jarvis.app import TaskInputError

        for smuggled in (
            {"url": "https://github.com/acme/widgets"},
            {"endpoint": "graphql"},
            {"argv": ["repo", "delete"]},
        ):
            with self.assertRaises(TaskInputError, msg=smuggled):
                self.decode(github={**self.decode().github, **smuggled})


class EnvelopeTests(unittest.TestCase):
    def test_the_external_read_renders_as_untrusted_reference_data(self) -> None:
        connector = GitHubCliConnector(
            BINDING,
            identity_observer=lambda: (PRINCIPAL, GITHUB_CREDENTIAL_ROUTE),
            plan_runner=lambda plan, **kwargs: FakeOutput('{"name":"widgets"}'),
        )
        task = github_task()
        result = connector.execute(task, SessionState(id="s-1"))
        task.external_read = result.data[EXTERNAL_READ_RESULT_KEY]
        payload = build_envelope(
            command="run",
            permission_mode=Settings().permission_mode,
            run=AgentRun(status=RunStatus.COMPLETED, session=SessionState(id="s-1"), task=task),
            session=SessionState(id="s-1"),
        )
        block = payload["external_read"]
        self.assertEqual(DESTINATION, block["destination"])
        self.assertFalse(block["trusted"])
        self.assertEqual(GITHUB_CONNECTOR, block["connector"])
        # A tampered row renders nothing rather than something plausible.
        task.external_read = dict(task.external_read, sha256="b" * 64)
        tampered = build_envelope(
            command="run",
            permission_mode=Settings().permission_mode,
            run=AgentRun(status=RunStatus.COMPLETED, session=SessionState(id="s-1"), task=task),
            session=SessionState(id="s-1"),
        )
        self.assertIsNone(tampered["external_read"])


if __name__ == "__main__":
    unittest.main()
