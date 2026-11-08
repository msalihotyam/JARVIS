"""Pure evidence receipts; hashes detect changes, not the identity of the issuer.

The seven plan fields form the payload. SHA-256 is envelope metadata, never
part of its own hash. JSON uses sorted keys, compact separators, UTF-8 and
no non-finite numbers. Verification is integrity checking, not acceptance.
"""

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
import hmac
import json


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def _json_value(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        _canonical(value)
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError("base_result must contain only JSON values")


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


@dataclass(frozen=True)
class EvidenceReceipt:
    """A receipt created by the caller from Core check results.

    ``base_result`` accepts JSON data so regression summaries need no app
    dependency. ``sha256`` is populated on creation and retained on edits;
    callers must verify receipts before consuming them.
    """

    plan_hash: str
    diff_sha: str
    check_argv: tuple[str, ...]
    exit_code: int
    output_sha: str
    base_result: object
    verdict: str
    sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.check_argv) not in (tuple, list):
            raise ValueError("check_argv must be an argv sequence")
        object.__setattr__(self, "check_argv", tuple(self.check_argv))
        self._validate()
        # Copy JSON input so mutations of the caller's summary cannot alter us.
        object.__setattr__(self, "base_result", json.loads(_canonical(self.base_result)))
        if self.sha256 is None:
            object.__setattr__(self, "sha256", self._digest())

    def _validate(self) -> None:
        for value in (self.plan_hash, self.diff_sha, self.output_sha, self.verdict):
            if type(value) is not str or not value:
                raise ValueError("receipt strings must be nonempty")
        if not self.check_argv or any(type(arg) is not str for arg in self.check_argv):
            raise ValueError("check_argv must contain strings")
        if type(self.exit_code) is not int:
            raise ValueError("exit_code must be an integer")
        _json_value(self.base_result)

    def canonical_json(self) -> str:
        """Return the exact seven-field payload covered by the digest."""
        self._validate()
        return _canonical({
            "plan_hash": self.plan_hash, "diff_sha": self.diff_sha,
            "check_argv": list(self.check_argv), "exit_code": self.exit_code,
            "output_sha": self.output_sha, "base_result": self.base_result,
            "verdict": self.verdict,
        })

    def _digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def verify(self) -> bool:
        """Fail closed on malformed data or a changed field/digest."""
        try:
            return (type(self.sha256) is str and len(self.sha256) == 64
                    and hmac.compare_digest(self.sha256, self._digest()))
        except (ValueError, TypeError, RecursionError, OverflowError):
            return False

    def to_json(self) -> str:
        return _canonical({"receipt": json.loads(self.canonical_json()),
                           "sha256": self.sha256})

    @classmethod
    def from_json(cls, serialized: str) -> "EvidenceReceipt":
        """Read a strict envelope and reject invalid or tampered receipts."""
        envelope = json.loads(serialized, object_pairs_hook=_unique_object)
        if type(envelope) is not dict or set(envelope) != {"receipt", "sha256"}:
            raise ValueError("invalid receipt envelope")
        payload = envelope["receipt"]
        fields = {"plan_hash", "diff_sha", "check_argv", "exit_code",
                  "output_sha", "base_result", "verdict"}
        if type(payload) is not dict or set(payload) != fields:
            raise ValueError("invalid receipt fields")
        if type(envelope["sha256"]) is not str:
            raise ValueError("missing receipt digest")
        receipt = cls(**payload, sha256=envelope["sha256"])
        if not receipt.verify():
            raise ValueError("receipt verification failed")
        return receipt


# --- N6-10 (B2+, M2, M4): trusted invocation records and the receipt verdict.
# The runner signs a record for each check run with a key the worker never
# holds (HMAC, not a bare digest a worker could recompute). Only signed
# records count; report text has no input here at all.

PASS, FAIL, NOT_RUN, PARTIAL, INVALID = "PASS", "FAIL", "NOT_RUN", "PARTIAL", "INVALID"
_RECORD_FIELDS = frozenset({"check_id", "argv", "commit_sha", "exit_code"})


def _is_sha(value: object) -> bool:
    return (type(value) is str and len(value) in (40, 64)
            and all(c in "0123456789abcdef" for c in value))


def _key(key: object) -> bytes:
    if type(key) is not bytes or not key:
        raise ValueError("record key must be nonempty bytes")
    return key


