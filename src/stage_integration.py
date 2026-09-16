"""Step 9 Codex-first Stage integration adapter.

This module is intentionally a thin orchestration boundary around the Step 7
``StageController`` and the Step 8 Git/context/artifact helpers.  It does not
run a worker loop and it never lets a GPT response call GPT or execute a local
action implicitly.  A caller must publish local evidence, explicitly request
one consultation, explicitly read the response, and then explicitly execute a
scoped local action.

The bridge is injected as ``bridge_runner`` so deterministic tests do not need
the browser.  ``subprocess_bridge_runner`` is provided for the headed E2E and
invokes the existing Node ``consult-pack`` entry point exactly once per call.
The Node bridge may reconnect to a machine-local Project Browser owned across
these short-lived invocations. It removes transient request/response/spec
files after the bounded metadata has been read; receipts never contain
prompts, raw replies, cookies, tokens, session state, or DOM dumps.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .artifacts import validate_artifact_manifest
from .bridge_adapter import (
    BridgeEnvelopeError,
    is_bridge_envelope,
    normalize_bridge_envelope,
    normalize_project_url as normalize_bridge_project_url,
)
from .contracts import ContractValidationError, canonical_json, sha256_json, validate_against_schema
from .git_baseline import capture_git_baseline, verify_git_baseline
from .stage_context import (
    build_stage_context,
    verify_stage_context,
    write_stage_context,
)
from .stage_controller import StageController, StageControllerError, StageState
from .supervisor import architecture_check


INTEGRATION_MARKER = "CODEX_GPT_STAGE_INTEGRATION_PASS"
INTEGRATION_BLOCKED_MARKER = "CODEX_GPT_STAGE_INTEGRATION_BLOCKED"
BRIDGE_REQUEST_COUNT = 1
DEFAULT_SAME_ABSTRACTION_THRESHOLD = 2
DEFAULT_EMERGENCY_ITERATION_CEILING = 12
DECISIONS = frozenset({"CONTINUE", "REPLAN", "STAGE_READY", "HUMAN_GATE", "BLOCKED"})
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_DECISION_LABEL = re.compile(r"\bWORKFLOW_DECISION\b", re.IGNORECASE)
_DECISION_LINE = re.compile(r"^\s*WORKFLOW_DECISION\s*:\s*(\S+)\s*$")
_SENSITIVE_KEYS = {
    "cookie",
    "cookies",
    "token",
    "tokens",
    "authorization",
    "session",
    "session_storage",
    "storage_state",
    "raw_dom",
    "dom",
    "prompt",
    "raw_response",
}
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE),
)
_BRIDGE_FAILURE_MARKER = "CONTEXT_PACK_CONSULTATION_FAILED"
_MAX_BRIDGE_FAILURE_CODE = 128
_MAX_STDERR_SUMMARY = 256
_FAILURE_CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_STDERR_SENSITIVE_LINE_PATTERN = re.compile(
    r"\b(?:prompt|cookie|cookies|token|tokens|authorization|session|storage|raw[_ -]?dom|dom|spec|path|file|filename|directory|folder)\b",
    re.IGNORECASE,
)
_STDERR_LONG_TOKEN_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")
_STDERR_SAFE_CHAR_PATTERN = re.compile(r"[^A-Za-z0-9 _.,:;()=_-]+")
_EXCEPTION_CLASS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_FAILURE_PHASES = frozenset(
    {
        "BRIDGE_RUNNER",
        "BRIDGE_SUBPROCESS",
        "BRIDGE_RESULT",
    }
)


class StageIntegrationError(RuntimeError):
    """A fail-closed integration guard or bounded bridge failure."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})
        self.cause = cause


def _known_request_count(value: Any) -> int | None:
    """Return a request count only when a receipt/marker established it."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value in {0, BRIDGE_REQUEST_COUNT} else None


def _stable_exception_class(value: Any) -> str:
    candidate = value if isinstance(value, str) else ""
    return candidate if _EXCEPTION_CLASS_PATTERN.fullmatch(candidate) else "UnknownException"


def _forensic_details(
    code: Any,
    exception_class: Any,
    *,
    phase: str = "BRIDGE_SUBPROCESS",
    request_count: Any = None,
    attempt_count: int = 1,
) -> dict[str, Any]:
    """Create the bounded diagnostic subset shared across bridge failures."""

    normalized_code = _normalise_bridge_failure_code(code, default="BRIDGE_EXTERNAL_FAILURE")
    return {
        "failure_phase": phase if phase in _FAILURE_PHASES else "BRIDGE_SUBPROCESS",
        "exception_class": _stable_exception_class(exception_class),
        "error_code": normalized_code,
        "request_count": _known_request_count(request_count),
        "attempt_count": 1 if attempt_count != 1 else attempt_count,
    }


@dataclass(frozen=True)
class EvidenceRecord:
    revision: int
    digest: str
    abstraction_layer: str
    user_visible_improvement: bool | None
    architecture: Mapping[str, Any]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class ConsultationHandle:
    consultation_id: str
    mode: str
    bridge_mode: str
    evidence_revision: int
    evidence_digest: str
    context_pack_id: str | None
    response_text: str
    receipt_path: str | None
    receipt: Mapping[str, Any]


def _bounded_text(value: Any, field: str, *, required: bool = True, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise StageIntegrationError("INTEGRATION_INVALID_INPUT", f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise StageIntegrationError("INTEGRATION_INVALID_INPUT", f"{field} must be non-empty")
    if len(text) > maximum or "\x00" in text:
        raise StageIntegrationError("INTEGRATION_INVALID_INPUT", f"{field} is too long or contains NUL")
    return text


def _safe_value(value: Any, field: str = "value", *, depth: int = 8) -> Any:
    """Reject sensitive transport material rather than redacting it."""

    if depth < 0:
        raise StageIntegrationError("INTEGRATION_SENSITIVE_DATA", f"{field} is too deeply nested")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.lower() in _SENSITIVE_KEYS:
                raise StageIntegrationError("INTEGRATION_SENSITIVE_DATA", f"{field}.{key} is not allowed")
            result[key] = _safe_value(child, f"{field}.{key}", depth=depth - 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, f"{field}[]", depth=depth - 1) for item in value]
    if isinstance(value, str):
        if len(value) > 12000:
            raise StageIntegrationError("INTEGRATION_UNBOUNDED_DATA", f"{field} contains an unbounded string")
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise StageIntegrationError("INTEGRATION_SENSITIVE_DATA", f"secret-like material in {field}")
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    raise StageIntegrationError("INTEGRATION_INVALID_INPUT", f"unsupported value at {field}")


def parse_dialogue_decision(response_text: str) -> str:
    """Parse the Step 6 closed decision vocabulary, fail-closed."""

    if not isinstance(response_text, str) or not response_text.strip():
        raise StageIntegrationError("GPT_DECISION_INVALID", "GPT response is empty")
    lines = response_text.splitlines()
    marker_lines = [line for line in lines if _DECISION_LABEL.search(line)]
    if len(marker_lines) != 1:
        raise StageIntegrationError("GPT_DECISION_INVALID", "response must contain exactly one workflow decision marker")
    match = _DECISION_LINE.match(marker_lines[0])
    if match is None or match.group(1) not in DECISIONS:
        raise StageIntegrationError("GPT_DECISION_INVALID", "workflow decision is unknown or malformed")
    return match.group(1)


def _response_digest(response_text: str) -> str:
    return hashlib.sha256(response_text.encode("utf-8")).hexdigest()


def _normalise_bridge_result(result: Mapping[str, Any]) -> tuple[str, str, str, dict[str, Any], str | None]:
    if not isinstance(result, Mapping):
        raise StageIntegrationError("BRIDGE_RESULT_INVALID", "bridge runner must return an object")
    try:
        normalized = normalize_bridge_envelope(result)
    except BridgeEnvelopeError as exc:
        raise StageIntegrationError(exc.code, str(exc), details=exc.details) from exc
    response_text = normalized["response_text"]
    consultation_id = normalized["consultation_id"]
    request_count = normalized["request_count"]
    receipt = normalized["receipt"]
    receipt_path = normalized.get("receipt_path")
    # Keep the Stage receipt contract deliberately local and bounded even when
    # a custom runner supplied a richer bridge envelope.
    safe_receipt = _safe_value(dict(receipt), "bridge receipt")
    if not isinstance(safe_receipt, dict):  # pragma: no cover - generic guard
        raise StageIntegrationError("BRIDGE_RESULT_INVALID", "bridge receipt is not an object")
    return (
        consultation_id,
        response_text,
        str(request_count),
        safe_receipt,
        receipt_path,
    )


def _summary_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only bounded, non-secret bridge receipt metadata."""

    allowed = {
        "consultation_id",
        "created_at",
        "mode",
        "request_count",
        "status",
        "conversation_id",
        "parent_consultation_id",
        "conversation_root_consultation_id",
        "conversation_validated",
        "response_char_count",
        "context_pack",
        "failure_code",
        "project_url",
        "project_scope_requested",
        "project_scope_verified",
        "project_scope_evidence",
        "chatgpt_target_mode",
        "chatgpt_target_url_digest",
        "chatgpt_target_origin",
        "chatgpt_project_target_verified",
        "fresh_project_chat_created",
        "browser_process_evidence",
        "browser_failure_class",
    }
    return {key: copy.deepcopy(value) for key, value in receipt.items() if key in allowed}


