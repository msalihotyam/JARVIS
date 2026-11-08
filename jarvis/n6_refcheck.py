# pyright: strict
"""N6-15 (B1/M1, M3): integration ref verify before any acceptance.

``read_evidence`` reads the candidate ref, then the INT report, then the ref again; a
change or a torn (partial / non-hex) sha raises. The report's single ``RUN_ID:`` line
must equal the run that produced it, and its single PICKS block is the cleanup
evidence: every pick's new commit must carry the source commit's patch-id.
``accept`` takes only sealed ``read_evidence`` output for the same run_id, then re-reads
the ref right before acceptance and refuses if it moved.

Git is an injected port (``GitPort``); this module runs no process and opens no file.
Port results are typed ``object`` because they are untrusted: anything malformed raises.
"""

from __future__ import annotations

import dataclasses
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

# Copied verbatim from scripts/orchbot.py (PICK_TOK_RE). The PICKS block itself is a closed
# line grammar (parse_picks), a superset of orchbot's PICKS_RE: every line up to the next
# top-level KEY: header is Unicode-blank or a pick, so no blank-looking line can end it.
# Every report is _lf()-normalized once before any parsing.
PICK_TOK_RE = re.compile(r"\b[A-Z][A-Z0-9_-]{1,63}\b")
_HEADER_RE = re.compile(r"[A-Z][A-Z _-]*:")  # .match on the raw line: column 0, ASCII only
_PICKS_LINE_RE = re.compile(r"PICKS:[ \t]*")  # fullmatch: the only header shape accepted

_SHA_RE = re.compile(r"[0-9a-f]{40}")
# Every hex-looking run of 4+ chars (git's shortest abbreviation) not glued to a word; a
# pick line must hold exactly two and both must be full lowercase 40-hex shas. Any
# non-ASCII neighbour (letter, digit, mark, zero-width, NBSP of any script) is glue too.
_GLUE = r"0-9A-Za-z_\-\u0080-\U0010ffff"
_HEX_RUN_RE = re.compile(rf"(?<![{_GLUE}])[0-9A-Fa-f]{{4,}}(?![{_GLUE}])")
_WORD_SPLIT_RE = re.compile(r"[ \t()]+")  # lane labels are whole words: bare or in parens
_SEAL = object()  # held only by read_evidence: a caller-built Evidence cannot carry it
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_OTHER_EOL_RE = re.compile("[\x0b\x0c\x1c-\x1e\x85  ]")
_REF_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,254}")


class RefCheckError(ValueError):
    """Any refusal: the evidence is not accepted."""


class GitPort(Protocol):
    def rev_parse(self, rev: str) -> object: ...  # the full commit sha ``rev`` names

    def patch_id(self, sha: str) -> object: ...  # ``git patch-id --stable`` of that commit


@dataclass(frozen=True)
class Pick:
    source: str
    new: str
    lane: str


@dataclass(frozen=True)
class Evidence:
    run_id: str
    ref: str
    sha: str
    picks: tuple[Pick, ...]
    # Set only by read_evidence, bound to the exact field objects; replace() resets it.
    seal: tuple[object, ...] = dataclasses.field(default=(), init=False, repr=False, compare=False)


def _seal_of(ev: Evidence) -> tuple[object, ...]:
    return (_SEAL, ev.run_id, ev.ref, ev.sha, ev.picks)


def _full_sha(value: object, what: str) -> str:
    if type(value) is not str or not _SHA_RE.fullmatch(value):  # exact str: a subclass could fake __eq__
        raise RefCheckError(f"torn or malformed sha for {what}: {str(value)[:80]!r}")
    return value


def _lf(report: str) -> str:
    """The input boundary: CRLF and lone CR become LF (as universal-newline reads do), so a
    CRLF blank line cannot end the PICKS block early and hide the picks after it. Any other
    separator str.splitlines() honours (\\v \\f \\x1c-\\x1e \\x85 U+2028 U+2029) is refused: at a
    line start it would end the block and hide an indented-looking pick."""
    if _OTHER_EOL_RE.search(report):
        raise RefCheckError("report holds a line separator other than CR/LF")
    return report.replace("\r\n", "\n").replace("\r", "\n")


