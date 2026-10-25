"""Pure evidence receipts; hashes detect changes, not the identity of the issuer.

The seven plan fields form the payload. SHA-256 is envelope metadata, never
part of its own hash. JSON uses sorted keys, compact separators, UTF-8 and
no non-finite numbers. Verification is integrity checking, not acceptance.
"""

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