class StageIntegrationAdapter:
    """Explicit Codex↔GPT orchestration around one active Stage.

    ``bridge_runner`` is a callable with this shape::

        runner(prompt, mode="fresh"|"continue", continue_from=None,
               context_pack=None, root_dir=..., profile_dir=...,
               timeout_ms=300000) -> mapping

    The runner is called once per ``consult_gpt`` invocation.  No method in
    this class invokes it as a side effect of parsing a response or executing
    a local action.
    """

    def __init__(
        self,
        controller: StageController,
        *,
        stage_id: str | None = None,
        repository_root: str | os.PathLike[str] | None = None,
        bridge_runner: Callable[..., Mapping[str, Any]] | None = None,
        receipt_root: str | os.PathLike[str] | None = None,
        same_abstraction_threshold: int = DEFAULT_SAME_ABSTRACTION_THRESHOLD,
        emergency_iteration_ceiling: int = DEFAULT_EMERGENCY_ITERATION_CEILING,
    ) -> None:
        if not isinstance(controller, StageController):
            raise StageIntegrationError("INTEGRATION_INVALID_CONTROLLER", "controller must be a StageController")
        if not isinstance(same_abstraction_threshold, int) or isinstance(same_abstraction_threshold, bool) or same_abstraction_threshold < 2:
            raise StageIntegrationError("INTEGRATION_INVALID_INPUT", "same_abstraction_threshold must be >= 2")
        if not isinstance(emergency_iteration_ceiling, int) or isinstance(emergency_iteration_ceiling, bool) or emergency_iteration_ceiling < same_abstraction_threshold:
            raise StageIntegrationError("INTEGRATION_INVALID_INPUT", "emergency_iteration_ceiling is too small")
        self.controller = controller
        self.stage_id = stage_id or controller.state.get("current_stage_id")
        if not isinstance(self.stage_id, str) or not self.stage_id:
            raise StageIntegrationError("INTEGRATION_INVALID_CONTROLLER", "an explicit stage_id is required")
        record = controller.show_stage(self.stage_id)
        contract_root = record["contract"].get("repository_root")
        self.repository_root = Path(repository_root or contract_root).expanduser().resolve()
        self.bridge_runner = bridge_runner
        self.same_abstraction_threshold = same_abstraction_threshold
        self.emergency_iteration_ceiling = emergency_iteration_ceiling
        self._evidence: EvidenceRecord | None = None
        self._next_revision = 0
        self._last_consult_revision = 0
        self._consulted: set[tuple[str, str]] = set()
        self._pending: ConsultationHandle | None = None
        self._read_responses: dict[str, dict[str, Any]] = {}
        self._same_abstraction_count = 0
        self._events: list[dict[str, Any]] = []
        self._receipt_path: Path | None = None
        self._failure_code: str | None = None
        self._stop_reason: str | None = None
        if receipt_root is not None:
            root = Path(receipt_root).expanduser().resolve()
            root.mkdir(parents=True, exist_ok=True)
            self._receipt_path = root / f"stage-integration-{uuid.uuid4().hex[:12]}.json"
            self._persist_receipt()

    @property
    def status(self) -> str:
        return str(self.controller.show_stage(self.stage_id)["status"])

    @property
    def receipt_path(self) -> Path | None:
        return self._receipt_path

    @property
    def latest_evidence(self) -> EvidenceRecord | None:
        return self._evidence

    def _guard_active(self, operation: str) -> None:
        status = self.status
        if status == StageState.ACTIVE.value:
            return
        code = {
            StageState.STAGE_READY.value: "STAGE_READY_LOCKED",
            StageState.APPROVED.value: "STAGE_APPROVED_LOCKED",
            StageState.STOPPED.value: "STAGE_STOPPED_LOCKED",
            StageState.BLOCKED.value: "STAGE_BLOCKED_LOCKED",
            StageState.PLANNED.value: "STAGE_NOT_STARTED",
        }.get(status, "STAGE_NOT_ACTIVE")
        raise StageIntegrationError(code, f"{operation} requires ACTIVE Stage; current status is {status}")

    def _ensure_no_pending_response(self, operation: str) -> None:
        if self._pending is not None and self._pending.consultation_id not in self._read_responses:
            raise StageIntegrationError(
                "GPT_RESPONSE_NOT_READ",
                f"{operation} is blocked until Codex explicitly reads the GPT response",
            )

    def _check_iteration_ceiling(self) -> None:
        stage = self.controller.show_stage(self.stage_id)
        current = int(stage.get("iteration_index", 0))
        if current >= self.emergency_iteration_ceiling:
            raise StageIntegrationError(
                "EMERGENCY_ITERATION_CEILING",
                "the emergency iteration ceiling is a fail-closed safety limit",
                details={"iteration_index": current, "ceiling": self.emergency_iteration_ceiling},
            )

    def _append_event(self, event_type: str, **details: Any) -> None:
        safe_details = _safe_value(details, "integration event")
        if event_type in {"consultation_failed", "local_action_failed"}:
            failure_code = safe_details.get("error_code") or safe_details.get("error_class")
            if not isinstance(failure_code, str) or not failure_code.strip():
                failure_code = "INTEGRATION_FAILURE"
            self._failure_code = _bounded_text(failure_code, "failure_code", maximum=128)
            self._stop_reason = _bounded_text(
                str(safe_details.get("stop_reason") or event_type),
                "stop_reason",
                maximum=256,
            )
        self._events.append({"event": event_type, **safe_details})
        self._persist_receipt()

    def _receipt_payload(self) -> dict[str, Any]:
        stage = self.controller.show_stage(self.stage_id)
        latest = self._evidence
        status = str(stage["status"])
        if status == StageState.STAGE_READY.value:
            marker: str | None = INTEGRATION_MARKER
        elif self._failure_code is not None:
            marker = INTEGRATION_BLOCKED_MARKER
        else:
            marker = None
        payload = {
            "schema_version": "stage_integration_receipt.v1",
            "marker": marker,
            "stage_id": self.stage_id,
            "status": status,
            "failure_code": self._failure_code,
            "stop_reason": self._stop_reason,
            "baseline_digest": stage.get("baseline_digest"),
            "iteration_index": stage.get("iteration_index", 0),
            "open_iteration_index": stage.get("open_iteration_index"),
            # Stage iteration and execution attempts are deliberately exposed
            # as separate receipt fields.  The integration receipt is a
            # bounded projection of controller state; it must never make a
            # provider retry look like a new Stage iteration.
            "attempt_count": stage.get("attempt_count", 0),
            "retry_count": stage.get("retry_count", 0),
            "retry_budget": stage.get("retry_budget"),
            "execution_evidence_complete": bool(stage.get("execution_evidence_complete", False)),
            "latest_attempt": (
                copy.deepcopy(stage.get("execution_attempts", [])[-1])
                if isinstance(stage.get("execution_attempts"), list) and stage.get("execution_attempts")
                else None
            ),
            "latest_stage_result": copy.deepcopy(stage.get("latest_stage_result")),
            "stage_result_binding": copy.deepcopy(stage.get("stage_result_binding")),
            "latest_evidence": None if latest is None else {
                "revision": latest.revision,
                "digest": latest.digest,
                "abstraction_layer": latest.abstraction_layer,
                "user_visible_improvement": latest.user_visible_improvement,
                "architecture": dict(latest.architecture),
            },
            "consultation_count": len(self._events_for("consultation_complete")),
            "consultations": [
                event["receipt"] for event in self._events_for("consultation_complete")
            ],
            "events": copy.deepcopy(self._events),
        }
        validate_against_schema(payload, "stage_integration_receipt")
        return payload

    def _persist_receipt(self) -> None:
        if self._receipt_path is None:
            return
        payload = self._receipt_payload()
        temporary = self._receipt_path.with_name(f".{self._receipt_path.name}.tmp")
        temporary.write_text(canonical_json(payload) + "\n", encoding="utf-8")
        os.replace(temporary, self._receipt_path)

    def _events_for(self, event_type: str) -> list[dict[str, Any]]:
        return [event for event in self._events if event.get("event") == event_type]

    def show_stage(self) -> dict[str, Any]:
        return self.controller.show_stage(self.stage_id)

    def start_stage(self, *, actor: str = "local-human", rationale: str = "") -> dict[str, Any]:
        """Explicit user/harness operation; never called by consultation."""

        result = self.controller.start_stage(self.stage_id, actor=actor, rationale=rationale)
        self._append_event("start_stage", status=result["stage"]["status"])
        return result

    def stop_stage(self, *, actor: str = "local-human", rationale: str = "") -> dict[str, Any]:
        result = self.controller.stop_stage(self.stage_id, actor=actor, rationale=rationale)
        self._append_event("stop_stage", status=result["stage"]["status"])
        return result

    def approve_stage(self, *, actor: str = "local-human", rationale: str = "") -> dict[str, Any]:
        result = self.controller.approve_stage(self.stage_id, actor=actor, rationale=rationale)
        self._append_event("approve_stage", status=result["stage"]["status"])
        return result

    def reject_stage(self, *, actor: str = "local-human", rationale: str = "") -> dict[str, Any]:
        result = self.controller.reject_stage(self.stage_id, actor=actor, rationale=rationale)
        self._append_event("reject_stage", status=result["stage"]["status"])
        return result

    def capture_baseline(
        self,
        *,
        metrics_refs: Sequence[str] | None = None,
        review_refs: Sequence[str] | None = None,
        config_refs: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        baseline = capture_git_baseline(
            self.repository_root,
            stage_id=self.stage_id,
            metrics_refs=metrics_refs,
            review_refs=review_refs,
            config_refs=config_refs,
        )
        verify_git_baseline(baseline)
        self._append_event("git_baseline_captured", digest=baseline["digest"], dirty=baseline["dirty"])
        return baseline

    def build_context(
        self,
        *,
        mode: str = "NORMAL",
        git_baseline: Mapping[str, Any] | None = None,
        latest_result: Any = None,
        artifact_refs: Any = None,
        resources: Mapping[str, Any] | None = None,
        previous_context: Mapping[str, Any] | None = None,
        iteration_index: int | None = None,
    ) -> dict[str, Any]:
        """Build and verify the bounded Step 8 context for a consultation."""

        stage = self.controller.show_stage(self.stage_id)
        context = build_stage_context(
            contract=stage["contract"],
            state=stage,
            mode=mode,
            git_baseline=git_baseline or {},
            latest_result=latest_result if latest_result is not None else (self._evidence.payload if self._evidence else {}),
            artifact_refs=artifact_refs if artifact_refs is not None else [],
            resources=resources,
            previous_context=previous_context,
            plan_id=stage["contract"].get("plan_id", "stage-plan"),
            stage_id=self.stage_id,
            iteration_index=stage.get("iteration_index", 0) if iteration_index is None else iteration_index,
        )
        return verify_stage_context(context)

    @staticmethod
    def write_context(context: Mapping[str, Any], destination: str | os.PathLike[str]) -> dict[str, str]:
        return write_stage_context(verify_stage_context(context), destination)

    @staticmethod
    def verify_artifacts(manifest: Mapping[str, Any]) -> dict[str, Any]:
        return validate_artifact_manifest(manifest, check_files=True)

    def publish_evidence(
        self,
        evidence: Mapping[str, Any],
        *,
        abstraction_layer: str,
        user_visible_improvement: bool | None = None,
    ) -> dict[str, Any]:
        """Publish one new local evidence revision and evaluate architecture."""

        self._guard_active("publish_evidence")
        if not isinstance(evidence, Mapping):
            raise StageIntegrationError("INTEGRATION_INVALID_INPUT", "evidence must be an object")
        layer = _bounded_text(abstraction_layer, "abstraction_layer", maximum=256)
        if user_visible_improvement is not None and not isinstance(user_visible_improvement, bool):
            raise StageIntegrationError("INTEGRATION_INVALID_INPUT", "user_visible_improvement must be boolean or null")
        safe_evidence = _safe_value(dict(evidence), "evidence")
        digest = sha256_json(safe_evidence)
        if self._evidence is not None and digest == self._evidence.digest:
            raise StageIntegrationError("EVIDENCE_NOT_NEW", "evidence digest did not change")
        same_layer = self._evidence is not None and self._evidence.abstraction_layer == layer
        if same_layer and user_visible_improvement is False:
            self._same_abstraction_count += 1
        elif same_layer and user_visible_improvement is True:
            self._same_abstraction_count = 0
        else:
            self._same_abstraction_count = 1 if user_visible_improvement is False else 0
        architecture = architecture_check(
            self._same_abstraction_count,
            user_visible_improvement=user_visible_improvement,
            threshold=self.same_abstraction_threshold,
        )
        self._next_revision += 1
        self._evidence = EvidenceRecord(
            revision=self._next_revision,
            digest=digest,
            abstraction_layer=layer,
            user_visible_improvement=user_visible_improvement,
            architecture=architecture,
            payload=safe_evidence,
        )
        self._append_event(
            "evidence_published",
            revision=self._evidence.revision,
            digest=digest,
            abstraction_layer=layer,
            user_visible_improvement=user_visible_improvement,
            architecture=architecture,
        )
        return {
            "revision": self._evidence.revision,
            "digest": digest,
            "abstraction_layer": layer,
            "architecture": copy.deepcopy(architecture),
        }

    def consult_gpt(
        self,
        prompt: str,
        *,
        mode: str = "NORMAL",
        reason: str = "",
        context_pack: Mapping[str, Any] | None = None,
        continue_from: str | None = None,
        profile_dir: str | os.PathLike[str] | None = None,
        timeout_ms: int = 300_000,
    ) -> ConsultationHandle:
        """Perform exactly one explicit bridge consultation."""

        self._guard_active("consult_gpt")
        if self._pending is not None:
            raise StageIntegrationError("CONSULTATION_PENDING", "the previous GPT response must be read before another consultation")
        if self.bridge_runner is None:
            raise StageIntegrationError("BRIDGE_RUNNER_MISSING", "no bridge runner was configured")
        if self._evidence is None:
            raise StageIntegrationError("EVIDENCE_REQUIRED", "a new local evidence revision is required before consultation")
        if self._evidence.revision <= self._last_consult_revision:
            raise StageIntegrationError("EVIDENCE_REVISION_NOT_NEW", "consultation evidence revision is not newer than the last request")
        mode_value = _bounded_text(mode, "mode", maximum=16).upper()
        if mode_value not in {"NORMAL", "FRESH"}:
            raise StageIntegrationError("INTEGRATION_INVALID_INPUT", "mode must be NORMAL or FRESH")
        if mode_value == "NORMAL" and self._evidence.architecture.get("triggered"):
            raise StageIntegrationError(
                "ARCHITECTURE_REVIEW_REQUIRED",
                "same-abstraction no-improvement threshold requires FRESH, REPLAN, or HUMAN_GATE",
                details={"architecture": dict(self._evidence.architecture)},
            )
        if mode_value == "FRESH" and continue_from is not None:
            raise StageIntegrationError("FRESH_CONTINUATION_FORBIDDEN", "FRESH closeout must use a new conversation")
        digest = self._evidence.digest
        key = (mode_value, digest)
        if key in self._consulted:
            raise StageIntegrationError("DUPLICATE_EVIDENCE_REJECTED", "the same evidence digest cannot be consulted twice in one mode")
        bridge_mode = "fresh" if mode_value == "FRESH" or continue_from is None else "continue"
        try:
            request = self.controller.request_consultation(
                self.stage_id,
                mode=mode_value,
                evidence_digest=digest,
                reason=reason,
                conversation_id=None,
            )
        except (StageControllerError, ContractValidationError) as exc:
            raise StageIntegrationError("CONSULTATION_GATE_REJECTED", str(exc), cause=exc) from exc
        try:
            raw_result = self.bridge_runner(
                prompt,
                mode=bridge_mode,
                continue_from=continue_from,
                context_pack=context_pack,
                root_dir=str(self.repository_root),
                profile_dir=None if profile_dir is None else str(Path(profile_dir).expanduser().resolve()),
                timeout_ms=timeout_ms,
            )
            consultation_id, response_text, request_count, receipt, receipt_path = _normalise_bridge_result(raw_result)
            if int(request_count) != BRIDGE_REQUEST_COUNT:
                raise StageIntegrationError("BRIDGE_REQUEST_BUDGET_INVALID", "bridge request count is not exactly one")
        except StageIntegrationError as exc:
            request_count_hint = exc.details.get("request_count") if isinstance(exc.details, Mapping) else None
            forensic = _forensic_details(
                exc.code,
                type(exc).__name__,
                phase=(
                    exc.details.get("failure_phase")
                    if isinstance(exc.details, Mapping) and isinstance(exc.details.get("failure_phase"), str)
                    else "BRIDGE_SUBPROCESS"
                ),
                request_count=request_count_hint,
            )
            self._append_event(
                "consultation_failed",
                mode=mode_value,
                evidence_revision=self._evidence.revision,
                evidence_digest=digest,
                controller_request_id=request["request"]["request_id"],
                error_code=exc.code,
                error_details=forensic,
            )
            for key, value in forensic.items():
                exc.details.setdefault(key, value)
            exc.details.setdefault("stop_reason", "consultation_failed")
            if self._receipt_path is not None:
                exc.details.setdefault("integration_receipt", str(self._receipt_path))
            raise
        except Exception as exc:  # noqa: BLE001 - external bridge boundary
            forensic = _forensic_details(
                "BRIDGE_EXTERNAL_FAILURE",
                type(exc).__name__,
                phase="BRIDGE_RUNNER",
                request_count=None,
            )
            self._append_event(
                "consultation_failed",
                mode=mode_value,
                evidence_revision=self._evidence.revision,
                evidence_digest=digest,
                controller_request_id=request["request"]["request_id"],
                error_code=forensic["error_code"],
                error_details=forensic,
            )
            wrapped = StageIntegrationError(
                "BRIDGE_EXTERNAL_FAILURE",
                "the headed bridge failed; no retry was attempted",
                details=forensic,
                cause=exc,
            )
            wrapped.details.setdefault("stop_reason", "consultation_failed")
            if self._receipt_path is not None:
                wrapped.details.setdefault("integration_receipt", str(self._receipt_path))
            raise wrapped from exc
        try:
            # A completed bridge envelope is not yet a completed workflow
            # consultation.  The response must carry exactly one known
            # workflow decision before it can enter the consultation-complete
            # event stream.  The response itself stays in memory only; the
            # bounded failure records its length and digest below.
            parse_dialogue_decision(response_text)
        except StageIntegrationError as exc:
            forensic = _forensic_details(
                exc.code,
                type(exc).__name__,
                phase="BRIDGE_RESULT",
                request_count=BRIDGE_REQUEST_COUNT,
            )
            failure_details = {
                **forensic,
                "response_char_count": len(response_text),
                "response_sha256": _response_digest(response_text),
            }
            self._append_event(
                "consultation_failed",
                mode=mode_value,
                evidence_revision=self._evidence.revision,
                evidence_digest=digest,
                controller_request_id=request["request"]["request_id"],
                error_code=exc.code,
                error_details=failure_details,
            )
            for key, value in failure_details.items():
                exc.details.setdefault(key, value)
            exc.details.setdefault("stop_reason", "consultation_failed")
            if self._receipt_path is not None:
                exc.details.setdefault("integration_receipt", str(self._receipt_path))
            raise
        pack_id = None
        if isinstance(context_pack, Mapping):
            candidate = context_pack.get("packet_id", context_pack.get("packetId"))
            if isinstance(candidate, str) and candidate.strip():
                pack_id = candidate.strip()
        self._consulted.add(key)
        self._last_consult_revision = self._evidence.revision
        handle = ConsultationHandle(
            consultation_id=consultation_id,
            mode=mode_value,
            bridge_mode=bridge_mode,
            evidence_revision=self._evidence.revision,
            evidence_digest=digest,
            context_pack_id=pack_id,
            response_text=response_text,
            receipt_path=receipt_path,
            receipt=receipt,
        )
        self._pending = handle
        self._append_event(
            "consultation_complete",
            mode=mode_value,
            bridge_mode=bridge_mode,
            evidence_revision=handle.evidence_revision,
            evidence_digest=handle.evidence_digest,
            consultation_id=handle.consultation_id,
            context_pack_id=pack_id,
            request_count=BRIDGE_REQUEST_COUNT,
            receipt=_summary_receipt(receipt),
        )
        return handle

    def read_response(self, handle: ConsultationHandle | None = None) -> dict[str, Any]:
        """Mark one response as read and return only a bounded response view."""

        if self._pending is None:
            raise StageIntegrationError("NO_PENDING_RESPONSE", "there is no GPT response awaiting a Codex read")
        selected = handle or self._pending
        if selected.consultation_id != self._pending.consultation_id:
            raise StageIntegrationError("RESPONSE_HANDLE_MISMATCH", "response handle does not match the pending consultation")
        decision = parse_dialogue_decision(selected.response_text)
        view = {
            "consultation_id": selected.consultation_id,
            "mode": selected.mode,
            "decision": decision,
            "response_char_count": len(selected.response_text),
            "response_sha256": _response_digest(selected.response_text),
            "evidence_revision": selected.evidence_revision,
            "evidence_digest": selected.evidence_digest,
            "context_pack_id": selected.context_pack_id,
            "receipt_path": selected.receipt_path,
        }
        self._read_responses[selected.consultation_id] = view
        self._append_event(
            "response_read",
            consultation_id=selected.consultation_id,
            decision=decision,
            response_char_count=view["response_char_count"],
            response_sha256=view["response_sha256"],
        )
        return copy.deepcopy(view)

    def apply_read_decision(self, handle: ConsultationHandle | None = None) -> dict[str, Any]:
        """Return the parsed decision; no controller transition is implicit."""

        view = self.read_response(handle)
        return {"decision": view["decision"], "consultation_id": view["consultation_id"]}

    def execute_local_action(
        self,
        action: Callable[[Mapping[str, Any] | None, Mapping[str, Any]], Mapping[str, Any]],
        *,
        objective: str | None = None,
        abstraction_layer: str = "local-action",
        user_visible_improvement: bool | None = None,
        mode: str = "PRIMARY",
        lane: str = "PRIMARY",
    ) -> dict[str, Any]:
        """Run one explicit local action after the required response read."""

        self._guard_active("execute_local_action")
        self._ensure_no_pending_response("execute_local_action")
        self._check_iteration_ceiling()
        response_view = None
        if self._pending is not None:
            response_view = self._read_responses[self._pending.consultation_id]
        try:
            request = self.controller.request_executor(
                self.stage_id,
                objective=objective,
                mode=mode,
                lane=lane,
            )
        except (StageControllerError, ContractValidationError) as exc:
            raise StageIntegrationError("EXECUTOR_GATE_REJECTED", str(exc), cause=exc) from exc
        try:
            raw_result = action(copy.deepcopy(response_view), copy.deepcopy(request["request"]))
        except Exception as exc:  # noqa: BLE001 - local action boundary
            # The adapter/runner failed after a real execution request was
            # issued.  Persist a bounded attempt receipt before surfacing the
            # integration error so the same Stage iteration can retry without
            # losing provenance.  Exception text is intentionally omitted.
            try:
                self.controller.record_executor_failure(
                    {
                        "kind": "ADAPTER_FAILURE",
                        "code": "LOCAL_ACTION_FAILED",
                        "retryable": True,
                    },
                    stage_id=self.stage_id,
                    request_id=request["request"]["request_id"],
                    provider={
                        "adapter": "stage_integration",
                        "runner": "local_action",
                    },
                )
            except (StageControllerError, ContractValidationError):
                # Preserve the original integration failure.  The controller
                # normally accepts this bounded receipt; if a concurrent or
                # already-terminal mutation rejects it, no raw exception is
                # allowed to escape through the receipt surface.
                pass
            self._append_event("local_action_failed", error_class=type(exc).__name__)
            raise StageIntegrationError("LOCAL_ACTION_FAILED", "the local action failed; no GPT retry was attempted", cause=exc) from exc
        if not isinstance(raw_result, Mapping):
            try:
                self.controller.record_executor_failure(
                    {
                        "kind": "CONTRACT_FAILURE",
                        "code": "LOCAL_RESULT_INVALID",
                        "retryable": True,
                    },
                    stage_id=self.stage_id,
                    request_id=request["request"]["request_id"],
                    provider={"adapter": "stage_integration", "runner": "local_action"},
                )
            except (StageControllerError, ContractValidationError):
                pass
            self._append_event("local_action_failed", error_code="LOCAL_RESULT_INVALID")
            raise StageIntegrationError("LOCAL_RESULT_INVALID", "local action must return a bounded result object")
        result = _safe_value(dict(raw_result), "local result")
        if bool(result.get("stage_ready")):
            try:
                self.controller.record_executor_failure(
                    {
                        "kind": "CONTRACT_FAILURE",
                        "code": "STAGE_READY_REQUIRES_EXPLICIT_REVIEW",
                        "retryable": True,
                    },
                    stage_id=self.stage_id,
                    request_id=request["request"]["request_id"],
                    provider={"adapter": "stage_integration", "runner": "local_action"},
                )
            except (StageControllerError, ContractValidationError):
                pass
            self._append_event(
                "local_action_failed",
                error_code="STAGE_READY_REQUIRES_EXPLICIT_REVIEW",
            )
            raise StageIntegrationError(
                "STAGE_READY_REQUIRES_EXPLICIT_REVIEW",
                "a local action cannot auto-promote a Stage; call mark_stage_ready after closeout review",
            )
        try:
            recorded = self.controller.record_executor_result(
                result,
                stage_id=self.stage_id,
                request_id=request["request"]["request_id"],
            )
        except (StageControllerError, ContractValidationError) as exc:
            raise StageIntegrationError("LOCAL_RESULT_REJECTED", str(exc), cause=exc) from exc
        status = str(recorded["stage"]["status"])
        if status != StageState.ACTIVE.value:
            self._append_event("local_result_recorded", status=status, decision=recorded.get("decision"))
            self._pending = None
            return {"controller": recorded, "evidence": None}
        payload = result.get("evidence")
        if not isinstance(payload, Mapping):
            payload = {"result": result, "request_id": request["request"]["request_id"]}
        evidence = self.publish_evidence(
            payload,
            abstraction_layer=abstraction_layer,
            user_visible_improvement=user_visible_improvement,
        )
        self._append_event(
            "local_action_complete",
            request_id=request["request"]["request_id"],
            evidence_revision=evidence["revision"],
            evidence_digest=evidence["digest"],
        )
        self._pending = None
        return {"controller": recorded, "evidence": evidence}

    def mark_stage_ready(
        self,
        *,
        required_checks: Any,
        review_artifacts: Any,
        result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Promote only after an explicitly read FRESH closeout response."""

        self._guard_active("mark_stage_ready")
        self._ensure_no_pending_response("mark_stage_ready")
        if not any(view.get("mode") == "FRESH" for view in self._read_responses.values()):
            raise StageIntegrationError("FRESH_CLOSEOUT_REQUIRED", "STAGE_READY requires an explicitly read FRESH closeout")
        if self._evidence is None:
            raise StageIntegrationError("EVIDENCE_REQUIRED", "STAGE_READY requires a local evidence revision")
        candidate = dict(result or self._evidence.payload)
        candidate.setdefault("status", "SUCCEEDED")
        candidate.setdefault("baseline_digest", self.controller.show_stage(self.stage_id)["baseline_digest"])
        candidate.setdefault("stage_id", self.stage_id)
        candidate["stage_ready"] = True
        try:
            ready = self.controller.mark_stage_ready(
                candidate,
                stage_id=self.stage_id,
                required_checks=required_checks,
                review_artifacts=review_artifacts,
            )
        except (StageControllerError, ContractValidationError) as exc:
            raise StageIntegrationError("STAGE_READY_EVIDENCE_REJECTED", str(exc), cause=exc) from exc
        self._append_event("stage_ready", status=ready["stage"]["status"])
        return ready

    def request_human_gate(self, rationale: str) -> dict[str, Any]:
        self._guard_active("request_human_gate")
        result = self.controller.apply_decision("HUMAN_GATE", stage_id=self.stage_id, rationale=rationale)
        self._append_event("human_gate_requested", rationale=_bounded_text(rationale, "rationale", required=False))
        return result


def _parse_keyed_stdout(stdout: str, key: str) -> str | None:
    prefix = f"{key}="
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return None


def _parse_pre_prompt_recovery_marker(*outputs: str) -> dict[str, Any] | None:
    """Parse the bridge's bounded recovery metadata without trusting logs."""

    raw = next((value for output in outputs if (value := _parse_keyed_stdout(output, "pre_prompt_recovery")) is not None), None)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StageIntegrationError("BRIDGE_RESULT_INVALID", "pre-prompt recovery metadata is not valid JSON", cause=exc) from exc
    if not isinstance(value, Mapping):
        raise StageIntegrationError("BRIDGE_RESULT_INVALID", "pre-prompt recovery metadata must be an object")
    cycles = value.get("cycles")
    max_cycles = value.get("max_cycles")
    failure_codes = value.get("failure_codes")
    if (
        value.get("attempted") is not True
        or isinstance(cycles, bool)
        or not isinstance(cycles, int)
        or cycles < 1
        or cycles > 2
        or max_cycles != 2
        or not isinstance(failure_codes, list)
        or len(failure_codes) != cycles
        or not all(isinstance(code, str) and _FAILURE_CODE_PATTERN.fullmatch(code) for code in failure_codes)
    ):
        raise StageIntegrationError("BRIDGE_RESULT_INVALID", "pre-prompt recovery metadata is outside the bounded contract")
    return {
        "attempted": True,
        "cycles": cycles,
        "max_cycles": 2,
        "failure_codes": list(failure_codes),
    }


def _normalise_bridge_failure_code(value: Any, *, default: str | None = "BRIDGE_EXTERNAL_FAILURE") -> str | None:
    """Return one bounded, non-sensitive bridge failure code.

    The bridge owns the failure-code vocabulary, but the subprocess boundary
    must not trust arbitrary stderr text (or a forged receipt) as a Python
    exception code.  Codes are deliberately restricted to one identifier-like
    token, upper-cased, and clipped before they can reach an error or receipt.
    """

    if not isinstance(value, str):
        return default
    candidate = value.strip()
    if not candidate or any(character.isspace() for character in candidate):
        return default
    candidate = candidate[:_MAX_BRIDGE_FAILURE_CODE]
    if not _FAILURE_CODE_PATTERN.fullmatch(candidate):
        return default
    # A marker carrying a field name/value instead of a bridge code must not
    # turn that value into an exception code (for example ``token=...``).
    if candidate.casefold() in {
        "prompt",
        "path",
        "spec",
        "token",
        "tokens",
        "cookie",
        "cookies",
        "dom",
        "raw_dom",
        "stderr",
        "stdout",
        "status",
    }:
        return default
    return candidate.upper()


def _parse_bridge_failure_marker(output: str) -> str | None:
    """Parse either bridge failure-marker spelling without accepting logs."""

    if not isinstance(output, str):
        return None
    prefix = _BRIDGE_FAILURE_MARKER
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped.startswith(prefix):
            continue
        suffix = stripped[len(prefix):]
        if suffix.startswith("="):
            candidate = suffix[1:].strip()
        elif suffix and suffix[0].isspace():
            candidate = suffix.strip()
        else:
            # A prefix embedded in an unrelated log line is not a marker.
            continue
        # The Node CLI emits exactly one code token.  Reject additional text
        # so an arbitrary stderr sentence cannot be smuggled into a code.
        if not candidate or any(character.isspace() for character in candidate):
            continue
        return _normalise_bridge_failure_code(candidate, default=None)
    return None


def _parse_request_count_marker(*outputs: str) -> int | None:
    """Read a bounded request-count marker from stdout/stderr."""

    for output in outputs:
        for key in ("request_count", "requestCount"):
            raw_value = _parse_keyed_stdout(output, key)
            if raw_value is None:
                continue
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                continue
            if value in {0, BRIDGE_REQUEST_COUNT}:
                return value
    return None


def _normalise_bridge_status(value: Any) -> str | None:
    """Return a bounded status token, or ``None`` for arbitrary log text."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > _MAX_BRIDGE_FAILURE_CODE:
        return None
    if not _FAILURE_CODE_PATTERN.fullmatch(candidate):
        return None
    return candidate


def _parse_status_marker(*outputs: str) -> str | None:
    for output in outputs:
        raw_value = _parse_keyed_stdout(output, "status")
        status = _normalise_bridge_status(raw_value)
        if status is not None:
            return status
    return None


def _safe_stderr_summary(stderr: str, failure_code: str) -> str:
    """Create a tiny diagnostic summary without retaining stderr content.

    The raw stream is intentionally never copied into a result.  Lines that
    can contain prompts, paths, credentials, DOM dumps, or transport specs are
    discarded; the remaining diagnostic text is reduced to a conservative
    ASCII character set and bounded to a single short field.
    """

    safe_parts: list[str] = []
    for raw_line in stderr.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        if line.startswith(_BRIDGE_FAILURE_MARKER):
            # Re-render the marker from the already validated code rather than
            # copying the original line (which may contain an injected suffix).
            safe_parts.append(f"failure_marker={failure_code}")
            continue
        if line.casefold().startswith("receipt="):
            # Receipt values are filesystem paths and are not diagnostic text.
            continue
        if (
            _STDERR_SENSITIVE_LINE_PATTERN.search(line)
            or any(character in line for character in ("/", "\\"))
            or _STDERR_LONG_TOKEN_PATTERN.search(line)
            or any(pattern.search(line) for pattern in _SECRET_PATTERNS)
        ):
            continue
        sanitized = _STDERR_SAFE_CHAR_PATTERN.sub(" ", line)
        sanitized = " ".join(sanitized.split())
        if sanitized:
            safe_parts.append(sanitized)
        if len(safe_parts) >= 2:
            break
    summary = "; ".join(safe_parts)
    if not summary:
        summary = f"failure_marker={failure_code}"
    return summary[:_MAX_STDERR_SUMMARY]


def _extract_response_markers(stdout: str) -> str:
    start = "CHATGPT_RESPONSE_BEGIN"
    end = "CHATGPT_RESPONSE_END"
    if stdout.count(start) != 1 or stdout.count(end) != 1:
        return ""
    start_index = stdout.index(start) + len(start)
    end_index = stdout.index(end)
    if end_index < start_index:
        return ""
    body = stdout[start_index:end_index]
    return body.strip("\r\n")


def _extract_json_envelope(stdout: str) -> Mapping[str, Any] | None:
    """Find a bridge JSON envelope without treating logs as a response."""

    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate or not candidate.startswith("{") or not candidate.endswith("}"):
            continue
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if is_bridge_envelope(value):
            return value
    return None


def _unlink_transient(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # A failed cleanup is not allowed to make a completed bridge request
        # look successful; the caller can inspect the bounded receipt and
        # report this as a local hygiene failure.
        raise StageIntegrationError("INTEGRATION_CLEANUP_FAILED", f"could not remove transient file {path.name}")


def _load_receipt(path: Path) -> dict[str, Any]:
    """Load one bridge receipt with a stable fail-closed error."""

    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StageIntegrationError("BRIDGE_RECEIPT_INVALID", "bridge receipt could not be read as JSON", cause=exc) from exc
    if not isinstance(parsed, Mapping):
        raise StageIntegrationError("BRIDGE_RECEIPT_INVALID", "bridge receipt must be an object")
    return dict(parsed)


def subprocess_bridge_runner(
    prompt: str,
    *,
    mode: str,
    continue_from: str | None,
    context_pack: Mapping[str, Any] | None,
    root_dir: str,
    profile_dir: str | None,
    timeout_ms: int = 300_000,
    node_executable: str = "node",
    bridge_root: str | os.PathLike[str] | None = None,
    project_url: str | None = None,
    project_id: str | None = None,
    transport: str | None = None,
    consultation_intent_key: str | None = None,
    recover_consultation_id: str | None = None,
) -> dict[str, Any]:
    """Invoke the existing one-request Node bridge for disposable E2E.

    ``context_pack`` must be a JSON-serialisable bridge-pack specification
    accepted by ``scripts/consult-pack.mjs``.  Any temporary spec is removed
    immediately.  The bridge's request/response files are removed after the
    bounded stdout/receipt metadata is read; the receipt itself is retained.
    """

    prompt = _bounded_text(prompt, "prompt", maximum=100_000)
    root = Path(root_dir).expanduser().resolve()
    script_root = Path(bridge_root or Path(__file__).resolve().parents[2] / "chatgpt_browser_bridge").resolve()
    script = script_root / "scripts" / "consult-pack.mjs"
    if not script.is_file():
        raise StageIntegrationError("BRIDGE_SCRIPT_MISSING", f"consult-pack script not found: {script}")
    if not isinstance(context_pack, Mapping):
        raise StageIntegrationError("CONTEXT_PACK_SPEC_REQUIRED", "real integration requires a context-pack spec")
    mode_value = _bounded_text(mode, "bridge mode", maximum=16).lower()
    if mode_value not in {"fresh", "continue"}:
        raise StageIntegrationError("INTEGRATION_INVALID_INPUT", "bridge mode must be fresh or continue")
    normalized_project_url: str | None = None
    if project_url is not None:
        try:
            normalized_project_url = normalize_bridge_project_url(project_url)
        except BridgeEnvelopeError as exc:
            raise StageIntegrationError(exc.code, str(exc), details=exc.details) from exc
    spec = {
        "root_dir": str(root),
        "question": prompt,
        "conversation_mode": mode_value,
        "pack": dict(context_pack),
    }
    if consultation_intent_key is not None:
        spec["consultation_intent_key"] = _bounded_text(consultation_intent_key, "consultation_intent_key", maximum=128)
    if recover_consultation_id is not None:
        spec["recover_consultation_id"] = _bounded_text(recover_consultation_id, "recover_consultation_id", maximum=128)
    if transport is not None:
        if transport != "homepage_fallback" or project_url is not None:
            raise StageIntegrationError("TRANSPORT_INVALID", "homepage fallback must not claim Project UI scope")
        spec["transport"] = transport
    if normalized_project_url is not None:
        # The Node CLI accepts this value either as an option or in the spec;
        # keep it in the spec so a single argv shape works across bridge
        # revisions while the bridge remains the owner of URL validation.
        spec["project_url"] = normalized_project_url
    effective_project_id = project_id
    if effective_project_id is None and isinstance(context_pack, Mapping):
        for key in ("PROJECT_ID", "project_id"):
            value = context_pack.get(key)
            if isinstance(value, str) and value.strip():
                effective_project_id = value.strip()
                break
    if effective_project_id is not None:
        spec["project_id"] = _bounded_text(effective_project_id, "project_id", maximum=128)
    if continue_from is not None:
        spec["continue_from"] = _bounded_text(continue_from, "continue_from", maximum=256)
    temporary_spec: Path | None = None
    stdout = ""
    stderr = ""
    receipt_path: Path | None = None
    response_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix=".stage9-request-",
            dir=str(root),
            delete=False,
        ) as handle:
            temporary_spec = Path(handle.name)
            json.dump(spec, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        command = [node_executable, str(script), "--spec", str(temporary_spec), "--timeout-ms", str(timeout_ms)]
        if profile_dir:
            command.extend(["--profile-dir", str(Path(profile_dir).expanduser().resolve())])
        try:
            child_environment = os.environ.copy()
            # The pack entrypoint materializes every upload below this
            # canonical staging boundary. Keep the fallback environment root
            # narrow; never pass the whole repository or a profile/auth
            # directory to the bridge.
            child_environment["CHATGPT_ALLOWED_ATTACHMENT_ROOTS"] = str(
                root / ".consultations" / "staging"
            )
            completed = subprocess.run(
                command,
                cwd=str(script_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(330, int(timeout_ms / 1000) + 30),
                check=False,
                env=child_environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise StageIntegrationError(
                "BRIDGE_TIMEOUT",
                "headed bridge exceeded the five-minute safety bound",
                details=_forensic_details(
                    "BRIDGE_TIMEOUT",
                    type(exc).__name__,
                    phase="BRIDGE_SUBPROCESS",
                    request_count=None,
                ),
                cause=exc,
            ) from exc
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        envelope = _extract_json_envelope(stdout)
        receipt_value = (
            _parse_keyed_stdout(stdout, "receipt")
            or _parse_keyed_stdout(stderr, "receipt")
            or (envelope.get("receiptPath") if isinstance(envelope, Mapping) else None)
            or (envelope.get("receipt_path") if isinstance(envelope, Mapping) else None)
        )
        if receipt_value is not None and not isinstance(receipt_value, (str, os.PathLike)):
            raise StageIntegrationError("BRIDGE_RECEIPT_INVALID", "bridge receipt path must be text")
        if receipt_value:
            receipt_candidate = Path(receipt_value).expanduser()
            if not receipt_candidate.is_absolute():
                receipt_candidate = root / receipt_candidate
            receipt_path = receipt_candidate.resolve()
        if receipt_path is not None:
            try:
                receipt_path.relative_to(root)
            except ValueError as exc:
                raise StageIntegrationError("BRIDGE_RECEIPT_INVALID", "bridge receipt escaped the project root", cause=exc) from exc
        if completed.returncode != 0:
            marker_code = _parse_bridge_failure_marker(stderr) or _parse_bridge_failure_marker(stdout)
            recovery_metadata = _parse_pre_prompt_recovery_marker(stdout, stderr)
            failure_receipt: dict[str, Any] = {}
            if receipt_path is not None and receipt_path.is_file():
                try:
                    parsed = json.loads(receipt_path.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict):
                        failure_receipt = parsed
                except (OSError, UnicodeError, json.JSONDecodeError):
                    failure_receipt = {}
            receipt_code = _normalise_bridge_failure_code(
                failure_receipt.get("failure_code"),
                default=None,
            )
            code = receipt_code or marker_code or "BRIDGE_EXTERNAL_FAILURE"
            receipt_request_count = failure_receipt.get(
                "request_count",
                failure_receipt.get("requestCount"),
            )
            if (
                isinstance(receipt_request_count, bool)
                or not isinstance(receipt_request_count, int)
                or receipt_request_count not in {0, BRIDGE_REQUEST_COUNT}
            ):
                receipt_request_count = None
            request_count = (
                receipt_request_count
                if receipt_request_count is not None
                else _parse_request_count_marker(stderr, stdout)
            )
            status = _normalise_bridge_status(failure_receipt.get("status"))
            if status is None and isinstance(envelope, Mapping):
                status = _normalise_bridge_status(envelope.get("status"))
            if status is None:
                status = _parse_status_marker(stderr, stdout)
            forensic = _forensic_details(
                code,
                "BridgeProcessFailure",
                phase="BRIDGE_SUBPROCESS",
                request_count=request_count,
            )
            failure_details = {
                "returncode": completed.returncode,
                "receipt_path": str(receipt_path) if receipt_path else None,
                "bridge_failure_code": code,
                "request_count": request_count,
                "status": status,
                "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
                "stderr_summary": _safe_stderr_summary(stderr, code),
                **forensic,
            }
            if recovery_metadata is not None:
                failure_details["pre_prompt_recovery"] = recovery_metadata
            raise StageIntegrationError(
                code,
                f"headed bridge failed ({code}); no retry was attempted",
                details=failure_details,
            )
        consultation_id = (
            _parse_keyed_stdout(stdout, "consultation_id")
            or _parse_keyed_stdout(stdout, "consultationId")
            or (envelope.get("consultationId") if isinstance(envelope, Mapping) else None)
            or (envelope.get("consultation_id") if isinstance(envelope, Mapping) else None)
        )
        response_text = _extract_response_markers(stdout)
        envelope_response = None
        if isinstance(envelope, Mapping):
            candidate_response = envelope.get("responseText", envelope.get("response_text", envelope.get("response")))
            if isinstance(candidate_response, str):
                envelope_response = candidate_response
        if response_text and envelope_response is not None and response_text != envelope_response.strip("\r\n"):
            raise StageIntegrationError("BRIDGE_RESULT_INVALID", "bridge envelope and response markers disagree")
        if not response_text and envelope_response is not None:
            response_text = envelope_response
        if not response_text and receipt_path is not None:
            response_path = receipt_path.parent / "response.txt"
            if response_path.is_file():
                response_text = response_path.read_text(encoding="utf-8")
        if not consultation_id or not response_text:
            raise StageIntegrationError("BRIDGE_RESULT_INVALID", "bridge output lacked bounded consultation metadata")
        receipt = {}
        if receipt_path is not None and receipt_path.is_file():
            receipt = _load_receipt(receipt_path)
        elif isinstance(envelope, Mapping) and isinstance(envelope.get("receipt"), Mapping):
            receipt = dict(envelope["receipt"])
        request_value = (
            _parse_keyed_stdout(stdout, "request_count")
            or _parse_keyed_stdout(stdout, "requestCount")
            or (envelope.get("requestCount") if isinstance(envelope, Mapping) else None)
            or (envelope.get("request_count") if isinstance(envelope, Mapping) else None)
        )
        try:
            request_count = int(request_value or 0)
        except (TypeError, ValueError) as exc:
            raise StageIntegrationError("BRIDGE_RESULT_INVALID", "bridge request count is not an integer", cause=exc) from exc
        result = {
            "consultation_id": consultation_id,
            "request_count": request_count,
            "response_text": response_text,
            "receipt_path": str(receipt_path) if receipt_path else None,
            "receipt": receipt,
        }
        recovery_metadata = _parse_pre_prompt_recovery_marker(stdout, stderr)
        if recovery_metadata is not None:
            result["pre_prompt_recovery"] = recovery_metadata
        output_project_url = None
        if isinstance(envelope, Mapping):
            output_project_url = envelope.get("projectUrl", envelope.get("project_url"))
        if isinstance(output_project_url, str) and output_project_url.strip():
            result["project_url"] = output_project_url
        # Validate the complete envelope at the Python boundary as well.  This
        # catches missing response/receipt/request metadata before Step 13/14
        # structured parsers see it.
        try:
            checked = normalize_bridge_envelope(
                result,
                expected_project_url=normalized_project_url,
                expected_mode=mode_value,
                require_receipt=normalized_project_url is not None,
            )
        except BridgeEnvelopeError as exc:
            raise StageIntegrationError(exc.code, str(exc), details=exc.details) from exc
        return checked
    finally:
        _unlink_transient(temporary_spec)
        # request.txt contains the actual prompt and response.txt contains the
        # full reply.  Keep only the bridge-created receipt for Step 9 audit.
        if receipt_path is not None:
            _unlink_transient(receipt_path.parent / "request.txt")
            _unlink_transient(response_path or receipt_path.parent / "response.txt")


__all__ = [
    "BRIDGE_REQUEST_COUNT",
    "DEFAULT_EMERGENCY_ITERATION_CEILING",
    "DEFAULT_SAME_ABSTRACTION_THRESHOLD",
    "DECISIONS",
    "EvidenceRecord",
    "INTEGRATION_BLOCKED_MARKER",
    "INTEGRATION_MARKER",
    "ConsultationHandle",
    "StageIntegrationAdapter",
    "StageIntegrationError",
    "parse_dialogue_decision",
    "subprocess_bridge_runner",
]
