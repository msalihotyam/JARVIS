"""V2 Projects Gate 6H H3 (K3): create and link a project's folder from chat.

The first local write a chat request can reach (Salih 2026-10-10). It is
allowed because JARVIS computes everything the write does: the model only
picks the capability and "desktop" or not; the path is ``propose_folder`` on
the STORED project name, computed once by the app and carried in the
connector-private ``task.context["project_folder"]`` (never in the objective,
scope, events, turns or any model payload). LOCAL_WRITE keeps the approval
mandatory in every mode, and no remembered session grant covers it.

Core's attestation (``canonical_outbound``) binds ``{project_id, path,
location}`` and, at every attestation, refuses unless the project is still
active and folderless, the path is still lexically allowed and
``propose_folder`` NOW returns exactly that path. An ``AttestationError``
ends the task (FAILED): nothing is re-proposed by itself. After Core's final
re-attestation ``execute`` creates exactly the context path through
``create_and_link`` (its transaction re-checks the project) and never another
candidate. Every method keeps paths out of its errors: Core stores an
exception's text, and only the local approval card shows the path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from jarvis import project_folders
from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.contracts import (
    AttestationError,
    BillingClass,
    CostEstimate,
    ErrorCode,
    MonetaryState,
    OutboundDataPreview,
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
)

PROJECT_FOLDER_CONNECTOR = "project-folder-create"
PROJECT_FOLDER_CAPABILITY = "project_folder_create"
#: The connector-private task context key: ``{"path", "location"}``.
PROJECT_FOLDER_CONTEXT_KEY = "project_folder"
#: The scope target the model may give, and the location each one means.
PROJECT_FOLDER_TARGETS = {"": "documents", "desktop": "desktop"}
PROJECT_FOLDER_FRAME_HEADER = b"JARVIS-PROJECT-FOLDER-CREATE/1"

# Every message below is a stable code, not words (D-6): Core stores and logs
# it as written, and the window says it in the interface language
# (``jarvis.ui.present``). A changed sentence here needs its mapping there.
STALE_PROPOSAL = "The folder proposal changed; ask again."
CREATED = "The project folder was created and linked to the project."
NOTHING_CREATED = "The project folder could not be created; no folder was created and nothing changed."
#: ``_partial``'s two cases, each followed by " Project link: <linked|not linked|unknown>."
PARTIAL_LEAF = ("The project folder was created but could not be linked; it was not deleted "
                "(its path was on the approval card).")
PARTIAL_PARENT = ("Only the shared \"JARVIS Projeleri\" folder was created; the project folder was not, "
                  "and nothing was deleted.")
#: The approval card's summary, per location.
PREVIEW = {"desktop": "Create the project folder on the Desktop and link it; nothing is sent.",
           "documents": "Create the project folder in Documents\\JARVIS Projeleri and link it; nothing is sent."}
#: What ``project_folders`` appends to a created path its handle could not locate.
UNVERIFIED_SUFFIX = f" ({project_folders._UNVERIFIED})"


class ProjectFolderCreateConnector(BaseConnector):
    """Create exactly the approved project folder and link it (K3)."""

    _billing_evidence = "project_folder:no_external_call"

    manifest = ConnectorManifest(
        name=PROJECT_FOLDER_CONNECTOR,
        capabilities=(PROJECT_FOLDER_CAPABILITY,),
        description=(
            "Create the project's folder at the exact path JARVIS proposed and "
            "the person approved, and link it to the project as context."
        ),
        priority=60,
        default_risk=RiskLevel.MEDIUM,
        default_side_effect=SideEffectProfile(
            effect=SideEffect.LOCAL_WRITE,
            # Nothing JARVIS created is ever removed (G6H1b D4), and LOCAL_WRITE
            # is in MANDATORY_APPROVAL_EFFECTS: every mode asks, every time.
            reversible=False,
            always_requires_approval=True,
        ),
        timeout_seconds=30,
        default_billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
        requires_paid_api=False,
    )

    def __init__(self, store: Any, protected: Iterable[str | Path]) -> None:
        #: The app's own store object (one per composition) and the protected
        #: roots ``project_folder_result`` uses (``app._folder_protected``).
        self.store = store
        self.protected = tuple(protected)

    # -- the proposal: JARVIS's, never the model's ----------------------------

    def proposal(self, project_id: Any, location: str) -> dict[str, str]:
        """``{"path", "location"}`` for an active, folderless project: where
        ``propose_folder`` puts its folder NOW. Raises on anything else."""
        if location not in PROJECT_FOLDER_TARGETS.values():
            raise ValueError("unknown location")
        project = self.store.load_project(project_id) if type(project_id) is str else None
        if project is None or not project.active or self.store.load_project_folder(project_id) is not None:
            raise ValueError("the project is not an active project without a folder")
        path = project_folders.propose_folder(project.name, location == "desktop", protected=self.protected)
        return {"path": path, "location": location}

    def _bound(self, task: Task) -> tuple[str, str, str]:
        """``(project_id, path, location)`` exactly as the task carries them."""
        folder = task.context.get(PROJECT_FOLDER_CONTEXT_KEY) if type(task.context) is dict else None
        if type(folder) is not dict or set(folder) != {"path", "location"}:
            raise ValueError("no project folder proposal")
        path, location = folder["path"], folder["location"]
        if type(path) is not str or PROJECT_FOLDER_TARGETS.get(task.scope.target) != location:
            raise ValueError("an inconsistent project folder proposal")
        if type(task.project_id) is not str:
            raise ValueError("no project")
        return task.project_id, path, location

    # -- availability / billing ------------------------------------------------

    def estimate_cost(self, task: Task) -> CostEstimate:
        return CostEstimate(
            provider="local",
            model=None,
            minimum_usd=0.0,
            maximum_usd=0.0,
            confidence="local_folder_create",
            billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
            billing_evidence=self._billing_evidence,
            resource_ceiling_usd=None,
            requires_paid_api=False,
        )

    # -- authority -------------------------------------------------------------

    def canonical_outbound(self, task: Task) -> bytes:
        """The exact effect the approval binds - project, path, location - and
        the one freshness check, run at EVERY attestation (the card's and
        Core's final one before execution). ``ensure_ascii`` keeps Turkish
        letters as lossless escapes; the path never appears in the error."""
        try:
            project_id, path, location = self._bound(task)
            if (project_folders.check_lexical(path, protected=self.protected) != path
                    or self.proposal(project_id, location) != {"path": path, "location": location}):
                raise ValueError("stale")
        except Exception:  # noqa: BLE001 - every refusal is the same path-free one
            raise AttestationError(ErrorCode.EGRESS_REFUSED, STALE_PROPOSAL) from None
        header = json.dumps({"project_id": project_id, "path": path, "location": location},
                            sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        return PROJECT_FOLDER_FRAME_HEADER + b"\n" + header

    def outbound_preview(self, task: Task) -> OutboundDataPreview | None:
        """Metadata only. The path is NOT here: Core writes the whole request
        into the event log. The local card reads it from the task (K3)."""
        try:
            _, _, location = self._bound(task)
        except Exception:  # noqa: BLE001 - attestation refuses it next
            return None
        return OutboundDataPreview(
            # The scope's destination: Core refuses a preview naming another.
            destination=task.scope.destination,
            data_classification=task.scope.data_classification,
            field_names=("project_id", "path", "location"),
            approximate_bytes=0,
            redacted_items=0,
            summary=PREVIEW[location],
        )

    # -- execution -------------------------------------------------------------

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        """Exactly the context path, after Core's final re-attestation; a
        collision refuses (nothing is reused), a rename does not stop it."""
        try:
            project_id, path, _ = self._bound(task)
        except Exception:  # noqa: BLE001
            return self._refusal(task, STALE_PROPOSAL)
        try:
            project_folders.create_and_link(self.store, project_id, path, protected=self.protected)
        except project_folders.FolderCreateError as exc:
            return self._refusal(task, self._partial(exc, path))
        except Exception:  # noqa: BLE001 - nothing was created (create_and_link's contract)
            return self._refusal(task, NOTHING_CREATED)
        return ToolResult(outcome=OutcomeStatus.SUCCESS, summary=CREATED, usage=self._usage(task, session, 1))

    @staticmethod
    def _partial(exc: project_folders.FolderCreateError, path: str) -> str:
        """What was created and kept, in words, never as a path (D4)."""
        state = exc.link_state if exc.link_state in ("linked", "not linked") else "unknown"
        try:
            created = [c[: -len(UNVERIFIED_SUFFIX)] if c.endswith(UNVERIFIED_SUFFIX) else c for c in exc.created]
            leaf = any(project_folders.same_folder(c, path) for c in created)
        except Exception:  # noqa: BLE001 - unreadable: say the stronger case
            leaf = True
        return f"{PARTIAL_LEAF if leaf else PARTIAL_PARENT} Project link: {state}."

    def verify(self, task: Task, result: ToolResult, session: SessionState) -> VerificationRecord:
        """Read back: the project's link is the created folder at the path."""
        try:
            project_id, path, _ = self._bound(task)
            link = self.store.load_project_folder(project_id)
            ok = (link is not None and link.origin == "created"
                  and project_folders.same_folder(link.path, path) and os.path.isdir(path))
        except Exception:  # noqa: BLE001
            ok = False
        return VerificationRecord(
            status=VerificationStatus.PASSED if ok else VerificationStatus.FAILED,
            method="project_folder_link_read_back",
            evidence=("the created folder exists and is the project's linked folder" if ok
                      else "the created folder link could not be read back",),
        )

    def _usage(self, task: Task, session: SessionState | None, calls: int) -> UsageRecord:
        return UsageRecord(
            provider="local",
            connector=self.manifest.name,
            session_id=None if session is None else session.id,
            task_id=task.id,
            call_count=calls,
            billing_class=BillingClass.NO_ADDITIONAL_CHARGE,
            monetary_state=MonetaryState.NOT_APPLICABLE,
            billing_evidence=self._billing_evidence,
        )

    def _refusal(self, task: Task, message: str) -> ToolResult:
        return ToolResult(
            outcome=OutcomeStatus.ERROR,
            summary=message,
            error=StructuredError(ErrorCode.INVALID_TASK, message),
            usage=self._usage(task, None, 0),
        )


__all__ = [
    "CREATED",
    "NOTHING_CREATED",
    "PARTIAL_LEAF",
    "PARTIAL_PARENT",
    "PREVIEW",
    "PROJECT_FOLDER_CAPABILITY",
    "PROJECT_FOLDER_CONNECTOR",
    "PROJECT_FOLDER_CONTEXT_KEY",
    "PROJECT_FOLDER_TARGETS",
    "ProjectFolderCreateConnector",
    "STALE_PROPOSAL",
    "UNVERIFIED_SUFFIX",
]
