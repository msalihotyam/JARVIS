"""GitHub Phase 2 - THE process seam, and the only one in this build.

Phase 1 shipped the offline foundation: the destination grammar, the closed
capability and resource vocabularies, the Core-owned identity binding, the
canonical request frame and the closed argument vectors as pure data. It
shipped no way to run anything, on purpose. This module is the deferred
half - the single place a child process is started - and it is deliberately
small enough to read in one sitting.

WHAT IT WILL RUN. Exactly one executable, ``gh``, found on PATH, with an
argument vector that is RE-DERIVED from a validated typed resource by
``jarvis.github`` and re-proved here before the process is started. There is
no subcommand parameter, no argv parameter, no shell, no string command line
and no way for a caller to add, reorder or override an argument. A plan that
does not equal its own derivation never runs.

WHAT IT NEVER TOUCHES. The credential. JARVIS does not read, request, store,
pass, log or display a GitHub token: authentication is the CLI's own
already-established session, backed by the OS credential store, and the only
thing this module learns about it is the NON-SECRET login it belongs to and
the NAME of the storage backing it. The child environment has every
``GH_``/``GITHUB_``/``GIT_`` variable stripped before it is handed over, so a
token sitting in the parent environment cannot even reach the child - which
is also what makes the observed route provably the credential store rather
than an environment override.

WHICH ACCOUNT AN OBSERVATION IS ABOUT. The one the CREDENTIAL belongs to,
established by GitHub rather than by this machine. ``gh auth status`` reports
the account named in the CLI's own configuration and does not reconcile it
with the token it then uses: a configuration naming one login while the
credential store answers with another's token passes it, and the storage word
is all that is worth taking from it. So the login is taken from ``gh api
user`` - the authenticated endpoint, answered by the token actually in use -
and the two must agree before either is reported. Neither call can be pointed
at a different account by a caller: both vectors are constants.

WHICH ACCOUNT A READ RAN AS IS NOT DECIDED HERE, AND NOT BY OBSERVATION.
The CLI selects its credential when each process starts, so an identity and a
result taken from two processes are two selections: an account that moves away
and back between them is invisible to any number of before/after checks. Every
read therefore runs as ONE ``gh api graphql`` request that selects
``viewer{login}`` beside the data, and ``jarvis.github`` refuses any response
whose viewer is not the principal Core bound. One request is one credential
selection, so that login describes the credential that served those exact
bytes. The observation below is Core's session gate and nothing more - it is
not, and must not be presented as, the proof.

WHAT THAT COST, MEASURED AGAINST gh 2.98 ON 2026-09-10:

- No environment variable selects a stored account. ``GH_TOKEN`` would mean
  handling the raw token, which this build refuses.
- ``GH_CONFIG_DIR`` does not pin one either: a configuration naming an account
  with no per-user keyring entry still authenticates, from the active-account
  slot that ``gh auth switch`` rewrites. The pin is cosmetic, and that slot
  exists on a normal installation, so it cannot be excluded.
- No REST response header names the caller. ``X-OAuth-Client-Id`` names the
  OAuth application, which is identical for every account.
- So GraphQL is the only transport that can carry the proof, and it does not
  carry everything: GitHub's GraphQL ``SearchType`` names ISSUE, REPOSITORY,
  USER and DISCUSSION and no ``CODE``. ``github_code_search`` is therefore
  deferred past usable-v1 rather than run over a second, unprovable route.

WHAT IT DECIDES. Nothing. It observes an identity and returns bytes; Core
compares that identity against its own binding, Core owns the destination
grant, and Core owns the approval. A refusal here is always a refusal - it
never falls back, retries, re-authenticates, prompts, opens a browser or
widens anything.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from shutil import which
from typing import Any, Mapping

from jarvis.github import (
    GH_ARGUMENT_MAX_CHARS,
    GH_MAX_ARGUMENTS,
    GITHUB_CREDENTIAL_ROUTE,
    GITHUB_HOST,
    GITHUB_HOSTNAME_ARGS,
    GitHubExecutionUnavailable,
    GitHubOperationPlan,
    github_operation_plan_refusal,
    github_principal_refusal,
    sanitized_github_environment,
)

#: The one executable name this build will ever start. Resolved through PATH
#: by ``shutil.which`` - never a caller-supplied path, and never a shell.
GH_EXECUTABLE_NAME = "gh"

#: Wall-clock ceiling on one child process. A read that cannot answer inside
#: it is a refusal, not a longer wait: an unbounded call would hold Core's
#: execution path open on a network the operator cannot see.
GH_TIMEOUT_SECONDS = 30

#: Ceiling on captured child output, applied to the BYTES before any decode,
#: so an unexpectedly huge response is refused rather than loaded.
GH_OUTPUT_MAX_BYTES = 1_048_576

#: The argument vector that observes - and only observes - which account the
#: local CLI session belongs to. It is a constant, not a parameter.
GH_AUTH_STATUS_ARGS: tuple[str, ...] = ("auth", "status", *GITHUB_HOSTNAME_ARGS)

#: The argument vector that asks GITHUB which account the credential in use
#: belongs to. Also a constant: there is no parameter here either, the path is
#: the authenticated-user endpoint, and ``--jq`` reduces the answer to one
#: non-secret field so no other part of the profile is ever captured.
# ponytail: one network round trip, on Core's session gate only - the read
# proves its own principal, so nothing observes around it any more. Cache this
# only if that one gate ever measurably matters; a cached identity is the stale
# answer the call exists to replace.
GH_API_USER_ARGS: tuple[str, ...] = (
    "api",
    *GITHUB_HOSTNAME_ARGS,
    "user",
    "--jq",
    ".login",
)

#: ``gh auth status`` names the account and, in parentheses, the storage
#: backing its token. The login grammar is GitHub's own; the storage word is
#: what tells a keyring-backed session apart from a config-file or
#: environment one.
_LOGIN_PATTERN = re.compile(
    r"Logged in to " + re.escape(GITHUB_HOST) + r" account ([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?) \(([a-z_]+)\)"
)

#: The ONE storage word that is the credential route Core knows. Anything
#: else - a token in a config file, a token from the environment, a helper
#: this build does not recognize - is refused rather than mapped onto it.
_KEYRING_STORAGE = "keyring"


class GitHubCommandFailed(GitHubExecutionUnavailable):
    """A GitHub read did not happen. Never a partial or assumed result."""


@dataclass(frozen=True, slots=True)
class GitHubCommandOutput:
    """What one completed child process produced. Data, with no authority."""

    stdout: str
    truncated: bool


def gh_executable(*, path_lookup: Any = which) -> str:
    """The resolved ``gh`` path, or fail closed.

    Resolved fresh for every call rather than cached: a cached path could
    outlive the file it names, and a stale absolute path is exactly the kind
    of thing that is worth resolving twice instead of guessing once.
    """
    resolved = path_lookup(GH_EXECUTABLE_NAME)
    if not resolved or type(resolved) is not str:
        raise GitHubCommandFailed(
            "the GitHub CLI is not available on this machine"
        )
    return resolved


def _child_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The sanitized environment the child gets, through Phase 1's policy.

    ``HOME``/``APPDATA``/``USERPROFILE`` deliberately SURVIVE: they are how
    the CLI finds its own configuration and the OS credential store, which is
    the authentication route this build requires. What does not survive is
    everything that could redirect the call - the whole ``GH_``/``GITHUB_``/
    ``GIT_`` families (tokens, hosts, config paths included), the proxy
    variables and the XDG relocations.
    """
    return sanitized_github_environment(
        os.environ if environ is None else environ
    )


