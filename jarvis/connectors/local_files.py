"""Bounded, read-only local file connector (M3).

JARVIS reads the bytes itself. No external model is ever granted filesystem
access, so everything a model can later see about a local file is a bounded,
redacted excerpt that JARVIS produced, persisted and attested. That is the
whole reason this connector exists rather than a ``Read`` tool grant on the
Claude Code subprocess: a tool grant would let the model pull bytes that no
approval ever attested, which is exactly the guarantee H1 exists to hold.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from pathlib import Path
from typing import Any, Iterable

from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.contracts import (
    LOCAL_READ_CAPABILITY,
    LOCAL_READ_MAX_BYTES,
    LOCAL_READ_RESULT_KEY,
    AttestationError,
    Availability,
    BillingClass,
    CostEstimate,
    DataClassification,
    ErrorCode,
    LocalReadSnapshot,
    MonetaryState,
    OutcomeStatus,
    RiskLevel,
    SessionState,
    SideEffect,
    SideEffectProfile,
    StructuredError,
    Task,
    ToolResult,
    UsageRecord,
    VerificationRecord,
    VerificationStatus,
    bound_local_text,
    canonical_path,
    local_read_denial_reason,
    local_read_path_refusal,
    local_read_snapshot_refusal,
    utc_now,
)

#: Every filesystem read this connector performs. ``O_BINARY`` matters on
#: Windows only; ``O_NOFOLLOW`` is added per-open where the platform has it.
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0)

#: Whether the platform can open a name RELATIVE TO AN OPEN DIRECTORY HANDLE.
#: Where it can (POSIX), the whole chain below the allowed root is walked
#: handle by handle with ``O_NOFOLLOW``, so no pathname is ever re-traversed
#: after it was validated and nothing outside the root can be opened at all -
#: not even transiently. Windows has no ``dir_fd`` support, so it takes the
#: handle-authoritative route in ``_open_in_root`` instead.
_DIRECTORY_HANDLE_TRAVERSAL = (
    os.open in os.supports_dir_fd
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
)

#: NTFS junctions and symlinks are both reparse points, and a junction needs
#: no privilege to create - so ``S_ISLNK`` alone is not the Windows question.
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)

#: Extended-length path prefixes ``GetFinalPathNameByHandleW`` returns.
_UNC_PREFIX = "\\\\?\\UNC\\"
_EXTENDED_PREFIX = "\\\\?\\"

#: Refusal texts. Each one names the policy rule that fired and the
#: root-relative path at most - never file content, never an absolute path.
_NO_TARGET = "A local read task must name exactly one file target."
_OUTSIDE_ROOTS = "Read target is outside the allowed roots."
_NOT_REGULAR = "Read target is not an existing regular file."
_TARGET_MOVED = (
    "Read target changed between authorization and open; refusing to read a "
    "different file under a stale authorization."
)
_TOO_LARGE = (
    f"Read target exceeds the {LOCAL_READ_MAX_BYTES}-byte local read ceiling; "
    "it is refused rather than silently truncated."
)
_NOT_TEXT = "Read target is not strict UTF-8 text."
#: A hard link is a second NAME for one file, not a link to be resolved, so
#: nothing in the containment chain can see it: ``resolve`` has nothing to
#: dereference, it is not a reparse point, and ``GetFinalPathNameByHandleW``
#: answers with the in-root name it was opened by. An in-root name for a file
#: that also lives outside the roots therefore passes every check while the
#: bytes come from outside. Multi-linked regular files are refused instead.
_MULTIPLY_LINKED = (
    "Read target has more than one filesystem name; a multiply-linked file "
    "cannot be proven to live inside the allowed roots and is refused."
)
#: P1-4/P1-5. The path is a resource IDENTIFIER, so it is refused rather than
#: rewritten: a redacted pseudo-path would make the approved identity and the
#: displayed identity diverge while still asserting a trustworthy read.
_UNREPRESENTABLE_PATH = "Read target path cannot be represented safely:"


def _final_path_from_handle(descriptor: int) -> Path | None:
    """The authoritative path of the OBJECT behind ``descriptor``, or ``None``.

    Windows has no ``dir_fd``, so containment cannot be established by walking
    directory handles; it is established after the fact instead, by asking the
    kernel what the opened object actually is. ``GetFinalPathNameByHandleW``
    answers from the handle, with every reparse point already resolved, so a
    junction or symlink introduced anywhere in the chain between validation
    and open shows up here as a path outside the allowed roots.

    ``None`` means the question could not be answered, which the caller treats
    exactly like an answer outside the roots: no bytes are read either way.
    """
    try:
        import ctypes
        import msvcrt
    except ImportError:  # pragma: no cover - non-Windows platforms
        return None
    try:
        handle = msvcrt.get_osfhandle(descriptor)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        final_path = kernel32.GetFinalPathNameByHandleW
        final_path.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        final_path.restype = ctypes.c_uint32
        # 32767 is the documented maximum extended-length path, so a single
        # call is always enough and a truncated answer is never accepted.
        buffer = ctypes.create_unicode_buffer(32768)
        length = final_path(ctypes.c_void_p(handle), buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            return None
        text = buffer.value
    except Exception:  # noqa: BLE001 - any failure to prove containment
        return None
    # The extended-length prefixes are spelling, not location: strip them so
    # the result compares against the roots like every other resolved path.
    if text.startswith(_UNC_PREFIX):
        text = "\\\\" + text[len(_UNC_PREFIX):]
    elif text.startswith(_EXTENDED_PREFIX):
        text = text[len(_EXTENDED_PREFIX):]
    if not text:
        return None
    return Path(text)


class _TargetMoved(Exception):
    """The opened descriptor is not the file that was authorized."""


class _TargetTooLarge(Exception):
    """The file exceeds the physical read ceiling; never truncated."""


class _MultiplyLinked(Exception):
    """The opened file has more than one name, so containment cannot hold."""


class LocalFileReadConnector(BaseConnector):
    """Read exactly one in-root text file into a bounded typed snapshot."""

    manifest = ConnectorManifest(
        name="local-file-read",
        capabilities=(LOCAL_READ_CAPABILITY,),
        description=(
            "Bounded, redacted read of one local UTF-8 text file inside the "
            "allowed roots."
        ),
        priority=60,
        default_risk=RiskLevel.LOW,
        # LOCAL_READ is deliberately NOT a mandatory-approval effect: this
        # connector is the first real consumer of the existing authority
        # model rather than a special case bolted onto it. STRICT still asks;
        # SESSION and SMART_AUTO auto-approve because the effect is LOW and
        # reversible. ``_LOCAL_EFFECT_LADDER`` still lets a task strengthen
        # the profile, and a conflicting pair still fails closed.
        default_side_effect=SideEffectProfile(
            effect=SideEffect.LOCAL_READ,
            reversible=True,
            always_requires_approval=False,
        ),
        timeout_seconds=30,
        # Explicit, not inherited: the manifest default is UNKNOWN and would
        # be refused before execution. A local read spends nothing.
        default_billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
        requires_paid_api=False,
    )

    def __init__(
        self,
        allowed_roots: Iterable[str | Path] | None = None,
        default_working_directory: str | Path | None = None,
    ) -> None:
        """Bind the authority set, or refuse to exist (P2-6).

        Omitting ``allowed_roots`` entirely is the documented default and
        means "the configured startup/project root". Passing an EXPLICITLY
        empty iterable is not a synonym for that - it says "no roots at all" -
        and quietly turning it into the current working directory would widen
        an empty authority set into a real one, at construction time, before
        any approval or execution could refuse it. An explicitly empty set is
        therefore an invalid configuration and raises here.

        A root is a real ``str``/``Path`` naming a real absolute location.
        ``Path("")`` resolves to the current directory, so a blank entry is
        the same authority expansion by another spelling and is refused too.
        """
        default_root = Path(default_working_directory or Path.cwd()).resolve()
        if allowed_roots is None:
            roots: tuple[str | Path, ...] = (default_root,)
        else:
            # A generator is consumed exactly once, here, so "yields nothing"
            # is observable rather than silently re-read as empty later.
            roots = tuple(allowed_roots)
            if not roots:
                raise ValueError(
                    "allowed_roots was explicitly empty; omit it to use the "
                    "default root, or name at least one real root"
                )
        resolved: list[Path] = []
        for root in roots:
            if not isinstance(root, (str, Path)):
                raise TypeError(f"allowed root is not a path: {type(root).__name__}")
            if not str(root).strip():
                raise ValueError("allowed root is blank")
            resolved.append(Path(root).expanduser().resolve())
        self.allowed_roots = tuple(resolved)
        # The identity of each root AS AN OBJECT, captured now - at the moment
        # the authority set is established - rather than re-derived from the
        # root's pathname at read time. Every containment decision downstream
        # is ultimately anchored here, so anchoring it to a pathname would
        # make the whole chain only as strong as that pathname: re-point the
        # root itself between validation and open and a "contained" read is
        # contained in the wrong directory. A root that does not exist gets no
        # identity, and a read under it therefore fails closed.
        self._root_identity: dict[Path, tuple[int, int]] = {}
        for root in self.allowed_roots:
            try:
                info = os.stat(root)
            except OSError:
                continue
            self._root_identity[root] = (info.st_dev, info.st_ino)
        self.default_working_directory = default_root

    # -- availability / billing ---------------------------------------------

    def availability(self) -> Availability:
        return Availability(True)

    def estimate_cost(self, task: Task) -> CostEstimate:
        """Connector-owned, never inherited from a persisted task estimate."""
        return CostEstimate(
            provider="local",
            model=None,
            minimum_usd=0.0,
            maximum_usd=0.0,
            confidence="local_filesystem_read",
            billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
            billing_evidence="local_read:no_external_call",
            # ``None``, not ``0.0``: there is no provider-side resource
            # ceiling to report, and the canonical monetary validator refuses
            # a non-positive ceiling rather than treating it as "no ceiling".
            resource_ceiling_usd=None,
            requires_paid_api=False,
        )

    # -- resource boundary ---------------------------------------------------

    def resource_boundary(self, task: Task) -> tuple[str, tuple[str, ...]]:
        """The canonical local boundary this read is authorized against.

        Called when the permission request is built AND again immediately
        before execution, so a target that leaves the roots or becomes a
        denied secret file after approval fails closed instead of executing
        under stale authority.

        The resolved target itself is bound separately and more tightly: it
        is ``ActionScope.target``, which ``AgentCore`` canonicalizes through
        ``canonical_path`` for every spelling of a local-read target before
        the scope is read at all (P1-2) - symlinks resolved, bare relative
        names included - and the scope is part of the attestation and
        therefore of the grant fingerprint. Re-pointing after approval
        changes the canonical target, which changes the fingerprint, which
        invalidates the grant.

        The stored ROOT-RELATIVE representation is checked here too, against
        the same single grammar the snapshot contract uses, so a
        credential-bearing or otherwise unrepresentable path name fails
        closed BEFORE any approval request, preview or attestation exists
        (P1-4).
        """
        try:
            working_directory = self._working_directory(task.context)
        except ValueError as exc:
            raise AttestationError(ErrorCode.EGRESS_REFUSED, str(exc)) from exc
        resolved = self._resolved_target(task)
        if resolved is None:
            raise AttestationError(ErrorCode.EGRESS_REFUSED, _NO_TARGET)
        if not self._within_allowed_roots(resolved):
            raise AttestationError(ErrorCode.EGRESS_REFUSED, _OUTSIDE_ROOTS)
        reason = local_read_denial_reason(resolved)
        if reason is not None:
            raise AttestationError(
                ErrorCode.EGRESS_REFUSED,
                f"Read target is refused by the secret-file policy: {reason}.",
            )
        reason = local_read_path_refusal(self._relative_path(resolved))
        if reason is not None:
            raise AttestationError(
                ErrorCode.EGRESS_REFUSED, f"{_UNREPRESENTABLE_PATH} {reason}."
            )
        return (
            canonical_path(working_directory),
            tuple(canonical_path(root) for root in self.allowed_roots),
        )

    # -- execution -----------------------------------------------------------

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        """The mandatory order, re-checked independently of attestation.

        canonical resolve -> allowed-root containment -> secret deny ->
        representable root-relative name -> exact resolved regular file ->
        open the file through a chain that cannot be re-pointed and prove the
        OPENED object is still in root -> physical ceiling -> binary refusal
        -> bounded representation.

        Every refusal happens before the bytes are read, except the two that
        are *about* the bytes (size, encoding); a denied file is therefore
        never held in memory at all, so its content cannot reach a
        diagnostic, a checkpoint or an event even in principle.
        """
        resolved = self._resolved_target(task)
        if resolved is None:
            return self._refusal(task, _NO_TARGET)
        if not self._within_allowed_roots(resolved):
            return self._refusal(task, _OUTSIDE_ROOTS)
        reason = local_read_denial_reason(resolved)
        if reason is not None:
            return self._refusal(
                task, f"Read target is refused by the secret-file policy: {reason}."
            )
        reason = local_read_path_refusal(self._relative_path(resolved))
        if reason is not None:
            return self._refusal(task, f"{_UNREPRESENTABLE_PATH} {reason}.")
        try:
            expected = resolved.lstat()
        except OSError:
            return self._refusal(task, _NOT_REGULAR)
        if not stat.S_ISREG(expected.st_mode):
            return self._refusal(task, _NOT_REGULAR)

        try:
            raw, opened = self._read_exact_file(resolved, expected)
        except _TargetMoved:
            return self._refusal(task, _TARGET_MOVED)
        except _MultiplyLinked:
            return self._refusal(task, _MULTIPLY_LINKED)
        except _TargetTooLarge:
            return self._refusal(task, _TOO_LARGE)
        except OSError as exc:
            # A genuine I/O fault, not a policy refusal: retryable, and the
            # message is the structured-error contract's own bounded text.
            return self._refusal(
                task,
                "Read target could not be opened.",
                code=ErrorCode.CONNECTOR_FAILED,
                retryable=True,
                details={"errno": exc.errno if isinstance(exc.errno, int) else 0},
            )

        if b"\x00" in raw:
            return self._refusal(task, _NOT_TEXT)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return self._refusal(task, _NOT_TEXT)

        excerpt, truncated = bound_local_text(text)
        # From the path the OPEN HANDLE proved, never from the pathname that
        # was merely validated, so what is persisted names the file that was
        # actually read.
        relative_path = self._relative_path(opened)
        reason = local_read_path_refusal(relative_path)
        if reason is not None:  # pragma: no cover - the pre-read check fired
            return self._refusal(task, f"{_UNREPRESENTABLE_PATH} {reason}.")
        snapshot = LocalReadSnapshot(
            relative_path=relative_path,
            byte_size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            excerpt=excerpt,
            truncated=truncated,
            # Deliberately the most restrictive value this connector could
            # possibly claim. AgentCore rebinds it from the reading task's own
            # trusted scope classification, so nothing a connector puts here
            # can establish, raise or lower authority.
            classification=DataClassification.SECRET,
            read_at=utc_now(),
        )
        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=(
                f"Read {relative_path}: {len(raw)} bytes, "
                f"{len(excerpt)}-character bounded excerpt"
                f"{' (truncated)' if truncated else ''}."
            ),
            data={LOCAL_READ_RESULT_KEY: snapshot},
            usage=self._usage(task, session),
        )

    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord:
        """Verify against the snapshot AgentCore actually bound to the task.

        Not against ``result.data``: that mapping is reduced at the execution
        boundary, so the only durable evidence is the typed task field - which
        is precisely the state a later Claude task would egress. Evidence is
        metadata only; no excerpt text is ever quoted into a verification
        record.
        """
        snapshot = task.local_read
        # The same single structural contract every other boundary uses, so
        # VERIFY cannot pass a snapshot the egress boundaries would refuse.
        if local_read_snapshot_refusal(snapshot) is not None:
            return VerificationRecord(
                status=VerificationStatus.FAILED,
                method="local_read_snapshot_integrity",
                evidence=("no valid bounded local-read snapshot on the task",),
            )
        return VerificationRecord(
            status=VerificationStatus.PASSED,
            method="local_read_snapshot_integrity",
            evidence=(
                f"path={snapshot.relative_path}",
                f"byte_size={snapshot.byte_size}",
                f"sha256={snapshot.sha256}",
                f"truncated={'true' if snapshot.truncated else 'false'}",
            ),
        )

    # -- internals -----------------------------------------------------------

    def _read_exact_file(
        self, resolved: Path, expected: os.stat_result
    ) -> tuple[bytes, Path]:
        """Read the file, and prove the bytes came from inside an allowed root.

        Returns the bytes and the AUTHORITATIVE path of the object that was
        actually opened - not the pathname that was validated earlier.

        The final-file replacement check is retained: the fstat of the opened
        descriptor must be the same ``(st_dev, st_ino)`` the pre-open stat
        saw, so a name swapped in that window is refused rather than read
        under an authorization granted for a different file. What that check
        alone cannot prove is that the file was reached through the APPROVED
        DIRECTORY CHAIN - an attacker who re-points an intermediate directory
        before the stat gets a matching device/inode pair for a file outside
        the root - so ``_open_in_root`` establishes the chain independently.

        A hard link defeats all of that by not being a link at all: it is a
        second NAME for the same file, so there is nothing to dereference, no
        reparse point to detect, and the authoritative final path is the
        in-root name it was opened by. Containment is a statement about
        names, and a file with two names is only ever inside the roots by one
        of them. The deliberate tradeoff, taken here for M3: multi-linked
        regular files are REFUSED so strict allowed-root isolation holds
        without exception. Legitimately hardlinked files - git worktrees,
        dedup and package caches - become unreadable, which is an accepted
        compatibility cost for now rather than a permanent decision.
        """
        descriptor, opened = self._open_in_root(resolved)
        with os.fdopen(descriptor, "rb") as handle:
            actual = os.fstat(handle.fileno())
            if not stat.S_ISREG(actual.st_mode) or (
                actual.st_dev,
                actual.st_ino,
            ) != (expected.st_dev, expected.st_ino):
                raise _TargetMoved()
            # Asked of the OPENED regular file only, and before a single byte
            # is read, so no outside content is ever held in memory. Nothing
            # about directory handling changes: a directory legitimately
            # carries several links on some platforms and never reaches here,
            # because the S_ISREG check above already refused it.
            if actual.st_nlink > 1:
                raise _MultiplyLinked()
            # One byte past the ceiling is read on purpose: it is the only way
            # to distinguish "exactly at the ceiling" from "over it" without
            # trusting a stat size that may be stale.
            raw = handle.read(LOCAL_READ_MAX_BYTES + 1)
        if len(raw) > LOCAL_READ_MAX_BYTES:
            raise _TargetTooLarge()
        return raw, opened

    def _open_in_root(self, resolved: Path) -> tuple[int, Path]:
        """Open ``resolved`` so that containment holds AT OPEN TIME (P1-1).

        A pre-open ``resolve()`` proves a pathname, not an object: any parent
        component can be replaced by a symlink or an NTFS junction in the
        window between validation and open, and the opened file then never
        traversed the approved chain at all. Neither ``O_NOFOLLOW`` (final
        component only, and absent on Windows) nor the device/inode binding
        catches that, because after such a swap both see the same post-swap
        file and agree with each other.

        Two platform-appropriate routes, one guarantee:

        * where the platform can open a name relative to an open DIRECTORY
          HANDLE, the chain below the root is walked handle by handle with
          ``O_NOFOLLOW``. Each step is pinned to the directory object the
          previous step opened, so no component can be re-pointed underneath
          it, and a swapped component fails the open outright. Nothing
          outside the root is opened, not even transiently.
        * on Windows there is no ``dir_fd``, so the chain is first walked with
          no-follow ``lstat`` and refused if ANY component below the root is a
          reparse point (a junction needs no privilege, which is exactly why
          ``S_ISLNK`` is not the question), and the opened handle is then
          asked for its own authoritative final path via
          ``GetFinalPathNameByHandleW``. That answer comes from the OBJECT,
          not from a pathname, so a chain swapped after the scan still lands
          outside the roots, is refused, and yields zero bytes.

        The returned path is the authoritative one on both routes.
        """
        root = self._containing_root(resolved)
        if root is None:
            raise _TargetMoved()
        parts = resolved.relative_to(root).parts
        if not parts:
            raise _TargetMoved()
        # Deliberately run on BOTH routes: redundant where directory handles
        # already pin the chain, and the only PRE-open defence where they do
        # not. It is also the one seam a coordinated-race test can drive.
        self._reject_reparse_chain(root, parts)
        if _DIRECTORY_HANDLE_TRAVERSAL:
            # Proven by construction: every step below the bound root was
            # opened no-follow relative to the previous handle.
            descriptor, opened = self._open_by_directory_handles(root, parts), resolved
        else:
            descriptor = os.open(resolved, _READ_FLAGS)
            opened = _final_path_from_handle(descriptor)
        # Containment and the file policy are re-asked of the AUTHORITATIVE
        # path on BOTH routes, so neither one can be the weaker of the two.
        try:
            if opened is None:
                raise _TargetMoved()
            if not self._within_allowed_roots(opened):
                raise _TargetMoved()
            if local_read_denial_reason(opened) is not None:
                raise _TargetMoved()
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor, opened

    def _reject_reparse_chain(self, root: Path, parts: tuple[str, ...]) -> None:
        """Refuse if the root, or any component below it, is not what it was.

        The root is checked FIRST and by identity, not merely by shape: a
        chain that starts somewhere other than the authorized root directory
        is outside the roots however clean its components look.
        """
        identity = self._root_identity.get(root)
        if identity is None:
            raise _TargetMoved()
        try:
            root_info = os.lstat(root)
        except OSError as exc:
            raise _TargetMoved() from exc
        if (root_info.st_dev, root_info.st_ino) != identity:
            raise _TargetMoved()
        current = root
        for name in parts:
            current = current / name
            try:
                info = os.lstat(current)
            except OSError as exc:
                raise _TargetMoved() from exc
            if stat.S_ISLNK(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & _REPARSE_POINT
            ):
                raise _TargetMoved()

    def _open_by_directory_handles(self, root: Path, parts: tuple[str, ...]) -> int:
        """Walk root -> file one no-follow step at a time, handle to handle.

        The walk is only as trustworthy as the directory it starts from, and
        that first ``os.open`` is the one step still taken BY PATHNAME. So the
        opened root is bound to the authorized root object before a single
        component below it is traversed: ``O_NOFOLLOW`` refuses a root path
        whose final component became a symlink, and the ``fstat`` of the
        handle must be the ``(st_dev, st_ino)`` recorded when the authority
        set was created. Only then does every subsequent no-follow,
        handle-relative step actually prove that ``root / parts`` is the file
        that was opened.
        """
        identity = self._root_identity.get(root)
        if identity is None:
            raise _TargetMoved()
        directory_flags = _READ_FLAGS | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(root, directory_flags)
        try:
            opened_root = os.fstat(parent)
            if (opened_root.st_dev, opened_root.st_ino) != identity:
                raise _TargetMoved()
            for name in parts[:-1]:
                child = os.open(name, directory_flags, dir_fd=parent)
                os.close(parent)
                parent = child
            return os.open(parts[-1], _READ_FLAGS | os.O_NOFOLLOW, dir_fd=parent)
        except OSError as exc:
            # ELOOP is a component that became a symlink, ENOTDIR one that
            # stopped being a directory, ENOENT one that vanished: all three
            # are the race, not an I/O fault, and read as "the target moved".
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENOENT):
                raise _TargetMoved() from exc
            raise
        finally:
            os.close(parent)

    def _containing_root(self, path: Path) -> Path | None:
        """The deepest allowed root containing ``path``; the chain starts there."""
        best: Path | None = None
        for root in self.allowed_roots:
            if root in path.parents and (
                best is None or len(root.parts) > len(best.parts)
            ):
                best = root
        return best

    @staticmethod
    def _resolved_target(task: Task) -> Path | None:
        """The canonical target, or ``None`` when the platform refuses it.

        ``resolve`` raises for a target the OS cannot name at all - an
        embedded NUL on POSIX - and a connector that raises out of ``execute``
        or ``resource_boundary`` turns a policy question into an exception at
        a stage boundary. ``None`` routes it to the ordinary no-target
        refusal instead. ``AgentCore`` refuses such a target earlier and more
        precisely; this keeps the connector correct when it is driven
        directly.
        """
        target = str(task.scope.target or "").strip()
        if not target:
            return None
        try:
            return Path(target).expanduser().resolve(strict=False)
        except (ValueError, OSError):
            return None

    def _relative_path(self, resolved: Path) -> str:
        """Root-relative POSIX path; no absolute path ever leaves this class.

        The longest matching root wins, so nested roots produce the shortest
        stable name. A path that matches no root cannot reach here - the
        containment check refused it - but the fallback is the bare file name
        rather than anything absolute.
        """
        best = ""
        for root in self.allowed_roots:
            if resolved == root or root in resolved.parents:
                candidate = resolved.relative_to(root).as_posix()
                if not best or len(candidate) < len(best):
                    best = candidate
        return best or resolved.name

    def _working_directory(self, context: dict[str, Any]) -> Path:
        requested = context.get("working_directory")
        path = Path(requested).resolve() if requested else self.default_working_directory
        if not path.is_dir():
            raise ValueError("Working directory does not exist.")
        if not self._within_allowed_roots(path):
            raise ValueError("Working directory is outside the allowed roots.")
        return path

    def _within_allowed_roots(self, path: Path) -> bool:
        """Containment over resolved paths; ``Path`` equality is case-aware
        per platform, so a Windows casing variant cannot escape."""
        return any(path == root or root in path.parents for root in self.allowed_roots)

    def _usage(self, task: Task, session: SessionState) -> UsageRecord:
        return UsageRecord(
            provider="local",
            model=None,
            connector=self.manifest.name,
            session_id=session.id,
            task_id=task.id,
            call_count=1,
            billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
            # An unbilled route: the state is known, and reporting any
            # monetary amount here would fail H3 reconciliation closed.
            monetary_state=MonetaryState.NOT_APPLICABLE,
            monetary_cost_usd=None,
            billing_evidence="local_read:no_external_call",
        )

    def _refusal(
        self,
        task: Task,
        message: str,
        *,
        code: ErrorCode = ErrorCode.INVALID_TASK,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> ToolResult:
        return ToolResult(
            outcome=OutcomeStatus.ERROR,
            summary=message,
            error=StructuredError(code, message, details or {}, retryable),
            usage=UsageRecord(
                provider="local",
                connector=self.manifest.name,
                task_id=task.id,
                call_count=0,
                billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
                monetary_state=MonetaryState.NOT_APPLICABLE,
                billing_evidence="local_read:no_external_call",
            ),
        )


__all__ = ["LOCAL_READ_RESULT_KEY", "LocalFileReadConnector"]