@dataclass(frozen=True)
class InvocationRecord:
    """What the runner saw for one check run: id, argv, commit, exit code.

    ``exit_code`` is ``None`` when the run timed out (never PASS).
    """

    check_id: str
    argv: tuple[str, ...]
    commit_sha: str
    exit_code: int | None

    def __post_init__(self) -> None:
        if type(self.argv) not in (tuple, list):
            raise ValueError("argv must be an argv sequence")
        object.__setattr__(self, "argv", tuple(self.argv))
        if type(self.check_id) is not str or not self.check_id:
            raise ValueError("check_id must be nonempty")
        if not self.argv or any(type(arg) is not str for arg in self.argv):
            raise ValueError("argv must contain strings")
        if not _is_sha(self.commit_sha):
            raise ValueError("commit_sha must be lowercase hex")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise ValueError("exit_code must be an integer or None")

    def canonical_json(self) -> str:
        return _canonical({"check_id": self.check_id, "argv": list(self.argv),
                           "commit_sha": self.commit_sha, "exit_code": self.exit_code})

    def tag(self, key: bytes) -> str:
        return hmac.new(_key(key), self.canonical_json().encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def sign(self, key: bytes) -> str:
        """Serialized envelope; only the runner holding ``key`` calls this."""
        return _canonical({"record": json.loads(self.canonical_json()),
                           "hmac": self.tag(key)})

    @classmethod
    def from_signed(cls, serialized: object, key: bytes) -> "InvocationRecord":
        """Strictly parse and authenticate; raise ``ValueError`` otherwise."""
        key = _key(key)
        if type(serialized) is not str:
            raise ValueError("a record is a signed JSON string")
        try:
            envelope = json.loads(serialized, object_pairs_hook=_unique_object)
        except (RecursionError, json.JSONDecodeError) as exc:
            raise ValueError("record is not JSON") from exc
        if type(envelope) is not dict or set(envelope) != {"record", "hmac"}:
            raise ValueError("invalid record envelope")
        payload, tag = envelope["record"], envelope["hmac"]
        if (type(payload) is not dict or set(payload) != _RECORD_FIELDS
                or type(tag) is not str or not tag.isascii()):
            raise ValueError("invalid record fields")
        record = cls(**payload)
        if not hmac.compare_digest(tag, record.tag(key)):
            raise ValueError("record tag does not verify")
        return record


@dataclass(frozen=True)
class ReceiptVerdict:
    #: PASS only when every required check is PASS and the commit is whole.
    verdict: str
    #: Every required check id -> PASS | FAIL | NOT_RUN | PARTIAL | INVALID.
    checks: dict[str, str]
    reasons: tuple[str, ...]


_RANK = {PASS: 0, NOT_RUN: 1, FAIL: 2, PARTIAL: 3, INVALID: 4}


def _claimed_check(raw: object) -> object:
    """The check id an unverified record claims, or None; never raises."""
    try:
        record = json.loads(raw)["record"]["check_id"] if type(raw) is str else None
    except Exception:  # anything unparseable claims no check
        return None
    return record if type(record) is str else None


def receipt_verdict(required: object, records: object, final_sha: object, *,
                    key: bytes, changed_files: Iterable[str],
                    commit_files: Iterable[str]) -> ReceiptVerdict:
    """Judge a slice from runner-signed records only.

    ``required`` maps check id -> argv. Raises ``ValueError`` for an empty or
    malformed check list, a bad ``final_sha`` or key; anything wrong with a
    record is a non-PASS verdict, never an exception.
    """
    key = _key(key)
    if not _is_sha(final_sha):
        raise ValueError("final_sha must be lowercase hex")
    if type(required) is not dict or not required:
        raise ValueError("refused: no checks to judge")
    for check_id, argv in required.items():
        if (type(check_id) is not str or not check_id or type(argv) not in (tuple, list)
                or not argv or any(type(a) is not str for a in argv)):
            raise ValueError("refused: malformed check list")
    checks = dict.fromkeys(required, NOT_RUN)
    reasons: list[str] = []
    seen: set[str] = set()
    malformed = False
    for raw in records if isinstance(records, (list, tuple)) else [records]:
        try:
            record = InvocationRecord.from_signed(raw, key)
        except (ValueError, TypeError) as exc:
            # N6-HARD2 H3: only the check the bad record names turns INVALID; the others
            # keep their own state (a missing one stays NOT_RUN); the verdict is INVALID.
            reasons.append(f"malformed record: {exc}"[:120])
            malformed = True
            cid = _claimed_check(raw)
            if cid in checks:
                checks[cid] = INVALID
            continue
        cid = record.check_id
        if cid not in checks:
            continue
        if cid in seen:
            reasons.append(f"{cid}: more than one record")
            checks[cid] = INVALID
            continue
        seen.add(cid)
        if checks[cid] == INVALID:
            continue
        if record.argv != tuple(required[cid]):
            checks[cid], why = INVALID, "record is for another command"
        elif record.commit_sha != final_sha:
            checks[cid], why = PARTIAL, "record is for another commit"
        elif record.exit_code != 0:
            checks[cid], why = FAIL, f"exit code {record.exit_code}"
        else:
            checks[cid], why = PASS, ""
        if why:
            reasons.append(f"{cid}: {why}")
    reasons += [f"{cid}: no record" for cid, s in checks.items() if s == NOT_RUN]
    verdict = INVALID if malformed else max(checks.values(), key=_RANK.__getitem__)
    try:
        if isinstance(changed_files, str) or isinstance(commit_files, str):
            raise TypeError
        outside = sorted(set(changed_files) - set(commit_files))
    except TypeError:
        outside, verdict = [], INVALID
        reasons.append("changed/commit file sets are malformed")
    if outside:
        reasons.append(f"files outside the commit: {outside[:5]}")
        if _RANK[verdict] < _RANK[PARTIAL]:
            verdict = PARTIAL
    return ReceiptVerdict(verdict, checks, tuple(reasons))