def _neutral_directory() -> str:
    """A working directory with no repository in it.

    The CLI infers a repository from the current directory's git remote when
    one is not stated. Every vector this build derives states ``--repo`` or a
    literal API path, so the inference is already unreachable - and running
    outside any repository means it cannot be reached by a future argument
    either.
    """
    return tempfile.gettempdir()


def _run(
    arguments: tuple[str, ...],
    *,
    environ: Mapping[str, str] | None = None,
    path_lookup: Any = which,
    runner: Any = subprocess.run,
    timeout: int = GH_TIMEOUT_SECONDS,
) -> GitHubCommandOutput:
    """Start ``gh`` once with exactly these arguments, or fail closed.

    ``shell=False`` and a real argument LIST: there is no command string
    anywhere in this module, so quoting, globbing and interpolation are not
    failure modes it can have. Stdin is closed with empty bytes so the child
    can never block on a prompt, and the prompt is disabled by environment as
    well.

    Output is captured as bytes, bounded, then decoded with replacement - a
    response that is not valid UTF-8 is a response, not an exception. Nothing
    from ``stderr`` is returned or re-raised: a diagnostic can carry paths,
    environment values and URLs, so the refusal states the shape of the
    failure and the exit status only.
    """
    if type(arguments) is not tuple or not arguments:
        raise GitHubCommandFailed("no argument vector was derived")
    if len(arguments) > GH_MAX_ARGUMENTS:
        raise GitHubCommandFailed("the argument list is over the bound")
    for argument in arguments:
        if type(argument) is not str or len(argument) > GH_ARGUMENT_MAX_CHARS:
            raise GitHubCommandFailed("an argument is not bounded text")
    executable = gh_executable(path_lookup=path_lookup)
    try:
        completed = runner(
            [executable, *arguments],
            input=b"",
            capture_output=True,
            shell=False,
            env=_child_environment(environ),
            cwd=_neutral_directory(),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubCommandFailed(
            "the GitHub CLI did not answer inside its time bound"
        ) from exc
    except OSError as exc:
        raise GitHubCommandFailed("the GitHub CLI could not be started") from exc
    if completed.returncode != 0:
        raise GitHubCommandFailed(
            f"the GitHub CLI refused the request (exit {completed.returncode})"
        )
    raw = completed.stdout or b""
    if type(raw) is str:  # a runner configured for text; bound it the same way
        raw = raw.encode("utf-8", errors="replace")
    truncated = len(raw) > GH_OUTPUT_MAX_BYTES
    return GitHubCommandOutput(
        stdout=raw[:GH_OUTPUT_MAX_BYTES].decode("utf-8", errors="replace"),
        truncated=truncated,
    )


def observed_github_identity(
    *,
    environ: Mapping[str, str] | None = None,
    path_lookup: Any = which,
    runner: Any = subprocess.run,
) -> tuple[str, str]:
    """The ``(principal, credential_route)`` the local session belongs to.

    AN OBSERVATION, NEVER A DECISION. Core compares this against its own
    binding; nothing here can influence what it is compared against, and a
    connector cannot select the identity it proves.

    Fail closed on every uncertainty: no session, an unreadable status, no
    recognizable account line, MORE than one account line (which of them a
    later call would use is exactly the ambiguity that must not be guessed),
    or a token whose storage is not the OS credential store. The token itself
    is never parsed, returned or logged - only the login and the storage word.

    TWO SOURCES, AND THEY MUST AGREE. ``gh auth status`` is asked only what
    this machine's configuration SAYS - the account name and the storage word
    behind it - and it is not evidence of whose credential would be used: it
    reports the configured name whether or not the token the CLI then reaches
    for belongs to that account. So the login is taken from ``gh api user``,
    which GitHub answers using the credential itself, and a disagreement
    between the two is a refusal rather than a preference for one of them.
    """
    try:
        output = _run(
            GH_AUTH_STATUS_ARGS,
            environ=environ,
            path_lookup=path_lookup,
            runner=runner,
        )
    except GitHubCommandFailed as exc:
        raise GitHubCommandFailed(
            "the GitHub CLI reports no usable authenticated session"
        ) from exc
    matches = _LOGIN_PATTERN.findall(output.stdout)
    if len(matches) != 1:
        raise GitHubCommandFailed(
            "the authenticated GitHub account could not be established "
            "unambiguously"
        )
    configured, storage = matches[0]
    if storage != _KEYRING_STORAGE:
        raise GitHubCommandFailed(
            "the GitHub session is not backed by the OS credential store"
        )
    try:
        answered = _run(
            GH_API_USER_ARGS,
            environ=environ,
            path_lookup=path_lookup,
            runner=runner,
        )
    except GitHubCommandFailed as exc:
        raise GitHubCommandFailed(
            "the GitHub credential in use did not identify its own account"
        ) from exc
    principal = answered.stdout.strip()
    if github_principal_refusal(principal) is not None:
        raise GitHubCommandFailed(
            "the GitHub credential in use did not identify its own account"
        )
    if principal.lower() != configured.lower():
        raise GitHubCommandFailed(
            "the configured GitHub account is not the account the credential "
            "belongs to"
        )
    return (principal, GITHUB_CREDENTIAL_ROUTE)


def run_operation_plan(
    plan: Any,
    *,
    environ: Mapping[str, str] | None = None,
    path_lookup: Any = which,
    runner: Any = subprocess.run,
) -> GitHubCommandOutput:
    """Execute ONE re-proved read plan, or fail closed.

    The plan is checked by RE-DERIVATION first, exactly as Phase 1 specified:
    its capability must be a closed read capability, its resource must bind to
    that capability and its destination, and its arguments must equal - element
    for element - what the closed builder produces for that resource. A
    hand-built plan carrying an arbitrary subcommand, an injected flag, a
    second repository or a token-shaped argument is refused by what it SAYS,
    not by where it came from.

    Passing that check is still not authorization. The destination grant and
    the identity comparison are Core's, they have already happened by the time
    anything reaches here, and this function neither consults nor re-decides
    them.
    """
    reason = github_operation_plan_refusal(plan)
    if reason is not None:
        raise GitHubCommandFailed(reason)
    if plan.host != GITHUB_HOST:  # pragma: no cover - the refusal just proved it
        raise GitHubCommandFailed("the plan names a host this build does not talk to")
    return _run(
        plan.arguments, environ=environ, path_lookup=path_lookup, runner=runner
    )


__all__ = [
    "GH_API_USER_ARGS",
    "GH_AUTH_STATUS_ARGS",
    "GH_EXECUTABLE_NAME",
    "GH_OUTPUT_MAX_BYTES",
    "GH_TIMEOUT_SECONDS",
    "GitHubCommandFailed",
    "GitHubCommandOutput",
    "GitHubOperationPlan",
    "gh_executable",
    "observed_github_identity",
    "run_operation_plan",
]