def _blank(c: str) -> bool:
    """Unicode whitespace or an invisible format char (NBSP, U+3000, BOM, ZWSP, ZWNJ, ...)."""
    return c.isspace() or unicodedata.category(c) in ("Zs", "Zl", "Zp", "Cf")


def _unblank(line: str) -> str:
    """``line`` without its leading Unicode-blank chars; "" for a blank line."""
    return next((line[i:] for i, c in enumerate(line) if not _blank(c)), "")


def _head(line: str, key: str) -> bool:
    """Does ``line`` start with ``key`` in any case behind any Unicode-blank prefix? Matching
    too much (ligatures, dotless i) only adds refusals."""
    return _unblank(line).upper().startswith(key)


def _check_run_id(report: str, run_id: str) -> None:
    # A second RUN_ID (any case, any suffix) behind any indent, NBSP, BOM or zero-width prefix counts.
    lines = [ln for ln in report.split("\n") if _head(ln, "RUN_ID")]
    if len(lines) != 1:
        raise RefCheckError(f"report needs exactly one RUN_ID line, found {len(lines)}")
    m = re.fullmatch(r"RUN_ID:[ \t]*(\S+)[ \t]*", lines[0])
    if m is None or not _RUN_ID_RE.fullmatch(m.group(1)):
        raise RefCheckError(f"malformed RUN_ID line: {lines[0][:80]!r}")
    if m.group(1) != run_id:
        raise RefCheckError(f"run_id mismatch: report {m.group(1)[:80]!r} != run {run_id!r}")


def _parse_pick_line(line: str) -> Pick:
    """The real INT shapes: two full shas (source, then candidate) with one ``->`` between
    them and exactly one lane label anywhere (bare or in parens); lowercase words allowed."""

    def bad(why: str) -> RefCheckError:
        return RefCheckError(f"malformed pick line ({why}): {line[:80]!r}")

    runs = list(_HEX_RUN_RE.finditer(line))
    glued = len(re.findall(r"[0-9A-Fa-f]{40,}", line))  # a third sha glued to a word still counts
    if glued != 2 or len(runs) != 2 or not all(_SHA_RE.fullmatch(m.group()) for m in runs):
        raise bad("needs exactly two full 40-hex shas, no abbreviation")
    src, new = runs
    arrow = line.find("->")
    if line.count("->") != 1 or "<-" in line or not src.end() <= arrow < new.start():
        raise bad("needs one -> from source to candidate")
    rest = f"{line[: src.start()]} {line[src.end() : new.start()]} {line[new.end() :]}"
    words = [w for w in _WORD_SPLIT_RE.split(rest) if w]
    lanes = [w for w in words if PICK_TOK_RE.fullmatch(w) and w[-1].isalnum()]  # N6-14_ is suffixed
    # Any other word with an upper/titlecase char (any script) is a glued, partial or
    # suffixed label (N6-14fix, xN6-14, N6-, N6-14,): refused, never trimmed to a lane.
    if any(c.isupper() or c.istitle() for w in words if w not in lanes for c in w):
        raise bad("lane label not a whole token")
    if len(lanes) != 1:
        raise bad(f"needs exactly one lane label, found {len(lanes)}")
    return Pick(src.group(), new.group(), lanes[0])


def parse_picks(report: str) -> tuple[Pick, ...]:
    """A whitelist: exactly one line starts with PICKS (any case, after any Unicode-blank
    prefix) and it is the bare header ``PICKS:`` plus spaces/tabs; every line up to the next
    top-level ``KEY:`` header (or the end) is Unicode-blank or one valid pick. Any other
    shape raises: refusal falls back to the orchestrator."""
    report_ok: object = report
    if type(report_ok) is not str:
        raise RefCheckError("report is not text")
    lines = _lf(report).split("\n")  # idempotent: read_evidence already normalized it
    heads = [i for i, ln in enumerate(lines) if _head(ln, "PICKS")]
    if len(heads) != 1:
        raise RefCheckError(f"report needs exactly one PICKS block, found {len(heads)}")
    head = heads[0]
    if _unblank(lines[head]) != lines[head]:
        raise RefCheckError(f"PICKS header not at line start: {lines[head][:80]!r}")
    if not _PICKS_LINE_RE.fullmatch(lines[head]):  # whitelist: the header holds nothing else
        raise RefCheckError(f"malformed PICKS header: {lines[head][:80]!r}")
    block: list[str] = []
    for line in lines[head + 1 :]:
        if _HEADER_RE.match(line):
            break
        block.append(line)
    picks = [_parse_pick_line(line) for line in block if _unblank(line)]
    if not picks:
        raise RefCheckError("empty PICKS block")
    for field in ("source", "new", "lane"):
        values = [getattr(p, field) for p in picks]
        if len(set(values)) != len(values):
            raise RefCheckError(f"duplicate pick {field}")
    return tuple(picks)


