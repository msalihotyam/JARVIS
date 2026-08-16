"""Hardened, read-only Claude Code CLI connector."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from jarvis.connectors.base import BaseConnector, ConnectorManifest
from jarvis.contracts import (
    Availability,
    CostEstimate,
    DataClassification,
    ErrorCode,
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


SECRET_KEY_PATTERN = re.compile(
    r"(?:api[_-]?key|token|secret|password|passwd|authorization|cookie|private[_-]?key)",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)


class ClaudeCodeConnector(BaseConnector):
    manifest = ConnectorManifest(
        name="claude-code-plan",
        capabilities=("code_analysis", "code_planning"),
        description="Read-only code analysis through an isolated Claude Code CLI call.",
        priority=50,
        default_risk=RiskLevel.MEDIUM,
        default_side_effect=SideEffectProfile(
            effect=SideEffect.EXTERNAL_EGRESS,
            reversible=True,
            always_requires_approval=True,
        ),
        timeout_seconds=120,
        is_paid=False,
    )

    def __init__(
        self,
        command: str = "claude",
        allowed_roots: tuple[str | Path, ...] | None = None,
        default_working_directory: str | Path | None = None,
        timeout_seconds: int | None = None,
        max_budget_usd: float = 0.25,
    ) -> None:
        default_root = Path(default_working_directory or Path.cwd()).resolve()
        roots = allowed_roots or (default_root,)
        self.allowed_roots = tuple(Path(root).resolve() for root in roots)
        if not self.allowed_roots:
            raise ValueError("At least one allowed root is required")
        self.default_working_directory = default_root
        self.command = command
        self.timeout_seconds = timeout_seconds or self.manifest.timeout_seconds
        self.max_budget_usd = max_budget_usd
        if self.max_budget_usd <= 0:
            raise ValueError("max_budget_usd must be positive")
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._process_lock = threading.Lock()
        self._last_payload_meta: dict[str, dict[str, Any]] = {}

    def availability(self) -> Availability:
        command_path = shutil.which(self.command)
        if command_path is None and not Path(self.command).is_file():
            return Availability(False, "Claude Code CLI is not installed or not on PATH")
        return Availability(True)

    def estimate_cost(self, task: Task) -> CostEstimate:
        if task.estimate.maximum_usd > 0:
            return task.estimate
        return CostEstimate(
            provider="anthropic",
            model=None,
            minimum_usd=0.0,
            maximum_usd=self.max_budget_usd,
            confidence="guardrail_limit",
            is_paid=False,
        )

    def outbound_preview(self, task: Task) -> OutboundDataPreview:
        payload, redacted_count = self._sanitized_payload(task)
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        field_names = tuple(sorted(self._field_names(payload)))
        return OutboundDataPreview(
            destination="anthropic/claude-code",
            data_classification=task.scope.data_classification,
            field_names=field_names,
            approximate_bytes=len(encoded),
            redacted_items=redacted_count,
            summary="Objective and redacted task context will leave the local machine.",
        )

    def execute(self, task: Task, session: SessionState) -> ToolResult:
        try:
            working_directory = self._working_directory(task.context)
        except ValueError as exc:
            return self._error_result(
                task,
                ErrorCode.CONNECTOR_FAILED,
                str(exc),
                retryable=False,
            )

        prompt, redacted_count = self._build_prompt(task)
        command = self._command(prompt)
        try:
            process = subprocess.Popen(
                command,
                cwd=working_directory,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
        except FileNotFoundError:
            return self._error_result(
                task,
                ErrorCode.CONNECTOR_UNAVAILABLE,
                "Claude Code CLI is not installed or is not on PATH.",
                retryable=True,
            )

        with self._process_lock:
            self._processes[task.id] = process
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return self._error_result(
                task,
                ErrorCode.CONNECTOR_FAILED,
                "Claude Code did not finish before the timeout.",
                retryable=True,
            )
        finally:
            with self._process_lock:
                self._processes.pop(task.id, None)

        if task.cancellation_requested:
            return ToolResult(
                outcome=OutcomeStatus.CANCELLED,
                summary="Claude Code task was cancelled.",
                error=StructuredError(ErrorCode.CANCELLED, "Task cancelled"),
                usage=self._usage(task, {}),
            )

        payload = self._parse_payload(stdout)
        if payload is None:
            detail = stderr.strip() or stdout.strip()
            safe_detail, _ = self._redact(detail)
            return self._error_result(
                task,
                ErrorCode.CONNECTOR_FAILED,
                "Claude Code returned an unreadable response.",
                details={"cli_detail": str(safe_detail)[:1000]},
                retryable=True,
            )

        usage = self._usage(task, payload)
        result_text = str(payload.get("result") or "").strip()
        failed = process.returncode != 0 or bool(payload.get("is_error"))
        metadata = {
            "response_type": payload.get("type"),
            "subtype": payload.get("subtype"),
            "stop_reason": payload.get("stop_reason"),
            "redacted_items": redacted_count,
            "api_error_status": payload.get("api_error_status"),
        }
        self._last_payload_meta[task.id] = metadata
        if failed:
            return ToolResult(
                outcome=OutcomeStatus.ERROR,
                summary=result_text or "Claude Code request failed.",
                data=metadata,
                error=StructuredError(
                    ErrorCode.CONNECTOR_FAILED,
                    result_text or "Claude Code request failed.",
                    details=metadata,
                    retryable=payload.get("api_error_status") in {401, 408, 429, 500, 503},
                ),
                usage=usage,
            )

        return ToolResult(
            outcome=OutcomeStatus.SUCCESS,
            summary=result_text or "Claude Code completed the request.",
            data=metadata,
            usage=usage,
        )

    def verify(
        self, task: Task, result: ToolResult, session: SessionState
    ) -> VerificationRecord:
        metadata = self._last_payload_meta.get(task.id, result.data)
        evidence = []
        if result.summary.strip():
            evidence.append("non_empty_result")
        if metadata.get("response_type") == "result":
            evidence.append("structured_result_envelope")
        if result.usage.connector == self.manifest.name:
            evidence.append("usage_attributed_to_connector")
        status = (
            VerificationStatus.PASSED
            if len(evidence) >= 2 and result.ok
            else VerificationStatus.FAILED
        )
        return VerificationRecord(
            status=status,
            method="claude_cli_structured_response",
            evidence=tuple(evidence),
        )

    def cancel(self, task_id: str) -> bool:
        with self._process_lock:
            process = self._processes.get(task_id)
        if process is None or process.poll() is not None:
            return False
        process.terminate()
        return True

    def _working_directory(self, context: dict[str, Any]) -> Path:
        requested = context.get("working_directory")
        path = Path(requested).resolve() if requested else self.default_working_directory
        if not path.is_dir():
            raise ValueError(f"Working directory does not exist: {path}")
        if not any(path == root or root in path.parents for root in self.allowed_roots):
            raise ValueError(f"Working directory is outside allowed roots: {path}")
        return path

    def _command(self, prompt: str) -> list[str]:
        empty_mcp = json.dumps({"mcpServers": {}}, separators=(",", ":"))
        return [
            self.command,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--permission-mode",
            "plan",
            "--safe-mode",
            "--strict-mcp-config",
            "--mcp-config",
            empty_mcp,
            "--tools",
            "Read,Glob,Grep",
            "--disable-slash-commands",
            "--no-chrome",
            "--no-session-persistence",
            "--max-turns",
            "3",
            "--max-budget-usd",
            f"{self.max_budget_usd:.4f}",
            "--system-prompt",
            (
                "You are a read-only code planning component. Never modify files, "
                "run state-changing commands, use external connectors, or disclose secrets."
            ),
        ]

    def _build_prompt(self, task: Task) -> tuple[str, int]:
        payload, redacted_count = self._sanitized_payload(task)
        return (
            "JARVIS read-only planning request. Return analysis and a proposed plan only.\n"
            + json.dumps(payload, ensure_ascii=False, default=str),
            redacted_count,
        )

    def _sanitized_payload(self, task: Task) -> tuple[dict[str, Any], int]:
        if task.scope.data_classification is DataClassification.SECRET:
            return (
                {
                    "objective": "[REDACTED_SECRET_CLASSIFICATION]",
                    "capability": task.capability,
                    "scope": {
                        "data_classification": DataClassification.SECRET.value,
                        "details": "[REDACTED_SECRET_SCOPE]",
                    },
                    "context": "[REDACTED_SECRET_CLASSIFICATION]",
                    "preferences": {},
                },
                6,
            )
        objective, objective_redactions = self._redact(task.objective)
        context, context_redactions = self._redact(task.context)
        preferences, preferences_redactions = self._redact(task.preferences)
        payload = {
            "objective": objective,
            "capability": task.capability,
            "scope": task.scope.to_dict(),
            "context": context,
            "preferences": preferences,
        }
        return payload, objective_redactions + context_redactions + preferences_redactions

    @classmethod
    def _redact(cls, value: Any, key: str = "") -> tuple[Any, int]:
        if key and SECRET_KEY_PATTERN.search(key):
            return "[REDACTED]", 1
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            count = 0
            for child_key, child_value in value.items():
                redacted, child_count = cls._redact(child_value, str(child_key))
                output[str(child_key)] = redacted
                count += child_count
            return output, count
        if isinstance(value, (list, tuple, set)):
            output_list = []
            count = 0
            for item in value:
                redacted, child_count = cls._redact(item)
                output_list.append(redacted)
                count += child_count
            return output_list, count
        if isinstance(value, str):
            text = value
            count = 0
            for pattern in SECRET_VALUE_PATTERNS:
                text, replacements = pattern.subn("[REDACTED]", text)
                count += replacements
            return text, count
        return value, 0

    @staticmethod
    def _field_names(value: Any, prefix: str = "") -> set[str]:
        names: set[str] = set()
        if isinstance(value, dict):
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                names.add(path)
                names.update(ClaudeCodeConnector._field_names(child, path))
        elif isinstance(value, list):
            for child in value:
                names.update(ClaudeCodeConnector._field_names(child, prefix))
        return names

    @staticmethod
    def _parse_payload(stdout: str) -> dict[str, Any] | None:
        for line in reversed(stdout.splitlines()):
            try:
                payload = json.loads(line.strip())
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                return payload
        return None

    def _usage(self, task: Task, payload: dict[str, Any]) -> UsageRecord:
        usage_data = payload.get("usage") or {}
        model_usage = payload.get("modelUsage") or {}
        model = str(next(iter(model_usage))) if isinstance(model_usage, dict) and model_usage else None
        return UsageRecord(
            provider="anthropic",
            model=model,
            connector=self.manifest.name,
            task_id=task.id,
            input_tokens=int(usage_data.get("input_tokens") or 0),
            output_tokens=int(usage_data.get("output_tokens") or 0),
            estimated_cost_usd=task.estimate.maximum_usd,
            actual_cost_usd=float(payload.get("total_cost_usd") or 0.0),
            is_paid=float(payload.get("total_cost_usd") or 0.0) > 0,
        )

    def _error_result(
        self,
        task: Task,
        code: ErrorCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool,
    ) -> ToolResult:
        return ToolResult(
            outcome=OutcomeStatus.ERROR,
            summary=message,
            error=StructuredError(code, message, details or {}, retryable),
            usage=self._usage(task, {}),
        )
