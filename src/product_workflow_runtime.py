"""Explicit Product composition for the frozen V2 controller.

This adapter has no lifecycle reducer/checkpoint. Intake uses the existing
brief service; runtime state and commands belong solely to StageController.
Planning receipts are immutable evidence, never an alternate Stage state.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import copy
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Mapping

from .bridge_adapter import BridgeEnvelopeError, normalize_bridge_envelope
from .artifact_resolver import ArtifactResolver, ArtifactResolverError
from .contract_handshake import (
    STAGE_PROPAGATION_INVARIANT,
    payload_snapshot,
    stage_digest,
    stage_identity_digest,
)
from .contracts import ContractValidationError, canonical_json, sha256_json
from .executor import ExecutionRequest, ExecutionResult
from .openai_codex_executor import OpenAICodexExecutor
from .project_intake import ProjectIntakeError, ProjectRequirementsIntake
from .runtime_composition import RuntimeCompositionConfig, load_runtime_composition_config
from .stage_integration import StageIntegrationError, parse_dialogue_decision, subprocess_bridge_runner
from .workflow_runtime import WorkflowRuntimeError
from .human_summary import build_human_presentation
from .workflow_v2_contracts import (
    PUBLIC_COMMANDS,
    assess_observation,
    classify_blocker,
    validate_stage,
)
from .workflow_v2_controller import (
    StageController,
    WorkflowV2ControllerError,
    build_provider_handoff_manifest,
    continue_iteration_change_digest,
)


STAGE_SCOPED_COMMANDS = frozenset({
    "START", "START_STAGE", "REQUEST_EXECUTION", "RECORD_OBSERVATION",
    "RESOLVE_LEGACY_ORPHAN",
    "SUPERSEDE_ASSESSMENT",
    "OBSERVE_RESULT", "ASSESS_RESULT", "APPLY_GPT_DECISION", "ADVANCE_ITERATION",
    "REQUEST_DECISION", "APPLY_DECISION", "RESOLVE_BLOCKER", "APPLY_RECEIPT",
    "COMMIT_INTEGRATION", "CLOSEOUT", "CLOSE_STAGE", "STOP", "ADD_DEPENDENCY",
    "SATISFY_DEPENDENCY", "MIGRATE_BUDGET",
})
_COMMAND_WIRE_NAMES = {
    "START_STAGE": "START",
    "OBSERVE_RESULT": "RECORD_OBSERVATION",
    "CLOSE_STAGE": "CLOSEOUT",
}


def _boundary(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (ProjectIntakeError, BridgeEnvelopeError, StageIntegrationError, ArtifactResolverError) as exc:
            raise WorkflowRuntimeError(exc.code, str(exc), details=getattr(exc, "details", None)) from exc
        except (ContractValidationError, WorkflowV2ControllerError) as exc:
            raise WorkflowRuntimeError("V2_CONTRACT_REJECTED", str(exc)[:512]) from exc
    return wrapped


def _seal(value: Mapping[str, Any]) -> dict[str, Any]:
    return {**value, "record_digest": sha256_json(dict(value))}


def _read_receipt(path: Path, digest: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        claimed = value.pop("record_digest")
        if claimed != sha256_json(value) or value.get("input_digest") != digest:
            raise ValueError("binding mismatch")
        if value.get("request_count") != 1 or value.get("conversation_validated") is not True:
            raise ValueError("invalid transport proof")
        for field in ("consultation_id", "conversation_id", "response_digest", "packet_digest"):
            if not isinstance(value.get(field), str) or not value[field]:
                raise ValueError("incomplete provenance")
        return {**value, "record_digest": claimed}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WorkflowRuntimeError("PLANNING_INPUT_CHANGED", "persisted consultation evidence is invalid or binds different input") from exc


def _inside(root: Path, path: str) -> Path:
    value = (root / path).resolve()
    try:
        value.relative_to(root)
    except ValueError as exc:
        raise WorkflowRuntimeError("PATH_OUTSIDE_WORKSPACE", "product artifact must remain inside workspace") from exc
    return value


def _journal_path(root: Path, config: RuntimeCompositionConfig) -> Path:
    path = config.stage_state_path
    if path == ".research/stage-state.json":
        path = ".workflow-v2/journal.json"
    return _inside(root, path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _controller(root: Path, config: RuntimeCompositionConfig) -> StageController:
    path = _journal_path(root, config)
    if path.is_file():
        return StageController.from_state(path)
    return StageController(workspace_id="workspace-" + hashlib.sha256(str(root).encode()).hexdigest()[:24], state_path=path)


def _immutable(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wire = canonical_json(dict(value)) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != wire:
            raise WorkflowRuntimeError("EVIDENCE_CONFLICT", "immutable product evidence already exists with different content")
        return
    with path.open("x", encoding="utf-8") as handle:
        handle.write(wire)
        handle.flush()
        os.fsync(handle.fileno())


def _validate_transport_recovery(root: Path, recovery: Any, *, input_digest: str,
                                 stage_id: str, planning_revision: int) -> dict[str, Any]:
    """Validate one pre-prompt transport recovery without changing semantics.

    A failed bridge upload with ``request_count=0`` has no GPT effect, but the
    immutable planning intent must not be discarded.  This explicit receipt
    binding permits one new transport identity for that same intent only after
    the caller proves the old attempt failed before prompt delivery.
    """
    if not isinstance(recovery, Mapping):
        raise WorkflowRuntimeError("TRANSPORT_RECOVERY_INVALID", "transport_recovery must be an object")
    if recovery.get("planning_revision") != planning_revision or recovery.get("stage_id") != stage_id:
        raise WorkflowRuntimeError("TRANSPORT_RECOVERY_INVALID", "transport recovery does not bind the current planning intent")
    receipt_value = recovery.get("receipt_path")
    if not isinstance(receipt_value, str) or not receipt_value.strip():
        raise WorkflowRuntimeError("TRANSPORT_RECOVERY_INVALID", "transport recovery requires a receipt path")
    receipt_path = _inside(root, receipt_value)
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowRuntimeError("TRANSPORT_RECOVERY_INVALID", "transport recovery receipt is unreadable") from exc
    if not isinstance(receipt, Mapping):
        raise WorkflowRuntimeError("TRANSPORT_RECOVERY_INVALID", "transport recovery receipt is not an object")
    if receipt.get("status") != "failed_before_prompt" or receipt.get("request_count") != 0:
        raise WorkflowRuntimeError("TRANSPORT_RECOVERY_INVALID", "transport recovery requires a pre-prompt failed receipt")
    if receipt.get("failure_code") != "ATTACHMENT_NOT_READY":
        raise WorkflowRuntimeError("TRANSPORT_RECOVERY_INVALID", "transport recovery only permits attachment readiness failures")
    if receipt.get("conversation_id") is not None or receipt.get("conversation_validated") is not False:
        raise WorkflowRuntimeError("TRANSPORT_RECOVERY_INVALID", "transport recovery receipt has conversation effects")
    packet = receipt.get("context_pack")
    if not isinstance(packet, Mapping) or not isinstance(packet.get("packet_id"), str) or not packet.get("pack_sha256"):
        raise WorkflowRuntimeError("TRANSPORT_RECOVERY_INVALID", "transport recovery receipt lacks packet provenance")
    attachments = receipt.get("attachments")
    if not isinstance(attachments, list) or not attachments or not all(
        isinstance(item, Mapping) and item.get("upload_status") == "failed" for item in attachments
    ):
        raise WorkflowRuntimeError("TRANSPORT_RECOVERY_INVALID", "transport recovery receipt lacks failed attachment evidence")
    return {
        "consultation_id": receipt.get("consultation_id"),
        "receipt_path": str(receipt_path.relative_to(root)).replace("\\", "/"),
        "request_count": 0,
        "failure_code": receipt.get("failure_code"),
        "packet_id": packet.get("packet_id"),
        "pack_sha256": packet.get("pack_sha256"),
        "input_digest": input_digest,
        "planning_revision": planning_revision,
    }


def doctor_product_runtime(workspace: str | Path, config_path: str | Path | None = None,
                           *, config: RuntimeCompositionConfig | None = None) -> dict[str, Any]:
    """Discover machine dependencies without creating any workflow state."""
    root = Path(workspace).expanduser().resolve(strict=True)
    cfg = config or load_runtime_composition_config(root, config_path=config_path)
    missing = []
    def need(ok: bool, component: str, code: str) -> None:
        if not ok:
            missing.append({"component": component, "code": code})
    need(cfg.lifecycle_version == "v2", "engine.lifecycle", "EXPLICIT_V2_CONFIG_REQUIRED")
    need(cfg.stage_contract_path is None, "stage.contract", "LEGACY_CONTRACT_NOT_ALLOWED")
    need(cfg.bridge_enabled, "gpt.consultant", "BRIDGE_CONFIGURATION_REQUIRED")
    bridge = Path(cfg.bridge_root).expanduser() if cfg.bridge_root else None
    need(bridge is not None and (bridge / "scripts/consult-pack.mjs").is_file(), "bridge.root", "BRIDGE_SCRIPT_MISSING")
    need(shutil.which(cfg.node_executable) is not None or Path(cfg.node_executable).is_file(), "bridge.node", "NODE_NOT_FOUND")
    profile = Path(cfg.bridge_profile_dir).expanduser() if cfg.bridge_profile_dir else None
    need(profile is not None and profile.is_dir(), "bridge.profile", "BROWSER_PROFILE_REQUIRED")
    need((cfg.bridge_transport == "homepage_fallback" and cfg.project_url is None)
         or (cfg.bridge_transport is None and bool(cfg.project_url)), "bridge.scope", "EXPLICIT_TRANSPORT_SCOPE_REQUIRED")
    provider = OpenAICodexExecutor(codex_executable=cfg.codex_executable,
                                  preferred_model=cfg.preferred_model, fallback_model=cfg.fallback_model)
    runtime = provider.discover_runtime()
    need(runtime.available, "executor.runtime", "CODEX_RUNTIME_UNAVAILABLE")
    need(runtime.authenticated and runtime.auth_mode == "chatgpt", "executor.auth", "AUTHENTICATION_REQUIRED")
    need(any(item.get("id") == "gpt-5.6-luna" for item in runtime.models), "executor.model", "STANDARD_MODEL_UNAVAILABLE")
    need(cfg.fallback_model is None, "executor.routing", "MODEL_FALLBACK_NOT_ALLOWED")
    # Check a present journal, but never require prior Stage state.
    controller = _controller(root, cfg)
    return {"schema_version": "product_runner_diagnostic.v1", "ready": not missing,
            "missing": missing, "lifecycle_version": "v2", "workspace_root": str(root),
            "journal_exists": _journal_path(root, cfg).is_file(), "stage_count": len(controller.state["stages"]),
            "provider": {"available": runtime.available, "auth_mode": runtime.auth_mode,
                         "authenticated": runtime.authenticated, "version": runtime.version,
                         "standard_model_available": any(item.get("id") == "gpt-5.6-luna" for item in runtime.models)},
            "browser_auth": "NOT_VERIFIED_UNTIL_REAL_CONSULTATION"}


def initialize_product_runtime(workspace: str | Path, config_path: str | Path | None = None) -> dict[str, Any]:
    root = Path(workspace).expanduser().resolve(strict=True)
    cfg = load_runtime_composition_config(root, config_path=config_path)
    result = doctor_product_runtime(root, config=cfg)
    if not result["ready"]:
        raise WorkflowRuntimeError("RUNNER_NOT_CONFIGURED", "Product machine dependencies are not ready", details=result)
    controller = _controller(root, cfg)
    controller.initialize()
    return {**result, "initialized": True, "canonical": controller.resume_projection(),
            "stage_created": False, "stage_started": False}


class ProductWorkflowRuntime:
    """MCP read/submit adapter; no legacy Bootstrap or Core V1 state routing."""
    lifecycle_version = "v2"
    supervisor_step_limit = 8

    def __init__(self, workspace: str | Path, *, config: RuntimeCompositionConfig,
                 maintenance_capability: Any | None = None) -> None:
        self.root = Path(workspace).expanduser().resolve(strict=True)
        self.config = config
        self.intake = ProjectRequirementsIntake(self.root)
        self.controller = _controller(self.root, config)
        self.maintenance_capability = maintenance_capability
        self._transport_request_snapshot: dict[str, Any] | None = None
        self._transport_request_pending = False
        self._last_contract_trace: dict[str, Any] | None = None

    @property
    def last_contract_trace(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._last_contract_trace)

    def note_transport_request(self, request: Mapping[str, Any]) -> None:
        """Record the request as received at the MCP transport boundary."""

        self._transport_request_snapshot = payload_snapshot(request)
        self._transport_request_pending = True

    def repair_context(self) -> dict[str, Any]:
        """Capture bounded business identity before a Workflow-only repair."""

        brief = self.intake.state
        projection = self.controller.resume_projection()
        stage = projection.get("stage") if isinstance(projection, Mapping) else None
        stage = stage if isinstance(stage, Mapping) else {}
        return {
            "workspace_root": str(self.root),
            "project_id": brief.get("project_id") if isinstance(brief, Mapping) else None,
            "stage_id": stage.get("stage_id") if isinstance(stage, Mapping) else None,
            "objective_fingerprint": stage.get("objective_fingerprint"),
            "baseline_digest": stage.get("baseline_digest"),
            "stage_identity_digest": stage_identity_digest(stage) if stage else None,
            "revision": projection.get("revision") if isinstance(projection, Mapping) else None,
            "status": stage.get("status"),
            "next_action": projection.get("next_action") if isinstance(projection, Mapping) else None,
            "target_identity": stage.get("target_identity") if isinstance(stage, Mapping) else None,
        }

    def _stage_failure(self, code: str, message: str, *, request: Mapping[str, Any], action: str,
                       canonical_stage: Mapping[str, Any] | None = None) -> WorkflowRuntimeError:
        trace = self._build_contract_trace(
            action=action,
            request=request,
            canonical_stage=canonical_stage,
            supervisor_request=request,
        )
        self._last_contract_trace = trace
        return WorkflowRuntimeError(
            code,
            message,
            details={
                "failure_classification": code,
                "contract_propagation_invariant": STAGE_PROPAGATION_INVARIANT,
                "contract_trace": trace,
            },
        )

    def _build_contract_trace(self, *, action: str, request: Mapping[str, Any],
                              canonical_stage: Mapping[str, Any] | None,
                              supervisor_request: Mapping[str, Any]) -> dict[str, Any]:
        canonical = payload_snapshot(canonical_stage) if isinstance(canonical_stage, Mapping) else None
        return {
            "action": action,
            "invariant": STAGE_PROPAGATION_INVARIANT,
            "canonical_stage": canonical,
            "canonical_stage_digest": stage_digest(canonical_stage) if isinstance(canonical_stage, Mapping) else None,
            "canonical_stage_identity_digest": stage_identity_digest(canonical_stage) if isinstance(canonical_stage, Mapping) else None,
            "client_request": payload_snapshot(request),
            "transport_request": copy.deepcopy(self._transport_request_snapshot or payload_snapshot(request)),
            "supervisor_received_request": payload_snapshot(supervisor_request),
        }

    def _resolve_stage_request(self, request: Mapping[str, Any], action: str, *, allow_missing: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        """Attach the one canonical Stage object to a Stage-scoped request."""

        normalized = copy.deepcopy(dict(request))
        raw_stage = normalized.get("stage")
        stage_id = normalized.get("stage_id") or normalized.get("subject_id")
        if isinstance(raw_stage, Mapping):
            if raw_stage.get("schema_version") == "stage_contract.v1":
                raise self._stage_failure(
                    "CONTRACT_VERSION_MISMATCH",
                    f"{action} accepts only the canonical stage.v2 object at request.stage",
                    request=request,
                    action=action,
                )
            try:
                candidate = validate_stage(raw_stage)
            except ContractValidationError as exc:
                raise self._stage_failure(
                    "STAGE_SCOPED_ACTION_MUST_USE_CANONICAL_STAGE",
                    f"{action} request.stage is not a valid canonical stage.v2 object",
                    request=request,
                    action=action,
                ) from exc
            stage_id = candidate["stage_id"]
        elif raw_stage is not None:
            raise self._stage_failure(
                "STAGE_SCOPED_ACTION_MUST_USE_CANONICAL_STAGE",
                f"{action} requires an object at request.stage",
                request=request,
                action=action,
            )
        elif not allow_missing:
            if action != "PLAN_STAGE" and isinstance(stage_id, str) and stage_id not in self.controller.state.get("stages", {}):
                raise WorkflowRuntimeError("V2_CONTRACT_REJECTED", f"unknown Stage: {stage_id}")
            raise self._stage_failure(
                "PLAN_STAGE_STAGE_OBJECT_OMITTED" if action == "PLAN_STAGE" else STAGE_PROPAGATION_INVARIANT,
                f"{action} requires the canonical stage.v2 object at request.stage",
                request=request,
                action=action,
            )
        if not isinstance(stage_id, str) or not stage_id.strip():
            raise self._stage_failure(
                "STAGE_SCOPED_ACTION_MUST_USE_CANONICAL_STAGE",
                f"{action} requires stage_id to resolve the canonical Stage",
                request=request,
                action=action,
            )
        try:
            canonical = self.controller.resolve_canonical_stage(stage_id)
        except WorkflowV2ControllerError as exc:
            # PLAN_STAGE is the one pre-registration boundary: its validated
            # request.stage is the canonical candidate that REGISTER_STAGE
            # will commit. Every later action resolves from the journal.
            if action == "PLAN_STAGE" and isinstance(raw_stage, Mapping) and stage_id == candidate["stage_id"]:
                canonical = candidate
            elif raw_stage is None and allow_missing:
                return normalized, {"stage_id": stage_id, "canonical_stage": None}
            else:
                raise WorkflowRuntimeError("V2_CONTRACT_REJECTED", str(exc)) from exc
        if isinstance(raw_stage, Mapping) and canonical_json(canonical) != canonical_json(candidate):
            raise self._stage_failure(
                STAGE_PROPAGATION_INVARIANT,
                f"{action} request.stage differs from the canonical Stage authority",
                request=request,
                action=action,
                canonical_stage=canonical,
            )
        normalized["stage"] = canonical
        trace = self._build_contract_trace(
            action=action,
            request=request,
            canonical_stage=canonical,
            supervisor_request=normalized,
        )
        self._last_contract_trace = trace
        return normalized, {"stage_id": canonical["stage_id"], "canonical_stage": canonical, "trace": trace}

    def build_stage_scoped_request(self, action: str, request: Mapping[str, Any], *, allow_missing: bool = False) -> dict[str, Any]:
        """Build a request with ``request.stage`` resolved from the authority."""

        normalized, _ = self._resolve_stage_request(request, action.upper(), allow_missing=allow_missing)
        return normalized

    def repair_request(self, request: Mapping[str, Any], error: BaseException) -> dict[str, Any] | None:
        """Repair only an omitted Stage projection using the journal authority."""

        if not isinstance(request, Mapping):
            return None
        operation = str(request.get("operation", "")).upper()
        action = operation
        if operation == "COMMAND":
            action = str(request.get("command", "")).upper()
            action = _COMMAND_WIRE_NAMES.get(action, action)
        if action not in {"PLAN_STAGE", *{_COMMAND_WIRE_NAMES.get(item, item) for item in STAGE_SCOPED_COMMANDS}}:
            return None
        try:
            normalized, info = self._resolve_stage_request(request, action, allow_missing=True)
        except WorkflowRuntimeError:
            return None
        if not isinstance(info.get("canonical_stage"), Mapping):
            return None
        return normalized

    def _artifact_path(self, artifact_kind: str, *, relative_path: str, source_operation: str) -> Path:
        """Resolve durable Product evidence through the single placement seam.

        The current self-host checkout is both the installed Engine and the
        owning Product root.  The resolver still records both identities and
        keeps this adapter ready for a relocated Engine without changing the
        V2 journal or command protocol.
        """
        brief = self.intake.state
        if brief is None:
            raise WorkflowRuntimeError("WORKFLOW_NOT_FOUND", "workflow has not been started")
        resolver = ArtifactResolver.from_roots(
            workspace_id=self.controller.workspace_id,
            project_id=brief["project_id"],
            project_root=self.root,
            engine_root=self.root,
        )
        return resolver.resolve(
            artifact_kind,
            "HISTORY",
            source_operation=source_operation,
            relative_path=relative_path,
        ).absolute_path

    def _view(self, **extra: Any) -> dict[str, Any]:
        brief = self.intake.state
        if brief is None:
            raise WorkflowRuntimeError("WORKFLOW_NOT_FOUND", "workflow has not been started")
        projection = self.controller.resume_projection()
        contract_path = Path(__file__).resolve().parents[1] / "docs" / "AUTONOMOUS_OBJECTIVE_COMPLETION_LOOP.md"
        try:
            contract_text = contract_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WorkflowRuntimeError("OUTER_LOOP_CONTEXT_MISSING", "outer supervisory contract is not available") from exc
        outer_loop_contract = {
            "path": str(contract_path),
            "digest": "contract-" + hashlib.sha256(contract_text.encode("utf-8")).hexdigest(),
            "loaded": True,
            "authority": "DESIGN_CONTEXT_ONLY",
        }
        presentation = build_human_presentation(
            metadata={**extra, "outer_loop_contract": outer_loop_contract},
            canonical_state=projection,
            artifact_root=self.root,
        )
        if brief["state"] != "APPROVED":
            question = self.intake.current_question
            action = "ASK_REQUIREMENT" if question else "REQUEST_BRIEF_APPROVAL"
            return {"human_summary": presentation["human_summary"],
                    "machine_details": presentation["machine_details"],
                    "presentation": presentation,
                    "schema_version": "product_workflow_result.v1", "next_action": action,
                    "next_tool": "workflow_answer", "question_id": question.get("question_id") if question else None,
                    "question": question, "brief_state": brief["state"], "canonical": projection,
                    "outer_loop_contract": outer_loop_contract, **extra}
        return {"human_summary": presentation["human_summary"],
                "machine_details": presentation["machine_details"],
                "presentation": presentation,
                "schema_version": "product_workflow_result.v1", "lifecycle_version": "v2",
                "workspace_root": str(self.root), "project_id": brief["project_id"],
                "brief_state": brief["state"], "canonical": projection, **projection, "next_tool": "workflow_answer" if projection["next_actor"] == "Human" else "workflow_run",
                "question_id": None, "outer_loop_contract": outer_loop_contract, **extra}

    def status(self) -> dict[str, Any]:
        return self._view()

    def _maintenance_context(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None] | None:
        """Return the current rejected result and its bound provider facts."""

        projection = self.controller.resume_projection()
        stage = projection.get("stage")
        if not isinstance(stage, Mapping):
            return None
        assessment_id = stage.get("current_assessment_id")
        state = self.controller.state
        if not isinstance(assessment_id, str) or not assessment_id:
            assessment_id = self._latest_continue_assessment_id(stage, state)
        assessment = state.get("assessments", {}).get(assessment_id)
        if not isinstance(assessment, Mapping):
            return None
        observation = next(
            (item for item in state.get("observations", {}).values()
             if item.get("stage_id") == stage.get("stage_id")
             and item.get("attempt_id") == assessment.get("attempt_id")
             and item.get("provider_result_digest") == assessment.get("provider_result_digest")),
            None,
        )
        if not isinstance(observation, Mapping):
            return None
        resolved = [
            item for item in state.get("decisions", {}).values()
            if item.get("actor_kind") == "GPT" and item.get("boundary") == "TECHNICAL_REVIEW"
            and item.get("subject_id") == assessment_id and item.get("resolution") is not None
        ]
        is_preserved_blocked = any(item.get("resolution") == "BLOCKED" for item in resolved)
        is_applied_continue = any(item.get("resolution") == "CONTINUE" for item in resolved)
        is_unreviewed_technical_failure = isinstance(observation.get("failure"), Mapping)
        if not assessment_id:
            return None
        operation = next(
            (item for item in state.get("operations", {}).values()
             if item.get("subject_id") == assessment.get("attempt_id")),
            None,
        )
        return dict(stage), dict(assessment), dict(observation), dict(operation) if isinstance(operation, Mapping) else None

    def _rebind_latest_live_assessment(self, stage_id: str) -> dict[str, Any] | None:
        """Recover a cleared assessment projection without rewriting evidence."""

        stage = self.controller.resolve_canonical_stage(stage_id)
        runtime = self.controller.state.get("stage_runtime", {}).get(stage_id, {})
        attempt_id = runtime.get("current_attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            return None
        candidates = [
            item for item in self.controller.state.get("assessments", {}).values()
            if item.get("stage_id") == stage_id
            and item.get("attempt_id") == attempt_id
            and item.get("iteration_id") == stage.get("current_iteration_id")
        ]
        if not candidates:
            return None
        assessment = copy.deepcopy(candidates[-1])
        receipt = self.controller.assess_result(
            stage_id,
            assessment,
            command_id="command-supervisor-assessment-rebind-" + sha256_json({"stage_id": stage_id, "assessment_id": assessment["assessment_id"], "revision": self.controller.revision})[:32],
        )
        return receipt.get("assessment") if isinstance(receipt.get("assessment"), Mapping) else assessment

    @staticmethod
    def _latest_continue_assessment_id(stage: Mapping[str, Any], state: Mapping[str, Any]) -> str | None:
        """Find an already-applied CONTINUE that still needs lifecycle handoff."""

        stage_id = stage.get("stage_id")
        iteration_id = stage.get("current_iteration_id")
        objective_identity = stage.get("objective_fingerprint")
        assessments = state.get("assessments", {})
        for decision in reversed(list(state.get("decisions", {}).values())):
            if (
                decision.get("actor_kind") != "GPT"
                or decision.get("boundary") != "TECHNICAL_REVIEW"
                or decision.get("resolution") != "CONTINUE"
            ):
                continue
            assessment_id = decision.get("subject_id")
            assessment = assessments.get(assessment_id) if isinstance(assessments, Mapping) else None
            if not isinstance(assessment, Mapping):
                continue
            if (
                assessment.get("stage_id") == stage_id
                and assessment.get("iteration_id") == iteration_id
                and assessment.get("objective_identity") in (None, objective_identity)
            ):
                return str(assessment_id)
        return None

    def _maintenance_blocker(self, context: tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None]) -> dict[str, Any]:
        stage, assessment, observation, operation = context
        failure = observation.get("failure") if isinstance(observation.get("failure"), Mapping) else None
        missing_owner = assessment.get("missing_thing_owner") or observation.get("missing_thing_owner")
        derived = classify_blocker(
            assessment=assessment,
            observation=observation,
            failure=failure,
            missing_thing_owner=missing_owner,
            evidence_refs=[observation.get("evidence_manifest_digest")],
        )
        operation_id = operation.get("operation_id") if operation else derived.get("operation_id")
        blocker_id = "blocker-" + sha256_json({
            "stage_id": stage["stage_id"],
            "assessment_id": assessment["assessment_id"],
            "failure_signature": derived["failure_signature"],
        })
        now = _utc_now()
        return {
            "blocker_id": blocker_id,
            "project_id": stage["project_id"],
            "stage_id": stage["stage_id"],
            "assessment_id": assessment["assessment_id"],
            "revision": self.controller.revision,
            "failure_class": derived["failure_class"],
            "failure_code": derived["failure_code"],
            "failure_signature": derived["failure_signature"],
            "evidence_refs": derived["evidence_refs"],
            "recoverability": derived["recoverability"],
            "origin": "RESUME_REASSESSMENT",
            "created_at": now,
            "last_validated_at": now,
            "resolved_at": None,
            "missing_thing_owner": missing_owner,
            "operation_id": operation_id,
            "recommended_action": derived["recommended_action"],
        }

    def _auto_advance_continue_iteration(
        self,
        context: tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """Resume an applied CONTINUE through canonical ADVANCE_ITERATION."""

        stage, assessment, _observation, _operation = context
        state = self.controller.state
        decision = next(
            (
                item for item in reversed(list(state.get("decisions", {}).values()))
                if item.get("actor_kind") == "GPT"
                and item.get("boundary") == "TECHNICAL_REVIEW"
                and item.get("resolution") == "CONTINUE"
                and item.get("subject_id") == assessment.get("assessment_id")
            ),
            None,
        )
        if not isinstance(decision, Mapping):
            return None
        if decision.get("objective_identity") not in (None, stage.get("objective_fingerprint")):
            return None
        budget = self.controller.attempt_budget_status(stage["stage_id"])
        if (
            not budget.get("per_iteration_exhausted")
            or budget.get("total_exhausted")
            or budget.get("max_iteration_exhausted")
        ):
            return None
        if stage.get("status") != "ACTIVE" or stage.get("pending_decisions") or stage.get("open_dependencies"):
            return None
        blocker = self._maintenance_blocker(context)
        if blocker.get("failure_class") == "EXTERNAL_BLOCKER" or blocker.get("recoverability") == "EXTERNAL_UNAVAILABLE":
            return None
        change_digest = continue_iteration_change_digest(
            decision_id=str(decision["decision_id"]),
            assessment_id=str(assessment["assessment_id"]),
            iteration_id=str(stage["current_iteration_id"]),
            objective_identity=str(stage["objective_fingerprint"]),
        )
        command_id = "command-auto-advance-continue-" + sha256_json(
            {
                "stage_id": stage["stage_id"],
                "decision_id": decision["decision_id"],
                "iteration_id": stage["current_iteration_id"],
            }
        )[:32]
        return self.controller.advance_iteration(
            stage["stage_id"],
            command_id=command_id,
            payload={
                "auto_advance": True,
                "requires_next_iteration": True,
                "review_identity": decision["decision_id"],
                "technical_change_digest": change_digest,
                "solution_fingerprint": stage["objective_fingerprint"],
            },
        )

    @staticmethod
    def _bounded_result(result: Mapping[str, Any]) -> dict[str, Any]:
        """Keep provider claims small before they become journal evidence."""

        return {
            key: copy.deepcopy(result.get(key))
            for key in ("status", "summary", "changed_files", "tests", "problems_discovered", "execution_failure")
            if key in result
        }

    def _maintenance_request(self, context: tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None], *, suffix: str) -> dict[str, Any]:
        stage, assessment, _observation, operation = context
        # The CONTINUE handoff may have opened a new iteration immediately
        # before this provider request.  Re-read the canonical stage so the
        # bounded request carries the new iteration index, while retaining the
        # old operation descriptor as immutable recovery provenance.
        stage = self.controller.resolve_canonical_stage(stage["stage_id"])
        descriptor = (operation or {}).get("provider_handoff_manifest", {}).get("reconstructible_request_descriptor", {})
        original = descriptor.get("request", {}) if isinstance(descriptor, Mapping) else {}
        request = copy.deepcopy(dict(original)) if isinstance(original, Mapping) else {}
        state = self.controller.state
        iteration = state.get("iterations", {}).get(stage.get("current_iteration_id"), {})
        attempt_index = len([item for item in state.get("attempts", {}).values() if item.get("stage_id") == stage["stage_id"]]) + 1
        request_id = "request-maintenance-" + suffix
        required_path = request.get("required_changed_path") or request.get("execution_receipt_path")
        if not isinstance(required_path, str) or not required_path.strip():
            required_path = ".tmp/workflow-v2-maintenance-result.json"
        allowed_paths = request.get("allowed_paths")
        if not isinstance(allowed_paths, list) or not allowed_paths:
            allowed_paths = [required_path.split("/", 1)[0]]
        protected_paths = request.get("protected_paths")
        if not isinstance(protected_paths, list):
            protected_paths = [".git", ".workflow-v2", ".research", ".consultations"]
        request.update({
            "request_id": request_id,
            "task_id": request_id,
            "plan_id": request.get("plan_id") or "plan-" + stage["objective_fingerprint"],
            "stage_id": stage["stage_id"],
            "objective": (
                str(request.get("objective") or stage["target_identity"])
                + " Resume only the bounded stage-owned generation or evidence-producing route needed by the current objective."
            ),
            "iteration_index": int(iteration.get("index", 1)),
            "attempt_index": attempt_index,
            "allowed_paths": allowed_paths,
            "protected_paths": protected_paths,
            "required_changed_path": required_path,
            "execution_receipt_path": required_path,
            "action_map": {
                "schema_version": "action_map.v1",
                "validated": True,
                "execute": True,
                "actions": ["bounded_stage_owned_recovery"],
                "authority": "WORKFLOW_CONTROLLER",
            },
            "maintenance_recovery": True,
            "maintenance_instruction": (
                "If a required stage-owned generated input or artifact is absent, start its bounded generation within the declared allowed scope and record only a generation-start/result marker. "
                "Do not modify source, configuration, tests, benchmark, workflow state, or existing evidence. Do not infer scientific success from a marker."
            ),
        })
        return request

    def _maintenance_provider(self) -> Any:
        injected = self.maintenance_capability
        if injected is not None and callable(getattr(injected, "execute", None)):
            return injected
        return OpenAICodexExecutor(
            codex_executable=self.config.codex_executable,
            workspace_root=str(self.root),
            preferred_model=self.config.preferred_model,
            fallback_model=self.config.fallback_model,
            auth_mode=self.config.auth_mode,
            timeout_seconds=self.config.timeout_seconds,
            artifact_dir=self.config.artifact_dir,
        )

    def _execute_maintenance_provider(self, context: tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None], *, reason: str, suffix: str) -> dict[str, Any]:
        stage, _assessment, _observation, _operation = context
        request = self._maintenance_request(context, suffix=suffix)
        submitted = self.controller.request_execution(
            stage["stage_id"], request=request, reason=reason,
            request_id=request["request_id"], attempt_id="attempt-maintenance-" + suffix,
            operation_id="operation-maintenance-" + suffix,
            purpose=stage["purpose"],
            provenance={"provider": "openai-codex", "engine_digest": "engine-v2.1.3-maintenance"},
            command_id="command-maintenance-execution-" + suffix,
        )
        attempt = submitted["attempt"]
        operation = submitted["operation"]
        try:
            execution_request = ExecutionRequest.from_stage_request(
                request,
                plan_id=request["plan_id"],
                task_id=request["task_id"],
                required_capabilities=stage.get("required_capabilities", ()),
                preferred_provider=self.config.preferred_provider,
                action_map=request["action_map"],
                workspace_root=str(self.root),
                metadata={
                    "executor_request_id": request["request_id"],
                    "operation_id": operation["operation_id"],
                    "required_test_command": request.get("required_test_command"),
                    "required_changed_path": request.get("required_changed_path"),
                    "execution_receipt_path": request.get("execution_receipt_path"),
                    "fallback_model": self.config.fallback_model,
                    "execution_profile": "STANDARD",
                },
            )
            provider_result = self._maintenance_provider().execute(execution_request)
            if isinstance(provider_result, ExecutionResult):
                result = provider_result
            elif isinstance(provider_result, Mapping):
                result = ExecutionResult(provider_id="openai-codex", result=provider_result, evidence={})
            else:
                raise ContractValidationError("maintenance provider returned an invalid result")
            result_payload = result.to_stage_result()
            provider_id = result.provider_id
            evidence = result.evidence
        except Exception as exc:
            # The attempt is already intent-committed.  Convert provider-side
            # exceptions into a settled, typed observation so resume never
            # relays the exception to Human or leaves an unclassified intent.
            provider_id = "openai-codex"
            result_payload = {
                "schema_version": "codex_result.v1",
                "plan_id": request["plan_id"], "stage_id": stage["stage_id"], "task_id": request["task_id"],
                "iteration_index": request["iteration_index"], "status": "ERROR",
                "summary": "Bounded maintenance provider invocation failed before evidence completion.",
                "changed_files": [], "tests": [], "measurements": {}, "evidence_refs": [],
                "review_artifacts": [], "problems_discovered": [type(exc).__name__],
                "abstraction_layer": "openai-codex", "stage_ready": False,
                "user_visible_failure": True, "human_gate_required": False,
                "decision_reason": "Controller will classify and escalate the provider failure.",
                "stop_reason": "maintenance_provider_exception",
                "execution_failure": {"kind": "PROVIDER_FAILURE", "code": type(exc).__name__, "retryable": True, "reason": str(exc)[:256]},
            }
            evidence = {"provider_exception": type(exc).__name__}
        result_digest = "provider-result-" + sha256_json(result_payload)
        required_path = request["required_changed_path"].replace("\\", "/")
        raw_changed = result_payload.get("changed_files", [])
        changed = [str(item).replace("\\", "/") for item in raw_changed if isinstance(item, str)] if isinstance(raw_changed, list) else []
        allowed = [str(item).replace("\\", "/").rstrip("/") for item in request["allowed_paths"]]
        scope_violation = [path for path in changed if not any(path == scope or path.startswith(scope + "/") for scope in allowed)]
        changed = [path for path in changed if path not in scope_violation]
        required_file = _inside(self.root, required_path)
        if required_file.is_file() and required_path not in changed:
            changed.append(required_path)
        inventory = [{"path": path, "sha256": "artifact-" + sha256_json({"path": path, "result": result_digest})} for path in sorted(set(changed))]
        complete = required_file.is_file() and not scope_violation
        manifest_body = {
            "schema_version": "evidence_manifest.v2", "stage_id": stage["stage_id"], "attempt_id": attempt["attempt_id"],
            "required_artifact_paths": [required_path], "changed_paths": sorted(set(changed)),
            "allowed_paths": allowed, "protected_paths": request["protected_paths"], "path_inventory": inventory, "complete": complete,
        }
        manifest = {"manifest_id": "manifest-" + sha256_json(manifest_body), **manifest_body}
        failure_data = result_payload.get("execution_failure") if isinstance(result_payload.get("execution_failure"), Mapping) else None
        terminal = "SUCCEEDED" if result_payload.get("status") == "SUCCEEDED" and not scope_violation else ("ERROR" if result_payload.get("status") == "ERROR" else "FAILED")
        failure = None
        if terminal != "SUCCEEDED":
            failure_code = str((failure_data or {}).get("code") or ("SCOPE_VIOLATION" if scope_violation else "PROVIDER_RESULT_FAILED"))
            declared_class = str(
                (failure_data or {}).get("failure_class")
                or (failure_data or {}).get("kind")
                or ""
            ).upper()
            if "TRANSPORT" in declared_class or "TRANSPORT" in failure_code.upper():
                failure_class = "TRANSPORT_FAILURE"
            elif declared_class == "SCIENTIFIC_BLOCKER" and result_payload.get("human_gate_required") is True:
                failure_class = "SCIENTIFIC_BLOCKER"
            else:
                # A provider's bare BLOCKED/ERROR token is an execution
                # outcome, not scientific evidence. It must remain a typed
                # technical failure until a reviewer proves otherwise.
                failure_class = "PROVIDER_FAILURE"
            failure = {
                "schema_version": "typed_failure.v2", "failure_class": failure_class, "code": failure_code[:128],
                "operation_id": operation["operation_id"], "retryability": "RETRYABLE" if bool((failure_data or {}).get("retryable", True)) else "NON_RETRYABLE",
                "effect_state": "SETTLED", "evidence_refs": ["evidence-" + sha256_json({"result": result_digest})], "authority": "CONTROLLER_POLICY",
            }
        observation = {
            "schema_version": "provider_observation.v2", "stage_id": stage["stage_id"],
            "objective_identity": stage["objective_fingerprint"], "iteration_id": attempt["iteration_id"], "attempt_id": attempt["attempt_id"],
            "provider_operation_id": operation["operation_id"], "result_identity": result_digest,
            "provider_result_digest": result_digest, "evidence_manifest_digest": manifest["manifest_id"],
            "provider_terminal_status": terminal, "raw_provider_claim": self._bounded_result(result_payload),
            "provenance": {"provider": provider_id, "engine_digest": "engine-v2.1.3-maintenance", "evidence": copy.deepcopy(dict(evidence))},
            "failure": failure, "outputs": {"required_artifact_paths": [required_path], "scope_violation": scope_violation}, "immutable": True,
        }
        observation["observation_id"] = "pending"
        from .workflow_v2_contracts import observation_identity
        observation["observation_id"] = observation_identity(observation)
        observed = self.controller.record_observation(
            stage["stage_id"], observation, effect_state="SETTLED", command_id="command-maintenance-observation-" + suffix,
        )
        checked_assessment = assess_observation(
            observation, manifest, baseline_digest=stage["baseline_digest"],
            validator_code_digest=_assessment.get("validator_code_digest") if isinstance(_assessment, Mapping) else "validator-maintenance-v213",
            validation_contract_revision="admission.v2-maintenance",
            checks={"provider_status": terminal, "required_artifact": "PASS" if complete else "MISSING", "scope": "FAIL" if scope_violation else "PASS"},
        )
        assessed = self.controller.assess_result(stage["stage_id"], checked_assessment, command_id="command-maintenance-assessment-" + suffix)
        return {
            "request": request, "attempt": attempt, "operation": operation,
            "provider_result": self._bounded_result(result_payload), "observation": observed.get("observation", observation),
            "assessment": assessed.get("assessment", checked_assessment), "manifest": manifest,
            "provider_id": provider_id, "scope_violation": scope_violation,
            "generation_started": bool(required_file.is_file()),
        }

    def _technical_review_decision(self, context: tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None], blocker: Mapping[str, Any], *, terminal_budget_review: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        stage, assessment, observation, _operation = context
        state = self.controller.state
        prior_admissible = next(
            (
                item for item in reversed(list(state.get("assessments", {}).values()))
                if item.get("stage_id") == stage["stage_id"]
                and item.get("verdict") == "ADMISSIBLE"
                and item.get("assessment_id") != assessment["assessment_id"]
            ),
            None,
        )
        attempted_recovery = [
            {
                "attempt_id": item.get("attempt_id"),
                "status": item.get("status"),
                "effect_state": item.get("effect_state"),
            }
            for item in state.get("attempts", {}).values()
            if item.get("stage_id") == stage["stage_id"]
        ]
        operation = next(
            (
                item for item in state.get("operations", {}).values()
                if item.get("subject_id") == assessment.get("attempt_id")
            ),
            None,
        )
        pack = {
            "pack_sha256": sha256_json({"stage": stage["stage_id"], "assessment": assessment["assessment_id"], "blocker": blocker["failure_signature"]}),
            "purpose": "TECHNICAL_ESCALATION_REVIEW",
            "PROJECT_ID": stage["project_id"],
            "STAGE_ID": stage["stage_id"],
            "OBJECTIVE_IDENTITY": stage["objective_fingerprint"],
            "STAGE_GOAL": stage["target_identity"],
            "ASSESSMENT_ID": assessment["assessment_id"],
            "OBSERVATION_ID": observation["observation_id"],
            "last_successful_state": {
                "assessment_id": prior_admissible.get("assessment_id") if isinstance(prior_admissible, Mapping) else None,
                "verdict": prior_admissible.get("verdict") if isinstance(prior_admissible, Mapping) else None,
            },
            "current_blocker": dict(blocker),
            "failure_signature": blocker["failure_signature"],
            "current_evidence": {
                "observation_id": observation["observation_id"],
                "evidence_manifest_digest": observation.get("evidence_manifest_digest"),
                "provider_result_digest": observation.get("provider_result_digest"),
                "outputs": copy.deepcopy(observation.get("outputs", {})),
            },
            "attempted_recovery": attempted_recovery,
            "recovery_results": copy.deepcopy(observation.get("raw_provider_claim", {})),
            "constraints": {
                "allowed_paths": copy.deepcopy(operation.get("provider_handoff_manifest", {}).get("allowed_paths", [])) if isinstance(operation, Mapping) else [],
                "protected_paths": copy.deepcopy(operation.get("provider_handoff_manifest", {}).get("protected_paths", [])) if isinstance(operation, Mapping) else [],
                "attempt_budget": copy.deepcopy(stage.get("budgets", {})),
            },
            "available_capabilities": copy.deepcopy(stage.get("required_capabilities", [])),
            "current_implementation_refs": copy.deepcopy(observation.get("outputs", {}).get("changed_paths", [])) if isinstance(observation.get("outputs"), Mapping) else [],
            "relevant_artifacts": copy.deepcopy(observation.get("outputs", {}).get("required_artifact_paths", [])) if isinstance(observation.get("outputs"), Mapping) else [],
            "accepted_baseline": stage.get("baseline_digest"),
        }
        terminal_instruction = (
            "The controller has exhausted the authorized attempt budget and rejected the proposed provider route. "
            "This is a terminal technical review: return exactly BLOCKED or HUMAN_GATE. Do not return CONTINUE, REPLAN, or STAGE_READY. "
            if terminal_budget_review else ""
        )
        prompt = (
            "Perform one bounded technical recovery review for the current Workflow Stage. "
            "Use only the packet identities and evidence. Decide whether a legal technical route remains. "
            "Return exactly one marker: WORKFLOW_DECISION: CONTINUE, REPLAN, STAGE_READY, HUMAN_GATE, or BLOCKED. "
            "CONTINUE and REPLAN must be applied automatically when legal; HUMAN_GATE requires QUESTION_FOR_HUMAN, WHY_AI_CANNOT_DECIDE, OPTIONS, CONSEQUENCE, HUMAN_DECISION_REQUIRED. "
            "Do not claim scientific success without evidence.\n" + terminal_instruction
            + json.dumps({"blocker": dict(blocker), "packet": pack}, ensure_ascii=False, sort_keys=True)
        )
        consulted = self._consult({"context_pack": pack, "prompt": prompt, "objective_identity": stage["objective_fingerprint"]}, purpose="TECHNICAL_ESCALATION_REVIEW")
        decision = {
            "schema_version": "decision.v2", "decision_id": "decision-maintenance-gpt-" + sha256_json({"assessment": assessment["assessment_id"], "response": consulted["response_digest"]}),
            "actor_kind": "GPT", "boundary": "TECHNICAL_REVIEW", "subject_id": assessment["assessment_id"],
            "objective_identity": stage["objective_fingerprint"], "subject_type": "BLOCKER_REASSESSMENT",
            "subject_digest": assessment["assessment_id"], "subject_version": 1,
            "allowed_choices": ["CONTINUE", "REPLAN:ENGINEERING_FIX", "REPLAN:NEXT_ITERATION", "REPLAN:BASELINE_CHANGE", "STAGE_READY", "HUMAN_GATE", "BLOCKED"],
            "requested_action": "APPLY_GPT_DECISION", "provenance": {
                "request_count": consulted["request_count"], "conversation_id": consulted["conversation_id"],
                "response_digest": consulted["response_digest"], "packet_digest": consulted["packet_digest"],
            }, "supersedes": None,
        }
        return consulted, decision

    def _resume_blocked_maintenance(self, context: tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None], *, inherited: Mapping[str, Any] | None = None) -> dict[str, Any]:
        stage, assessment, observation, _operation = context
        blocker = self._maintenance_blocker(context)
        action = blocker["recommended_action"]
        state = self.controller.state
        preserved_blocked = any(
            item.get("actor_kind") == "GPT" and item.get("boundary") == "TECHNICAL_REVIEW"
            and item.get("subject_id") == assessment["assessment_id"] and item.get("resolution") == "BLOCKED"
            for item in state.get("decisions", {}).values()
        )
        applied_continue = any(
            item.get("actor_kind") == "GPT"
            and item.get("boundary") == "TECHNICAL_REVIEW"
            and item.get("subject_id") == assessment["assessment_id"]
            and item.get("resolution") == "CONTINUE"
            for item in state.get("decisions", {}).values()
        )
        budget = self.controller.attempt_budget_status(stage["stage_id"])
        suffix = sha256_json({"blocker": blocker["blocker_id"], "revision": self.controller.revision})[:32]
        maintenance: dict[str, Any] = {"blocker_classification": blocker, "human_intervention_count": 0, **(dict(inherited) if isinstance(inherited, Mapping) else {})}
        if action == "WORK_REMAINING" and not budget.get("per_iteration_exhausted") and not budget.get("total_exhausted"):
            revalidated = None
            if preserved_blocked:
                revalidated = self.controller.revalidate_blocker(
                    stage["stage_id"], blocker_record=blocker, blocker_still_true="NO",
                    released_attempt_id=self.controller.state["stage_runtime"][stage["stage_id"]].get("current_attempt_id"),
                    revalidation_id="blocker-revalidation-" + suffix,
                    command_id="command-maintenance-revalidate-" + suffix,
                )
            executed = self._execute_maintenance_provider(
                context, reason="CONTINUE", suffix=suffix,
            )
            maintenance.update({
                "resume_blocker_revalidation": revalidated.get("revalidation"),
                "technical_recovery": {"status": "PASS", "route": "WORK_REMAINING", "provider": executed["provider_id"]},
                "provider_execution": executed,
                "stage_owned_output_missing": True,
                "generation_started": executed["generation_started"],
                "gpt_technical_escalation": {"status": "NOT_REQUIRED", "reason": "stage-owned work remains"},
            })
            return self._view(**maintenance)
        terminal_budget_review = bool(budget.get("total_exhausted") or budget.get("max_iteration_exhausted"))
        consulted, decision = self._technical_review_decision(context, blocker, terminal_budget_review=terminal_budget_review)
        choice = consulted["decision"]
        if choice in {"CONTINUE", "REPLAN", "STAGE_READY"}:
            revalidated = None
            if preserved_blocked and choice != "STAGE_READY":
                revalidated = self.controller.revalidate_blocker(
                    stage["stage_id"], blocker_record=blocker, blocker_still_true="NO",
                    released_attempt_id=self.controller.state["stage_runtime"][stage["stage_id"]].get("current_attempt_id"),
                    revalidation_id="blocker-revalidation-" + suffix,
                    command_id="command-maintenance-revalidate-" + suffix,
                )
            apply_kwargs: dict[str, Any] = {}
            if choice == "REPLAN":
                apply_kwargs.update({"replan_subtype": "ENGINEERING_FIX", "technical_change_digest": "change-" + blocker["failure_signature"]})
            budget_before_apply = self.controller.attempt_budget_status(stage["stage_id"])
            continue_can_open_next_iteration = (
                choice == "CONTINUE"
                and budget_before_apply.get("per_iteration_exhausted")
                and not budget_before_apply.get("total_exhausted")
                and not budget_before_apply.get("max_iteration_exhausted")
            )
            if budget_before_apply.get("total_exhausted") or (
                budget_before_apply.get("per_iteration_exhausted") and not continue_can_open_next_iteration
            ):
                # A valid GPT marker is not permission to bypass the
                # controller's attempt budget.  Preserve the assessment and
                # leave a non-terminal technical continuation point for the
                # next supervisory resume.
                maintenance.update({
                    "technical_gpt_escalation": {"status": "PASS", "decision": choice, "consultation_id": consulted["consultation_id"]},
                    "technical_recovery": {"status": "BOUNDED_LIMIT", "route": choice},
                    "provider_execution": None,
                    "generation_started": False,
                    "budget_route_rejected": "ATTEMPT_BUDGET_EXHAUSTED",
                })
                return self._view(**maintenance)
            applied = self.controller.apply_gpt_decision(
                stage["stage_id"], decision, choice=choice, command_id="command-maintenance-gpt-apply-" + suffix, **apply_kwargs,
            )
            executed = None
            if choice != "STAGE_READY":
                try:
                    executed = self._execute_maintenance_provider(context, reason="ENGINEERING_FIX" if choice == "REPLAN" else "CONTINUE", suffix=suffix)
                except (WorkflowRuntimeError, WorkflowV2ControllerError) as exc:
                    # A technical review may be valid while the bounded attempt
                    # budget is already exhausted.  Keep the applied decision and
                    # expose the controller's typed limit without relaying it as a
                    # new Human decision.
                    maintenance["provider_execution_error"] = {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)[:256]}
            maintenance.update({
                "resume_blocker_revalidation": revalidated.get("revalidation") if revalidated else None,
                "technical_gpt_escalation": {"status": "PASS", "decision": choice, "consultation_id": consulted["consultation_id"]},
                "gpt_decision_applied": applied.get("decision"), "provider_execution": executed,
                "technical_recovery": {"status": "PASS", "route": choice},
                "generation_started": executed["generation_started"] if executed else False,
                "auto_next_iteration": bool(applied.get("auto_next_iteration")),
                "new_iteration_started": bool(applied.get("auto_next_iteration")),
                "iteration_transition": {
                    "iteration": copy.deepcopy(applied.get("iteration")),
                    "stage": copy.deepcopy(applied.get("stage")),
                } if applied.get("auto_next_iteration") else None,
            })
            return self._view(**maintenance)
        revalidated = None
        if preserved_blocked:
            revalidated = self.controller.revalidate_blocker(
                stage["stage_id"], blocker_record=blocker, blocker_still_true="YES",
                revalidation_id="blocker-revalidation-" + suffix,
                command_id="command-maintenance-revalidate-" + suffix,
            )
        if choice == "HUMAN_GATE":
            # The bridge returns only the closed decision marker.  Without the
            # required explanation fields it is not legal to emit a gate.
            maintenance["technical_gpt_escalation"] = {"status": "INVALID_HUMAN_GATE", "consultation_id": consulted["consultation_id"]}
        elif choice == "BLOCKED":
            blocked_validation = {
                "blocker_still_true": "YES", "auto_recovery_exhausted": True,
                "gpt_technical_escalation_completed": True, "no_legal_automated_next_action": True,
            }
            applied = self.controller.apply_gpt_decision(
                stage["stage_id"], decision, choice="BLOCKED", strict_blocked_validation=True,
                blocked_validation=blocked_validation, command_id="command-maintenance-gpt-apply-" + suffix,
            )
            maintenance["gpt_decision_applied"] = applied.get("decision")
            maintenance["blocked_validation"] = blocked_validation
            maintenance["technical_gpt_escalation"] = {"status": "PASS", "decision": "BLOCKED", "consultation_id": consulted["consultation_id"]}
        else:
            maintenance["technical_gpt_escalation"] = {"status": "INVALID_STAGE_READY", "consultation_id": consulted["consultation_id"]}
        maintenance["resume_blocker_revalidation"] = revalidated.get("revalidation") if revalidated else None
        maintenance["technical_recovery"] = {"status": "BLOCKED", "route": choice}
        return self._view(**maintenance)

    @_boundary
    def resume(self) -> dict[str, Any]:
        inherited: dict[str, Any] = {}
        supervisor_steps: list[dict[str, Any]] = []
        budget_authority: dict[str, Any] | None = None
        budget_migration: dict[str, Any] | None = None
        carry_keys = (
            "legacy_resolution", "old_operation_preserved", "old_operation_redispatched", "side_effect_audit", "self_repair",
            "blocker_classification", "resume_blocker_revalidation", "technical_recovery", "technical_gpt_escalation",
            "gpt_decision_applied", "provider_execution", "stage_owned_output_missing", "generation_started",
            "blocked_validation", "human_intervention_count", "provider_execution_error", "auto_next_iteration",
            "new_iteration_started", "iteration_transition",
            "budget_route_rejected",
        )

        def carry(result: Mapping[str, Any]) -> None:
            for key in carry_keys:
                if key in result:
                    inherited[key] = copy.deepcopy(result[key])

        for step_index in range(self.supervisor_step_limit):
            projection = self.controller.resume_projection()
            termination = projection.get("termination_validation", {})
            stage = projection.get("stage") if isinstance(projection.get("stage"), Mapping) else {}
            stage_id = stage.get("stage_id")
            if termination.get("allowed"):
                return self._view(
                    **inherited,
                    budget_authority=budget_authority,
                    budget_migration=budget_migration,
                    termination_validation=termination,
                    legal_next_action=projection.get("legal_next_action"),
                    outer_supervisory_loop={
                        "status": "TERMINATED_BY_ALLOWED_CONDITION",
                        "steps": supervisor_steps,
                        "step_limit": self.supervisor_step_limit,
                        "human_intervention_count": 0,
                    },
                )

            if projection.get("next_action") == "RESOLVE_LEGACY_ORPHAN" and isinstance(stage_id, str):
                operation_id = stage.get("in_flight_operation_id")
                if isinstance(operation_id, str) and operation_id:
                    audit = self.controller.audit_legacy_side_effects(stage_id, search_roots=[self.root])
                    if audit.get("classification") == "NONE" and audit.get("complete") is True:
                        resolution = self.controller.resolve_legacy_orphan(
                            stage_id,
                            operation_id=operation_id,
                            effect_class="REVERSIBLE_LOCAL_RESEARCH",
                            side_effect_audit=audit,
                            command_id="command-legacy-orphan-resolution-" + operation_id[-32:],
                        )
                        inherited.update({
                            "legacy_resolution": resolution.get("legacy_resolution"),
                            "old_operation_preserved": True,
                            "old_operation_redispatched": False,
                            "side_effect_audit": audit,
                            "self_repair": {"status": "PASS", "human_intervention_count": 0},
                        })
                        supervisor_steps.append({"index": step_index + 1, "action": "RESOLVE_LEGACY_ORPHAN", "status": "PASS"})
                        continue
                    return self._view(
                        **inherited,
                        legacy_resolution={"status": "REQUIRES_BOUNDED_AUDIT", "operation_id": operation_id},
                        side_effect_audit=audit,
                        self_repair={"status": "NOT_SAFE_TO_ABANDON", "human_intervention_count": 0},
                        termination_validation=termination,
                        legal_next_action=projection.get("legal_next_action"),
                        outer_supervisory_loop={
                            "status": "NON_TERMINAL_REQUIRES_BOUNDED_AUDIT",
                            "steps": supervisor_steps,
                            "step_limit": self.supervisor_step_limit,
                            "human_intervention_count": 0,
                        },
                    )

            if isinstance(stage_id, str):
                brief = self.intake.state or {}
                profile = brief.get("execution_profile") if isinstance(brief, Mapping) else None
                budget_authority = self.controller.budget_authority_status(stage_id, execution_profile=profile if isinstance(profile, Mapping) else None)
                if budget_authority.get("migratable"):
                    recommended = budget_authority.get("recommended_budget")
                    profile_view = budget_authority.get("execution_profile", {})
                    migrated = self.controller.migrate_budget(
                        stage_id,
                        payload={
                            "authorization": "CURRENT_EXECUTION_PROFILE_MIGRATION",
                            "authority": "PROJECT_BRIEF",
                            "profile_name": profile_view.get("name") or "autonomous_research",
                            "profile_version": profile_view.get("version") or "1",
                            "budget_policy": recommended,
                            "migration_reason": "resume audit of unattributed legacy/default Stage budget",
                        },
                        command_id="command-supervisor-budget-migration-" + sha256_json({"stage_id": stage_id, "before": budget_authority.get("budget_value"), "after": recommended})[:32],
                    )
                    budget_migration = migrated.get("budget_audit")
                    budget_authority = self.controller.budget_authority_status(stage_id, execution_profile=profile if isinstance(profile, Mapping) else None)
                    supervisor_steps.append({"index": step_index + 1, "action": "MIGRATE_BUDGET", "status": "PASS", "budget_audit_id": budget_migration.get("budget_audit_id") if isinstance(budget_migration, Mapping) else None})
                    continue

            context = self._maintenance_context()
            if context is not None:
                advanced = self._auto_advance_continue_iteration(context)
                if advanced is not None:
                    next_stage = advanced.get("stage", {})
                    suffix = sha256_json({
                        "stage_id": next_stage.get("stage_id"),
                        "iteration_id": next_stage.get("current_iteration_id"),
                        "revision": self.controller.revision,
                    })[:32]
                    executed = self._execute_maintenance_provider(context, reason="CONTINUE", suffix=suffix)
                    inherited.update({
                        "auto_next_iteration": True,
                        "new_iteration_started": True,
                        "iteration_transition": advanced,
                        "provider_execution": executed,
                        "technical_recovery": {"status": "PASS", "route": "CONTINUE", "provider": executed["provider_id"]},
                        "generation_started": executed["generation_started"],
                        "human_intervention_count": 0,
                    })
                    supervisor_steps.append({"index": step_index + 1, "action": "ADVANCE_ITERATION", "status": "PASS"})
                    continue
                maintenance_result = self._resume_blocked_maintenance(context, inherited=inherited)
                carry(maintenance_result)
                supervisor_steps.append({
                    "index": step_index + 1,
                    "action": "TECHNICAL_SUPERVISORY_RECOVERY",
                    "status": maintenance_result.get("technical_recovery", {}).get("status", "PASS"),
                })
                provider_execution = maintenance_result.get("provider_execution")
                if "provider_execution_error" in maintenance_result:
                    current_projection = self.controller.resume_projection()
                    return self._view(
                        **inherited,
                        budget_authority=budget_authority,
                        budget_migration=budget_migration,
                        termination_validation=current_projection.get("termination_validation"),
                        legal_next_action=current_projection.get("legal_next_action"),
                        outer_supervisory_loop={
                            "status": "NON_TERMINAL_PROVIDER_ROUTE_EXHAUSTED",
                            "steps": supervisor_steps,
                            "step_limit": self.supervisor_step_limit,
                            "human_intervention_count": 0,
                        },
                    )
                if "budget_route_rejected" in maintenance_result:
                    current_projection = self.controller.resume_projection()
                    return self._view(
                        **inherited,
                        budget_authority=budget_authority,
                        budget_migration=budget_migration,
                        termination_validation=current_projection.get("termination_validation"),
                        legal_next_action=current_projection.get("legal_next_action"),
                        outer_supervisory_loop={
                            "status": "NON_TERMINAL_BUDGET_ROUTE_REJECTED",
                            "steps": supervisor_steps,
                            "step_limit": self.supervisor_step_limit,
                            "human_intervention_count": 0,
                        },
                    )
                if isinstance(provider_execution, Mapping) and isinstance(provider_execution.get("provider_result"), Mapping) and provider_execution["provider_result"].get("status") == "SUCCEEDED":
                    # One successful bounded Provider action is enough for this
                    # resume call.  The resulting admissible assessment is a
                    # non-terminal continuation point; a later resume may ask
                    # the technical reviewer for STAGE_READY without making a
                    # second provider attempt in the same turn.
                    current_projection = self.controller.resume_projection()
                    return self._view(
                        **inherited,
                        budget_authority=budget_authority,
                        budget_migration=budget_migration,
                        termination_validation=current_projection.get("termination_validation"),
                        legal_next_action=current_projection.get("legal_next_action"),
                        outer_supervisory_loop={
                            "status": "NON_TERMINAL_ASSESSMENT_PENDING",
                            "steps": supervisor_steps,
                            "step_limit": self.supervisor_step_limit,
                            "human_intervention_count": 0,
                        },
                    )
                continue

            legal = self.controller.resolve_legal_next_action(stage_id)
            action = legal.get("action")
            if action == "START" and isinstance(stage_id, str):
                started = self.controller.start(stage_id, command_id="command-supervisor-start-" + sha256_json({"stage_id": stage_id})[:32])
                supervisor_steps.append({"index": step_index + 1, "action": "START", "status": "PASS"})
                continue
            if action == "ASSESS_RESULT" and isinstance(stage_id, str):
                rebound = self._rebind_latest_live_assessment(stage_id)
                if rebound is not None:
                    supervisor_steps.append({"index": step_index + 1, "action": "ASSESS_RESULT", "status": "PASS", "assessment_id": rebound.get("assessment_id")})
                    continue
            supervisor_steps.append({"index": step_index + 1, "action": action, "status": "NON_TERMINAL"})
            return self._view(
                **inherited,
                budget_authority=budget_authority,
                budget_migration=budget_migration,
                termination_validation=self.controller.validate_termination(stage_id),
                legal_next_action=legal,
                outer_supervisory_loop={
                    "status": "NON_TERMINAL_CONTINUATION_REQUIRED",
                    "steps": supervisor_steps,
                    "step_limit": self.supervisor_step_limit,
                    "human_intervention_count": 0,
                },
            )

        final_projection = self.controller.resume_projection()
        return self._view(
            **inherited,
            budget_authority=budget_authority,
            budget_migration=budget_migration,
            termination_validation=final_projection.get("termination_validation"),
            legal_next_action=final_projection.get("legal_next_action"),
            outer_supervisory_loop={
                "status": "NON_TERMINAL_CONTINUATION_REQUIRED",
                "steps": supervisor_steps,
                "step_limit": self.supervisor_step_limit,
                "human_intervention_count": 0,
            },
        )

    @_boundary
    def start(self, **kwargs: Any) -> dict[str, Any]:
        # Reuse the existing requirements normalization/intake, without its
        # legacy checkpoint/orchestrator. No second brief identity is created.
        from .workflow_runtime import _normalize_brief_input, _BRIEF_FIELDS
        workspace = kwargs.pop("workspace", kwargs.pop("workspace_root", str(self.root)))
        if Path(workspace).expanduser().resolve() != self.root:
            raise WorkflowRuntimeError("WORKSPACE_IDENTITY_MISMATCH", "intake workspace does not match")
        mode = kwargs.pop("mode", None)
        rough = None
        for key in ("rough_requirement", "requirement", "raw_requirement", "original_requirement", "user_requirement"):
            value = kwargs.pop(key, None)
            if rough is None:
                rough = value
        brief = kwargs.pop("brief", {})
        direct = {key: kwargs.pop(key) for key in tuple(kwargs) if key in _BRIEF_FIELDS}
        if kwargs or not isinstance(brief, Mapping):
            raise WorkflowRuntimeError("INPUT_INVALID", "unknown or invalid workflow_start fields")
        self.intake.initialize(mode=mode, rough_requirement=rough,
                               brief=_normalize_brief_input({**brief, **direct}), entrypoint="workflow_start")
        return self._view()

    @_boundary
    def answer(self, *, answer: Any = None, approve: bool = False, mode: str | None = None,
               question_id: str | None = None, update: Any = None, **kwargs: Any) -> dict[str, Any]:
        if not isinstance(approve, bool):
            raise WorkflowRuntimeError("INPUT_INVALID", "approve must be boolean")
        if isinstance(answer, Mapping) and "decision_id" in answer:
            if mode not in {None, "ANSWER"} or approve or update is not None:
                raise WorkflowRuntimeError("INPUT_INVALID", "Human decision must be an exclusive explicit answer")
            decision = self.controller.state["decisions"].get(answer["decision_id"])
            if decision is None or decision["actor_kind"] != "HUMAN":
                raise WorkflowRuntimeError("DECISION_INVALID", "no matching pending Human decision")
            stage = self.controller.resolve_canonical_stage(decision["subject_id"])
            decision_request = {
                "operation": "ANSWER",
                "command": "APPLY_DECISION",
                "subject_id": stage["stage_id"],
                "stage": stage,
            }
            self._last_contract_trace = self._build_contract_trace(
                action="APPLY_DECISION",
                request=decision_request,
                canonical_stage=stage,
                supervisor_request=decision_request,
            )
            result = self.controller.apply_decision(decision["subject_id"], payload=dict(answer))
            return self._view(decision_result=result, contract_propagation=self.last_contract_trace)
        if sum((answer is not None, update is not None, bool(approve or mode == "APPROVE"))) != 1:
            raise WorkflowRuntimeError("INPUT_INVALID", "answer, update and approve are exclusive")
        if approve or mode == "APPROVE":
            self.intake.approve(actor="workflow-user", rationale="explicit workflow approval")
        elif mode == "UPDATE" or update is not None:
            self.intake.update(update)
        elif mode == "ANSWER" or answer is not None:
            self.intake.answer(answer, question_id=question_id)
        else:
            raise WorkflowRuntimeError("INPUT_INVALID", "Product answer requires explicit intake answer, update or approval")
        return self._view()

    def _consult(self, request: Mapping[str, Any], *, purpose: str) -> dict[str, Any]:
        cfg = self.config
        pack = request.get("context_pack")
        prompt = request.get("prompt")
        if not isinstance(pack, Mapping) or not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowRuntimeError("PLANNING_INPUT_REQUIRED", "consultation requires a bounded prompt and context_pack")
        try:
            raw = subprocess_bridge_runner(prompt, mode="fresh", continue_from=None, context_pack=pack,
                    root_dir=str(self.root), profile_dir=cfg.bridge_profile_dir, bridge_root=cfg.bridge_root,
                    node_executable=cfg.node_executable, project_url=cfg.project_url, transport=cfg.bridge_transport,
                    timeout_ms=min(int(cfg.timeout_seconds * 1000), 300000))
        except StageIntegrationError as exc:
            raise WorkflowRuntimeError(exc.code, str(exc), details=exc.details) from exc
        checked = normalize_bridge_envelope(raw, expected_mode="fresh", require_receipt=True)
        receipt = checked["receipt"]
        if receipt.get("conversation_validated") is not True or checked.get("request_count") != 1:
            raise WorkflowRuntimeError("CONSULTATION_INVALID", "real fresh consultation identity was not validated")
        context = receipt.get("context_pack", {})
        if not context.get("pack_sha256"):
            raise WorkflowRuntimeError("CONSULTATION_INVALID", "real consultation lacks packet provenance")
        response = checked["response_text"]
        return {"purpose": purpose, "decision": parse_dialogue_decision(response),
                "response_digest": hashlib.sha256(response.encode()).hexdigest(),
                "consultation_id": checked["consultation_id"], "receipt_path": checked.get("receipt_path"),
                "request_count": checked["request_count"], "conversation_id": receipt.get("conversation_id"),
                "conversation_validated": True, "packet_digest": context["pack_sha256"],
                "objective_identity": request.get("objective_identity")}

    @_boundary
    def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise WorkflowRuntimeError("RUN_REQUEST_INVALID", "workflow_run requires a request object")
        self._last_contract_trace = None
        if self._transport_request_pending:
            self._transport_request_pending = False
        else:
            self._transport_request_snapshot = payload_snapshot(request)
        brief = self.intake.state
        if brief is None:
            raise WorkflowRuntimeError("WORKFLOW_NOT_FOUND", "workflow has not been started")
        if brief["state"] != "APPROVED":
            raise WorkflowRuntimeError("HUMAN_APPROVAL_REQUIRED", "approved project brief required")
        operation = request.get("operation", "PLAN_STAGE")
        if operation == "DOCTOR":
            return self._view(diagnostic=doctor_product_runtime(self.root, config=self.config))
        if operation == "PLAN_STAGE":
            diagnostic = doctor_product_runtime(self.root, config=self.config)
            if not diagnostic["ready"]:
                raise WorkflowRuntimeError("RUNNER_NOT_CONFIGURED", "Product dependencies are not ready", details=diagnostic)
            request, stage_info = self._resolve_stage_request(request, "PLAN_STAGE")
            stage = stage_info["canonical_stage"]
            if stage["workspace_id"] != self.controller.workspace_id or stage["project_id"] != brief["project_id"]:
                raise WorkflowRuntimeError("WORKSPACE_IDENTITY_MISMATCH", "planning proposal belongs to another workspace/project")
            if stage["status"] != "PLANNED":
                raise WorkflowRuntimeError("PLANNING_INPUT_INVALID", "planning proposal must be PLANNED")
            revision = request.get("planning_revision", 1)
            if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 32:
                raise WorkflowRuntimeError("PLANNING_INPUT_INVALID", "planning_revision must be an explicit bounded positive integer")
            digest = sha256_json({"brief": brief, "stage": stage, "prompt": request.get("prompt"), "context_pack": request.get("context_pack"), "planning_revision": revision})
            directory = self._artifact_path(
                "planning_record",
                relative_path=".research/planning/" + hashlib.sha256(stage["stage_id"].encode()).hexdigest()[:24] + "/directory.marker",
                source_operation="PLAN_STAGE",
            ).parent
            if revision > 1:
                # Only explicit caller-selected artifact revisions; never an
                # automatic retry of an uncertain external consultation.
                directory = directory / ("revision-" + str(revision))
            intent = directory / "request.json"
            result_path = directory / "receipt.json"
            recovery_marker = directory / "transport-recovery.json"
            if result_path.exists():
                result = _read_receipt(result_path, digest)
            else:
                recovery = request.get("transport_recovery")
                if intent.exists():
                    prior_intent = json.loads(intent.read_text(encoding="utf-8"))
                    if not isinstance(prior_intent, Mapping) or prior_intent.get("input_digest") != digest:
                        raise WorkflowRuntimeError("PLANNING_INPUT_CHANGED", "persisted planning intent binds different input")
                    if recovery is None:
                        raise WorkflowRuntimeError("CONSULTATION_EFFECT_UNRESOLVED", "prior planning intent has no validated result; inspect bridge receipt before any further request")
                    recovery = _validate_transport_recovery(
                        self.root,
                        recovery,
                        input_digest=digest,
                        stage_id=stage["stage_id"],
                        planning_revision=revision,
                    )
                    if recovery_marker.exists():
                        raise WorkflowRuntimeError("TRANSPORT_RECOVERY_ALREADY_USED", "bounded planning transport recovery was already consumed")
                    _immutable(recovery_marker, recovery)
                else:
                    _immutable(intent, {"input_digest": digest, "stage_id": stage["stage_id"],
                                        "timeout_ms": min(int(self.config.timeout_seconds * 1000), 300000)})
                result = _seal({"input_digest": digest, **self._consult(request, purpose="STAGE_PLANNING")})
                _immutable(result_path, result)
            if result["decision"] != "CONTINUE":
                return self._view(planning=result, WORKFLOW_DECISION=result["decision"], stage_created=False,
                                  contract_propagation=self.last_contract_trace)
            self.controller.initialize()
            if stage["stage_id"] not in self.controller.state["stages"]:
                self.controller.register_stage(stage, command_id="command-plan-" + digest[:32])
            return self._view(planning=result, WORKFLOW_DECISION=result["decision"], stage_created=True,
                              stage_started=False, runner_ready=True,
                              contract_propagation=self.last_contract_trace)
        if operation == "CONSULT_REVIEW":
            stage = self.controller.show_stage(request.get("stage_id"))
            assessment_id = stage.get("current_assessment_id")
            if not assessment_id:
                raise WorkflowRuntimeError("REVIEW_NOT_READY", "technical review requires current admissible assessment")
            assessment = self.controller.state["assessments"][assessment_id]
            objective_identity = assessment.get("objective_identity")
            if objective_identity is None and assessment.get("verdict") != "ADMISSIBLE":
                raise WorkflowRuntimeError("REVIEW_NOT_READY", "technical review requires current admissible assessment")
            if objective_identity is not None:
                pack = request.get("context_pack")
                if not isinstance(pack, Mapping):
                    raise WorkflowRuntimeError("GPT_PACKET_OBJECTIVE_BINDING_REQUIRED", "technical review packet must be an object")
                observation_id = next(
                    (
                        item.get("observation_id")
                        for item in self.controller.state.get("observations", {}).values()
                        if item.get("stage_id") == stage["stage_id"]
                        and item.get("attempt_id") == assessment.get("attempt_id")
                        and item.get("provider_result_digest") == assessment.get("provider_result_digest")
                    ),
                    None,
                )
                required_binding = {
                    "PROJECT_ID": stage["project_id"],
                    "STAGE_ID": stage["stage_id"],
                    "OBJECTIVE_IDENTITY": objective_identity,
                    "STAGE_GOAL": pack.get("STAGE_GOAL") or request.get("stage_goal") or request.get("STAGE_GOAL"),
                    "ASSESSMENT_ID": assessment_id,
                    "OBSERVATION_ID": observation_id,
                }
                missing = [field for field, value in required_binding.items() if not isinstance(value, str) or not value]
                if missing:
                    raise WorkflowRuntimeError("GPT_PACKET_OBJECTIVE_BINDING_REQUIRED", "technical review packet is missing: " + ", ".join(missing))
                mismatches = [field for field, value in required_binding.items() if pack.get(field) != value]
                if mismatches:
                    raise WorkflowRuntimeError("GPT_PACKET_OBJECTIVE_MISMATCH", "technical review packet binding mismatch: " + ", ".join(mismatches))
                # The bridge context-pack builder intentionally whitelists its
                # public fields and drops unknown top-level extensions.  Keep
                # the machine-readable binding in the adapter contract while
                # also projecting the exact identity into a generated,
                # GPT-visible source context entry.
                binding_excerpt = "\n".join([
                    f"PROJECT_ID: {required_binding['PROJECT_ID']}",
                    f"STAGE_ID: {required_binding['STAGE_ID']}",
                    f"OBJECTIVE_IDENTITY: {required_binding['OBJECTIVE_IDENTITY']}",
                    f"STAGE_GOAL: {required_binding['STAGE_GOAL']}",
                    f"ASSESSMENT_ID: {required_binding['ASSESSMENT_ID']}",
                    f"OBSERVATION_ID: {required_binding['OBSERVATION_ID']}",
                ])
                bound_pack = copy.deepcopy(dict(pack))
                prior_source_context = bound_pack.get("sourceContext")
                source_context = list(prior_source_context) if isinstance(prior_source_context, list) else ([] if prior_source_context is None else [prior_source_context])
                source_context.append({
                    "filePath": "OBJECTIVE_BINDING.md",
                    "relevantFunctions": [],
                    "excerpt": binding_excerpt,
                    "whyRelevant": "Canonical objective binding for this technical review; do not infer it from natural-language context.",
                })
                bound_pack["sourceContext"] = source_context
                request = {**request, "context_pack": bound_pack, "objective_identity": objective_identity}
            revision = request.get("review_revision", 1)
            if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 32:
                raise WorkflowRuntimeError("REVIEW_INPUT_INVALID", "review_revision must be an explicit bounded positive integer")
            digest = sha256_json({"assessment_id": assessment_id, "prompt": request.get("prompt"), "context_pack": request.get("context_pack")})
            directory = self._artifact_path(
                "review_record",
                relative_path=".research/reviews/" + hashlib.sha256(assessment_id.encode()).hexdigest()[:24] + "/directory.marker",
                source_operation="CONSULT_REVIEW",
            ).parent
            if revision > 1:
                # An unresolved transport intent is never overwritten or
                # retried in place. A caller may submit a deliberately revised
                # packet after correcting a local preflight failure.
                directory = directory / ("revision-" + str(revision))
            intent, receipt_path = directory / "request.json", directory / "receipt.json"
            if receipt_path.exists():
                result = _read_receipt(receipt_path, digest)
            else:
                if intent.exists():
                    raise WorkflowRuntimeError("CONSULTATION_EFFECT_UNRESOLVED", "prior review intent has no result; inspect external receipt")
                _immutable(intent, {"input_digest": digest, "assessment_id": assessment_id, "review_revision": revision})
                result = _seal({"input_digest": digest, **self._consult(request, purpose="TECHNICAL_REVIEW")})
                _immutable(receipt_path, result)
            return self._view(technical_review=result, WORKFLOW_DECISION=result["decision"])
        # Public commands remain validated and reduced by the frozen authority.
        # Registration cannot bypass real planning; Human decisions have a
        # dedicated resolver and are not accepted as a run side effect.
        command = request.get("command")
        if operation == "COMMAND":
            command_name = str(command or "").upper()
            wire_command = _COMMAND_WIRE_NAMES.get(command_name, command_name)
            if wire_command in PUBLIC_COMMANDS and wire_command not in {"REGISTER_STAGE", "APPLY_DECISION"}:
                normalized_request = request
                propagation = None
                if wire_command in STAGE_SCOPED_COMMANDS:
                    normalized_request, info = self._resolve_stage_request(request, wire_command)
                    propagation = info.get("trace")
                payload = copy.deepcopy(dict(normalized_request.get("payload", {})))
                command_id = normalized_request.get("command_id")
                if wire_command == "REQUEST_EXECUTION" and "provider_handoff_manifest" not in payload:
                    if not isinstance(command_id, str) or not command_id:
                        command_id = "command-" + sha256_json({"command": wire_command, "subject_id": normalized_request.get("subject_id"), "payload": payload})[:32]
                    stage = normalized_request["stage"]
                    request_body = payload.get("request", {})
                    request_id = payload.get("request_id") or (command_id if command_id.startswith("request-") else "request-" + command_id)
                    attempt_id = payload.get("attempt_id")
                    if attempt_id is None and str(payload.get("reason", "")).upper() == "OBJECTIVE_SUPERSESSION_RECOVERY":
                        epoch_id = self.controller.state.get("stage_runtime", {}).get(stage["stage_id"], {}).get("current_assessment_epoch_id")
                        attempt_id = next(
                            (
                                item.get("new_attempt_id")
                                for item in self.controller.state.get("assessment_supersessions", {}).values()
                                if item.get("stage_id") == stage["stage_id"] and item.get("new_assessment_epoch_id") == epoch_id
                            ),
                            None,
                        )
                    attempt_id = attempt_id or (command_id if command_id.startswith("attempt-") else "attempt-" + command_id)
                    operation_id = payload.get("operation_id") or (command_id if command_id.startswith("operation-") else "operation-" + command_id)
                    payload["provider_handoff_manifest"] = build_provider_handoff_manifest(
                        operation_id=operation_id,
                        stage_id=stage["stage_id"],
                        iteration_id=stage["current_iteration_id"],
                        attempt_id=attempt_id,
                        request_id=request_id,
                        request=request_body,
                        provenance=payload.get("provenance", {"provider": "fixture-provider", "engine_digest": "engine-fixed-v2"}),
                        purpose=payload.get("purpose", stage["purpose"]),
                        project_id=stage["project_id"],
                        objective_identity=stage["objective_fingerprint"],
                    )
                result = self.controller.dispatch(wire_command, subject_id=normalized_request.get("subject_id"),
                           payload=payload, command_id=command_id,
                           expected_revision=normalized_request.get("expected_revision"))
                return self._view(command_result=result, contract_propagation=propagation or self.last_contract_trace)
        raise WorkflowRuntimeError("RUN_REQUEST_INVALID", "Product run requires PLAN_STAGE, DOCTOR or an allowed canonical command")
