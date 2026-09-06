"""Pure deterministic condition predicates (Condition Watch kernel).

One question, answered from arguments alone:

    validated observation + typed predicate -> TRUE / FALSE / UNKNOWN

Nothing else. No scheduler, no job, no persistence, no action, no
notification, no Core call. This module does not read a file, open a socket,
start a process, look at a clock or consult global settings; it cannot, and
the tests assert that from the module's own syntax tree rather than from this
paragraph.

NON-AUTHORITY INVARIANT. This is the load-bearing property, so it is stated
before the API. Evaluating a predicate here establishes NOTHING about
authority. It does not decide whether the source task was authoritative,
whether that task completed, whether a connector is trusted, whether a
destination was approved, whether a classification is valid, whether approval
exists, whether billing permits execution, or whether any action may run.
A ``FileObservation`` is a CLAIM that some caller assembled - constructing one
is not a finding that it was authoritative, and this module never treats it as
one. Every one of those questions belongs to Core and must be re-proved there,
separately, on the trusted path. A ``TRUE`` from this module is an input to
that proof, never a substitute for it.

THREE-VALUED, AND UNKNOWN IS NOT FALSE. A predicate answers ``FALSE`` only
when a valid whole-file observation is CONCLUSIVELY equal to - or conclusively
different from - the expected digest. Every other situation is ``UNKNOWN``: no
observation, a read that went missing, failed or was refused, a region read,
or an observation of a different file. Fabricating ``FALSE`` out of ignorance
is the failure this whole design exists to prevent, because downstream a
``FALSE`` from ``LOCAL_READ_SHA256_DIFFERS`` reads as "the file is unchanged"
- a positive claim that no failed read can support.
``ConditionResult.__bool__`` raises for the same reason: ``if evaluate(...):``
would silently fold ``UNKNOWN`` into a branch, so it is refused outright
rather than answered.

WHOLE-FILE ONLY. Only a whole-file observation can establish these
conditions. A region read has a perfectly valid digest - of the region it
captured - and comparing it against a whole-file expectation would answer a
question nobody asked. Scope is therefore part of the type, and a REGION
observation yields ``UNKNOWN`` rather than being silently compared.

The vocabulary is closed: two kinds, and no way to add a third from data.
There is no expression language, no comparator framework, no regex dialect,
no callback, no shell-out, and no path by which model-generated text becomes
a predicate - a predicate is a typed object or it does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConditionError(ValueError):
    """A malformed predicate, or an input this kernel refuses to interpret.

    Raised at CONSTRUCTION, so a malformed condition or observation cannot
    exist to be asked a question later, and by ``evaluate`` for an argument of
    the wrong type. This is deliberately distinct from ``UNKNOWN``: a wrong
    TYPE is a caller defect and raises, while a well-formed observation that
    is merely not truth-bearing (missing, failed, refused, region, or about
    another file) is a legitimate STATE and answers ``UNKNOWN``.
    """


class ConditionKind(Enum):
    """The closed set of approved condition kinds. There is no other."""

    LOCAL_READ_SHA256_EQUALS = "local_read_sha256_equals"
    LOCAL_READ_SHA256_DIFFERS = "local_read_sha256_differs"


class ConditionResult(Enum):
    """Three-valued, and the three values never collapse into two."""

    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"

    def __bool__(self) -> bool:
        """Refuse truthiness; ``UNKNOWN`` has no honest boolean reading.

        Without this, ``if evaluate(...):`` would quietly treat ``UNKNOWN`` as
        a fired condition - and ``FALSE`` as one too, since every enum member
        is truthy by default - which is precisely the conflation the
        architecture forbids. Callers compare against a member explicitly, so
        the third case cannot be forgotten.
        """
        raise ConditionError(
            "a condition result is three-valued; compare it to a "
            "ConditionResult member explicitly rather than coercing it"
        )


class ObservationStatus(Enum):
    """What happened to the read that produced - or failed to produce - a digest.

    ``OK`` is the only status that carries a digest at all; see
    ``FileObservation``. The rest are the ways an observation can exist while
    establishing nothing, and every one of them answers ``UNKNOWN``.
    """

    OK = "ok"
    MISSING = "missing"
    FAILED = "failed"
    REFUSED = "refused"
    UNKNOWN = "unknown"


class ObservationScope(Enum):
    """Whether the digest describes the whole file or only a region of it."""

    WHOLE_FILE = "whole_file"
    REGION = "region"


#: The canonical digest form, stated once. Exactly 64 lowercase hex
#: characters: no uppercase variant, no ``sha256:`` prefix, no surrounding or
#: embedded whitespace, and no bytes/str ambiguity - ``bytes`` is refused
#: rather than decoded, because choosing an encoding for it would be a guess.
_DIGEST_LENGTH = 64
_DIGEST_ALPHABET = "0123456789abcdef"


def _digest(value: object, field: str) -> str:
    """The value as a canonical sha256 hex digest, or ``ConditionError``.

    ``type() is str`` and not ``isinstance``, so a ``str`` subclass carrying
    its own ``__eq__`` cannot reach the comparison in ``evaluate`` and decide
    the answer for itself.
    """
    if type(value) is not str:
        raise ConditionError(f"{field} is not a string")
    if len(value) != _DIGEST_LENGTH:
        raise ConditionError(f"{field} is not exactly {_DIGEST_LENGTH} characters")
    if any(character not in _DIGEST_ALPHABET for character in value):
        raise ConditionError(
            f"{field} is not lowercase hexadecimal; the canonical form has no "
            "uppercase, no prefix and no whitespace"
        )
    return value


def _reference(value: object) -> str:
    """The source identity the observation and the condition must agree on.

    Opaque here on purpose: this kernel compares references for EQUALITY and
    never interprets, normalizes or resolves one. Normalization would be a
    policy decision about which two names denote the same file, and that
    belongs to whoever owns the read - not to a predicate.
    """
    if type(value) is not str:
        raise ConditionError("the source reference is not a string")
    if not value:
        raise ConditionError("the source reference is empty")
    return value


@dataclass(frozen=True, slots=True)
class FileObservation:
    """One caller-assembled claim about a file's digest at some moment.

    NOT an authority record. Constructing this asserts only that the fields
    are well formed - never that the read happened, that its producer was
    trusted, or that anyone verified provenance. Core owns those questions.

    The status/digest coupling is structural rather than advisory: a status
    other than ``OK`` must carry an EMPTY digest, so a failed or refused read
    cannot arrive holding a stale digest that some later refactor compares by
    accident. No state in this type means "probably fine".
    """

    reference: str
    status: ObservationStatus
    scope: ObservationScope
    sha256: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference", _reference(self.reference))
        if type(self.status) is not ObservationStatus:
            raise ConditionError("status is not an exact ObservationStatus")
        if type(self.scope) is not ObservationScope:
            raise ConditionError("scope is not an exact ObservationScope")
        if self.status is ObservationStatus.OK:
            object.__setattr__(self, "sha256", _digest(self.sha256, "the digest"))
        elif self.sha256 != "":
            raise ConditionError(
                f"a {self.status.value} observation carries a digest; only an "
                "ok observation may"
            )


@dataclass(frozen=True, slots=True)
class LocalReadCondition:
    """A typed predicate over one file's whole-file digest.

    A predicate is THIS OBJECT or it does not exist. There is no string form
    to parse, no dictionary form to load and no expression to compile, so
    model-generated text has no route to becoming one: it would have to be
    hand-translated into an exact ``ConditionKind`` and a canonical digest by
    trusted code, which is where that decision belongs.
    """

    kind: ConditionKind
    reference: str
    expected_sha256: str

    def __post_init__(self) -> None:
        if type(self.kind) is not ConditionKind:
            raise ConditionError("kind is not an exact ConditionKind")
        object.__setattr__(self, "reference", _reference(self.reference))
        object.__setattr__(
            self,
            "expected_sha256",
            _digest(self.expected_sha256, "the expected digest"),
        )


def evaluate(
    condition: LocalReadCondition, observation: FileObservation | None
) -> ConditionResult:
    """Evaluate one typed predicate against one observation, purely.

    ``observation`` may be ``None``, meaning no observation exists yet; that
    is a legitimate state and answers ``UNKNOWN``. Anything else that is not a
    ``FileObservation`` - a raw dictionary from a connector, a task row, a
    model's JSON - is a caller defect and RAISES, so no untyped payload can
    slip in and have its fields read as though they had been validated.

    Every ``UNKNOWN`` below is a refusal to invent a ``FALSE``:

    * no observation, or one that went missing, failed or was refused - the
      file's digest is simply not known;
    * a REGION observation - its digest is real but describes a fragment, and
      a fragment can neither confirm nor deny a whole-file expectation;
    * an observation of a DIFFERENT reference - it is evidence about another
      file, and says nothing at all about this one.
    """
    if type(condition) is not LocalReadCondition:
        raise ConditionError("condition is not an exact LocalReadCondition")
    if observation is None:
        return ConditionResult.UNKNOWN
    if type(observation) is not FileObservation:
        raise ConditionError("observation is not an exact FileObservation")
    if observation.status is not ObservationStatus.OK:
        return ConditionResult.UNKNOWN
    if observation.scope is not ObservationScope.WHOLE_FILE:
        return ConditionResult.UNKNOWN
    if observation.reference != condition.reference:
        return ConditionResult.UNKNOWN
    equal = observation.sha256 == condition.expected_sha256
    if condition.kind is ConditionKind.LOCAL_READ_SHA256_EQUALS:
        return ConditionResult.TRUE if equal else ConditionResult.FALSE
    if condition.kind is ConditionKind.LOCAL_READ_SHA256_DIFFERS:
        return ConditionResult.FALSE if equal else ConditionResult.TRUE
    raise ConditionError("unknown condition kind")


__all__ = [
    "ConditionError",
    "ConditionKind",
    "ConditionResult",
    "FileObservation",
    "LocalReadCondition",
    "ObservationScope",
    "ObservationStatus",
    "evaluate",
]
