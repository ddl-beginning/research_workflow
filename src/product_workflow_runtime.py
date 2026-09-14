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
from functools import wraps
from pathlib import Path
from typing import Any, Mapping

from .bridge_adapter import BridgeEnvelopeError, normalize_bridge_envelope
from .artifact_resolver import ArtifactResolver, ArtifactResolverError
from .contracts import ContractValidationError, canonical_json, sha256_json
from .openai_codex_executor import OpenAICodexExecutor
from .project_intake import ProjectIntakeError, ProjectRequirementsIntake
from .runtime_composition import RuntimeCompositionConfig, load_runtime_composition_config
from .stage_integration import StageIntegrationError, parse_dialogue_decision, subprocess_bridge_runner
from .workflow_runtime import WorkflowRuntimeError
from .human_summary import build_human_presentation
from .workflow_v2_contracts import PUBLIC_COMMANDS, validate_stage
from .workflow_v2_controller import StageController, WorkflowV2ControllerError


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

    def __init__(self, workspace: str | Path, *, config: RuntimeCompositionConfig) -> None:
        self.root = Path(workspace).expanduser().resolve(strict=True)
        self.config = config
        self.intake = ProjectRequirementsIntake(self.root)
        self.controller = _controller(self.root, config)

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
        presentation = build_human_presentation(
            metadata=extra,
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
                    "question": question, "brief_state": brief["state"], "canonical": projection, **extra}
        return {"human_summary": presentation["human_summary"],
                "machine_details": presentation["machine_details"],
                "presentation": presentation,
                "schema_version": "product_workflow_result.v1", "lifecycle_version": "v2",
                "workspace_root": str(self.root), "project_id": brief["project_id"],
                "brief_state": brief["state"], **projection, "next_tool": "workflow_answer" if projection["next_actor"] == "Human" else "workflow_run",
                "question_id": None, **extra}

    def status(self) -> dict[str, Any]:
        return self._view()

    def resume(self) -> dict[str, Any]:
        return self._view()

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
            result = self.controller.apply_decision(decision["subject_id"], payload=dict(answer))
            return self._view(decision_result=result)
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
                "conversation_validated": True, "packet_digest": context["pack_sha256"]}

    @_boundary
    def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(request, Mapping):
            raise WorkflowRuntimeError("RUN_REQUEST_INVALID", "workflow_run requires a request object")
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
            stage = validate_stage(request.get("stage", {}))
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
                return self._view(planning=result, WORKFLOW_DECISION=result["decision"], stage_created=False)
            self.controller.initialize()
            if stage["stage_id"] not in self.controller.state["stages"]:
                self.controller.register_stage(stage, command_id="command-plan-" + digest[:32])
            return self._view(planning=result, WORKFLOW_DECISION=result["decision"], stage_created=True,
                              stage_started=False, runner_ready=True)
        if operation == "CONSULT_REVIEW":
            stage = self.controller.show_stage(request.get("stage_id"))
            assessment_id = stage.get("current_assessment_id")
            if not assessment_id or self.controller.state["assessments"][assessment_id]["verdict"] != "ADMISSIBLE":
                raise WorkflowRuntimeError("REVIEW_NOT_READY", "technical review requires current admissible assessment")
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
        if operation == "COMMAND" and command in PUBLIC_COMMANDS and command not in {"REGISTER_STAGE", "APPLY_DECISION"}:
            result = self.controller.dispatch(command, subject_id=request.get("subject_id"),
                       payload=request.get("payload", {}), command_id=request.get("command_id"),
                       expected_revision=request.get("expected_revision"))
            return self._view(command_result=result)
        raise WorkflowRuntimeError("RUN_REQUEST_INVALID", "Product run requires PLAN_STAGE, DOCTOR or an allowed canonical command")