def _resolve(git: GitPort, abbrev: str) -> str:
    sha = _full_sha(git.rev_parse(abbrev), abbrev)
    if not sha.startswith(abbrev):
        raise RefCheckError(f"{abbrev} resolved to a different commit {sha}")
    return sha


def _patch_id(git: GitPort, sha: str) -> str:
    return _full_sha(git.patch_id(sha), f"patch-id of {sha}")


def verify_picks(git: GitPort, picks: tuple[Pick, ...]) -> None:
    """M3 cleanup evidence: each pick's new commit has its source commit's patch-id."""
    seen: set[str] = set()
    for p in picks:
        src_sha, new_sha = _resolve(git, p.source), _resolve(git, p.new)
        if src_sha == new_sha or src_sha in seen or new_sha in seen:
            raise RefCheckError(f"pick {p.lane} {p.source}->{p.new}: commit reused")
        seen.update((src_sha, new_sha))
        src = _patch_id(git, src_sha)
        new = _patch_id(git, new_sha)
        if src != new:
            raise RefCheckError(f"pick {p.lane} {p.source}->{p.new}: patch-id differs")


def read_evidence(git: GitPort, ref: str, read_report: Callable[[], object], run_id: str) -> Evidence:
    """Read ref, report, ref; refuse a torn read, a foreign run_id or bad PICKS."""
    ref_ok: object = ref  # callers are untrusted too: a non-str must raise RefCheckError
    run_ok: object = run_id
    if type(ref_ok) is not str or not _REF_RE.fullmatch(ref_ok) or ".." in ref_ok:
        raise RefCheckError(f"malformed ref: {str(ref_ok)[:80]!r}")
    if type(run_ok) is not str or not _RUN_ID_RE.fullmatch(run_ok):
        raise RefCheckError(f"malformed run_id: {str(run_ok)[:80]!r}")
    before = _full_sha(git.rev_parse(ref), ref)
    report = read_report()
    after = _full_sha(git.rev_parse(ref), ref)
    if before != after:
        raise RefCheckError(f"torn read: {ref} moved {before} -> {after} during the evidence read")
    if type(report) is not str or not report:
        raise RefCheckError("torn or empty report read")
    report = _lf(report)
    _check_run_id(report, run_id)
    picks = parse_picks(report)
    verify_picks(git, picks)
    ev = Evidence(run_id, ref, before, picks)
    object.__setattr__(ev, "seal", _seal_of(ev))
    return ev


def accept(git: GitPort, evidence: Evidence, run_id: str) -> str:
    """Compare-before-accept: only sealed read_evidence output for this run_id, and the
    ref still names the sha the evidence was read at."""
    ev_ok: object = evidence
    run_ok: object = run_id  # exact str: a subclass could fake __eq__/__ne__
    if type(ev_ok) is not Evidence:
        raise RefCheckError("evidence not produced by read_evidence")
    seal, want = evidence.seal, _seal_of(evidence)
    if len(seal) != len(want) or not all(a is b for a, b in zip(seal, want)):
        raise RefCheckError("evidence not produced by read_evidence")
    if type(run_ok) is not str or evidence.run_id != run_id:
        raise RefCheckError(f"run_id mismatch: evidence {evidence.run_id!r} != run {str(run_ok)[:80]!r}")
    now = _full_sha(git.rev_parse(evidence.ref), evidence.ref)
    if now != evidence.sha:
        raise RefCheckError(f"{evidence.ref} moved {evidence.sha} -> {now} between read and accept")
    return now
