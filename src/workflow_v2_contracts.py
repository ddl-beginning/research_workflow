"""Pure Workflow V2 lifecycle contracts.

This module is deliberately free of persistence, provider dispatch, and
lifecycle mutation.  It defines the immutable records and validation rules
that the StageController reducer will consume in later phases.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    ContractValidationError,
    _ensure_relative_path,
    canonical_json,
    sha256_json,
    validate_instance,
)


V2_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas" / "workflow_v2"

DOMAIN_SCHEMA_ROOTS = (
    "stage",
    "semantic_iteration",
    "execution_attempt",
    "provider_observation",
    "stage_assessment",
    "decision",
    "dependency",
)
SHARED_SUPPORT_SCHEMA_ROOTS = ("command_envelope", "operation_envelope", "evidence_manifest")
STAGE_STATES = ("PLANNED", "ACTIVE", "READY", "CLOSED", "STOPPED")
PUBLIC_COMMANDS = (
    "REGISTER_STAGE",
    "START",
    "REQUEST_EXECUTION",
    "RECORD_OBSERVATION",
    "ASSESS_RESULT",
    "APPLY_GPT_DECISION",
    "ADVANCE_ITERATION",
    "REQUEST_DECISION",
    "APPLY_DECISION",
    "ADD_DEPENDENCY",
    "SATISFY_DEPENDENCY",
    "RESOLVE_BLOCKER",
    "APPLY_RECEIPT",
    "COMMIT_INTEGRATION",
    "CLOSEOUT",
    "STOP",
)
FAILURE_CLASSES = (
    "ENGINEERING_FAILURE",
    "PROVIDER_FAILURE",
    "TRANSPORT_FAILURE",
    "SEMANTIC_FAILURE",
    "AUTHORITY_FAILURE",
    "EXTERNAL_BLOCKER",
)
RETRYABILITY = ("RETRYABLE", "NON_RETRYABLE", "UNKNOWN")
EFFECT_STATES = (
    "INTENT_COMMITTED",
    "NOT_SENT_PROVEN",
    "SENT_UNSETTLED",
    "UNKNOWN",
    "RECEIPT_OBSERVED",
    "SETTLED",
    "CONFLICT",
)
REPLAN_SUBTYPES = ("ENGINEERING_FIX", "NEXT_ITERATION", "BASELINE_CHANGE")
DECISION_ACTORS = ("HUMAN", "GPT")
DECISION_BOUNDARIES = ("REQUIREMENT", "DESIGN", "STAGE_PLANNING", "TECHNICAL_REVIEW")
ASSESSMENT_VERDICTS = ("ADMISSIBLE", "REJECTED", "INSUFFICIENT")

_TYPED_FAILURE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "failure_class", "code", "operation_id", "retryability", "effect_state", "evidence_refs", "authority"],
    "properties": {
        "schema_version": {"const": "typed_failure.v2"},
        "failure_class": {"type": "string", "enum": list(FAILURE_CLASSES)},
        "code": {"type": "string", "minLength": 1},
        "operation_id": {"type": "string", "minLength": 8},
        "retryability": {"type": "string", "enum": list(RETRYABILITY)},
        "effect_state": {"type": "string", "enum": list(EFFECT_STATES)},
        "evidence_refs": {"type": "array", "items": {"type": "string", "minLength": 8}},
        "authority": {"type": "string", "const": "CONTROLLER_POLICY"},
    },
}

_CORRECTION_RECEIPT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version", "correction_id", "old_validator_code_digest", "new_validator_code_digest",
        "bug_evidence_digest", "unchanged_contract_digest", "review_identity", "human_decision_id",
        "subject_type", "scope_expansion", "approved",
    ],
    "properties": {
        "schema_version": {"const": "correction_receipt.v2"},
        "correction_id": {"type": "string", "minLength": 8},
        "old_validator_code_digest": {"type": "string", "minLength": 8},
        "new_validator_code_digest": {"type": "string", "minLength": 8},
        "bug_evidence_digest": {"type": "string", "minLength": 8},
        "unchanged_contract_digest": {"type": "string", "minLength": 8},
        "review_identity": {"type": "string", "minLength": 8},
        "human_decision_id": {"type": "string", "minLength": 8},
        "subject_type": {"const": "VALIDATOR_CORRECTION"},
        "scope_expansion": {"type": "boolean", "const": False},
        "approved": {"type": "boolean"},
    },
}


def _fail(field: str, message: str) -> None:
    raise ContractValidationError(f"{field}: {message}")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        _fail(field, "must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _int(value: Any, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        _fail(field, f"must be an integer >= {minimum}")
    return value


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        _fail(field, "must be a boolean")
    return value


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _fail(field, "must be an object")
    return copy.deepcopy(dict(value))


def _list(value: Any, field: str, *, minimum: int = 0) -> list[Any]:
    if not isinstance(value, list) or len(value) < minimum:
        _fail(field, f"must be an array with at least {minimum} item(s)")
    return copy.deepcopy(value)


def _one_of(value: Any, field: str, choices: Sequence[str]) -> str:
    checked = _text(value, field).upper()
    if checked not in choices:
        _fail(field, f"must be one of {tuple(choices)!r}")
    return checked


def _digest(value: Any, field: str) -> str:
    checked = _text(value, field)
    if not re.fullmatch(r"[A-Za-z0-9_.:/-]{8,256}", checked):
        _fail(field, "must be a stable digest/identity token")
    return checked


def load_v2_schema(name: str) -> dict[str, Any]:
    """Load one of the ten bounded V2 root schemas."""

    filename = name if name.endswith(".json") else f"{name}.schema.json"
    path = (V2_SCHEMA_DIR / filename).resolve()
    try:
        path.relative_to(V2_SCHEMA_DIR.resolve())
    except ValueError as exc:
        raise ContractValidationError(f"schema path escapes V2 schema directory: {name}") from exc
    if not path.is_file():
        raise ContractValidationError(f"V2 schema not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"cannot load V2 schema {path}: {exc}") from exc
    if not isinstance(payload, dict):
        _fail(name, "schema root must be an object")
    return payload


def _schema(payload: Mapping[str, Any], root: str) -> dict[str, Any]:
    detached = copy.deepcopy(dict(payload))
    validate_instance(detached, load_v2_schema(root))
    return detached


def validate_budget(value: Mapping[str, Any]) -> dict[str, int]:
    budget = _mapping(value, "budgets")
    fields = (
        "max_iterations",
        "max_attempts_per_iteration",
        "max_attempts_total",
        "max_revalidation_ops",
        "max_validator_revisions",
        "max_dependency_nodes",
        "max_dependency_depth",
        "max_descendant_attempts",
    )
    for field in fields:
        _int(budget.get(field), f"budgets.{field}")
    if budget["max_attempts_total"] < budget["max_attempts_per_iteration"]:
        _fail("budgets.max_attempts_total", "must cover at least one iteration budget")
    return {field: budget[field] for field in fields}


def validate_stage(value: Mapping[str, Any]) -> dict[str, Any]:
    stage = _schema(value, "stage")
    _one_of(stage["status"], "status", STAGE_STATES)
    _text(stage["stage_id"], "stage_id")
    _text(stage["workspace_id"], "workspace_id")
    _text(stage["project_id"], "project_id")
    _digest(stage["objective_fingerprint"], "objective_fingerprint")
    _text(stage["target_identity"], "target_identity")
    _one_of(stage["purpose"], "purpose", ("DOMAIN", "INFRASTRUCTURE_REPAIR"))
    repair_depth = _int(stage["repair_depth"], "repair_depth")
    if repair_depth > 1:
        _fail("repair_depth", "root infrastructure repair is bounded to depth 1")
    if repair_depth == 1 and stage["purpose"] != "INFRASTRUCTURE_REPAIR":
        _fail("purpose", "repair_depth=1 requires INFRASTRUCTURE_REPAIR")
    if repair_depth == 0 and stage["purpose"] == "INFRASTRUCTURE_REPAIR":
        _fail("repair_depth", "infrastructure repair must identify an ancestor")
    validate_budget(stage["budgets"])
    if stage["purpose"] == "INFRASTRUCTURE_REPAIR" and not stage.get("predecessor_stage_id"):
        _fail("predecessor_stage_id", "required for infrastructure repair")
    if stage.get("predecessor_stage_id") is not None:
        _text(stage["predecessor_stage_id"], "predecessor_stage_id")
        _digest(stage["semantic_baseline_diff_digest"], "semantic_baseline_diff_digest")
    return stage


def derive_objective_fingerprint(
    *, project_goal: str, acceptance_criteria: Sequence[str], target_identity: str, required_capability: str
) -> str:
    """Derive the objective identity independently of Stage id or wording."""

    body = {
        "project_goal": _text(project_goal, "project_goal"),
        "acceptance_criteria": sorted(_text(item, "acceptance_criteria") for item in acceptance_criteria),
        "target_identity": _text(target_identity, "target_identity"),
        "required_capability": _text(required_capability, "required_capability"),
    }
    return "objective-" + sha256_json(body)


def validate_semantic_iteration(value: Mapping[str, Any]) -> dict[str, Any]:
    iteration = _schema(value, "semantic_iteration")
    _text(iteration["iteration_id"], "iteration_id")
    _text(iteration["stage_id"], "stage_id")
    _int(iteration["index"], "index", minimum=1)
    _digest(iteration["solution_fingerprint"], "solution_fingerprint")
    _one_of(iteration["opened_by"], "opened_by", ("START", "ADVANCE_ITERATION"))
    if iteration["index"] == 1:
        if iteration["opened_by"] != "START" or iteration["requires_next_iteration"]:
            _fail("iteration", "first iteration must be opened atomically by START")
    else:
        if iteration["opened_by"] != "ADVANCE_ITERATION" or not iteration["requires_next_iteration"]:
            _fail("iteration", "later iterations require an explicit semantic review")
        _digest(iteration["review_identity"], "review_identity")
        _digest(iteration["technical_change_digest"], "technical_change_digest")
    return iteration


def validate_execution_attempt(value: Mapping[str, Any]) -> dict[str, Any]:
    attempt = _schema(value, "execution_attempt")
    for field in ("attempt_id", "stage_id", "iteration_id", "request_id", "request_digest"):
        _digest(attempt[field], field)
    _one_of(attempt["purpose"], "purpose", ("DOMAIN", "INFRASTRUCTURE_REPAIR"))
    _one_of(attempt["effect_state"], "effect_state", EFFECT_STATES)
    _int(attempt["attempt_index"], "attempt_index", minimum=1)
    _bool(attempt["committed"], "committed")
    provenance = _mapping(attempt["provenance"], "provenance")
    _text(provenance.get("provider"), "provenance.provider")
    _text(provenance.get("engine_digest"), "provenance.engine_digest")
    return attempt


def validate_typed_failure(value: Mapping[str, Any]) -> dict[str, Any]:
    failure = copy.deepcopy(dict(value))
    validate_instance(failure, _TYPED_FAILURE_SCHEMA)
    _one_of(failure["failure_class"], "failure_class", FAILURE_CLASSES)
    _text(failure["code"], "code")
    _digest(failure["operation_id"], "operation_id")
    _one_of(failure["retryability"], "retryability", RETRYABILITY)
    _one_of(failure["effect_state"], "effect_state", EFFECT_STATES)
    refs = _list(failure["evidence_refs"], "evidence_refs")
    for index, ref in enumerate(refs):
        _digest(ref, f"evidence_refs[{index}]")
    if failure["authority"] != "CONTROLLER_POLICY":
        _fail("authority", "provider raw claims cannot author transition-control fields")
    return failure


def validate_provider_observation(value: Mapping[str, Any]) -> dict[str, Any]:
    observation = _schema(value, "provider_observation")
    for field in (
        "observation_id",
        "stage_id",
        "iteration_id",
        "attempt_id",
        "provider_result_digest",
        "evidence_manifest_digest",
    ):
        _digest(observation[field], field)
    _one_of(observation["provider_terminal_status"], "provider_terminal_status", ("SUCCEEDED", "FAILED", "ERROR", "UNKNOWN"))
    _mapping(observation["raw_provider_claim"], "raw_provider_claim")
    _mapping(observation["provenance"], "provenance")
    if observation["failure"] is not None:
        validate_typed_failure(observation["failure"])
    if observation["immutable"] is not True:
        _fail("immutable", "provider observations are append-only historical facts")
    if observation["observation_id"] != observation_identity(observation):
        _fail("observation_id", "must bind the complete immutable observation identity")
    return observation


def _observation_identity_body(observation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(observation[key])
        for key in (
            "stage_id", "iteration_id", "attempt_id", "provider_result_digest",
            "evidence_manifest_digest", "provider_terminal_status", "raw_provider_claim",
            "provenance", "failure", "outputs",
        )
    }


def observation_identity(observation: Mapping[str, Any]) -> str:
    return "observation-" + sha256_json(_observation_identity_body(observation))


def assessment_identity(
    *,
    stage_id: str,
    baseline_digest: str,
    iteration_id: str,
    attempt_id: str,
    provider_result_digest: str,
    evidence_manifest_digest: str,
    validator_code_digest: str,
    validation_contract_revision: str,
    correction_receipt_digest: str | None,
) -> str:
    tuple_body = {
        "stage_id": _text(stage_id, "stage_id"),
        "baseline_digest": _digest(baseline_digest, "baseline_digest"),
        "iteration_id": _digest(iteration_id, "iteration_id"),
        "attempt_id": _digest(attempt_id, "attempt_id"),
        "provider_result_digest": _digest(provider_result_digest, "provider_result_digest"),
        "evidence_manifest_digest": _digest(evidence_manifest_digest, "evidence_manifest_digest"),
        "validator_code_digest": _digest(validator_code_digest, "validator_code_digest"),
        "validation_contract_revision": _text(validation_contract_revision, "validation_contract_revision"),
        "correction_receipt_digest": correction_receipt_digest,
    }
    if correction_receipt_digest is not None:
        _digest(correction_receipt_digest, "correction_receipt_digest")
    return "assessment-" + sha256_json(tuple_body)


def validate_correction_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    receipt = copy.deepcopy(dict(value))
    validate_instance(receipt, _CORRECTION_RECEIPT_SCHEMA)
    for field in (
        "correction_id", "old_validator_code_digest", "new_validator_code_digest",
        "bug_evidence_digest", "unchanged_contract_digest", "review_identity", "human_decision_id",
    ):
        _digest(receipt[field], field)
    if receipt["old_validator_code_digest"] == receipt["new_validator_code_digest"]:
        _fail("new_validator_code_digest", "must differ from the old validator revision")
    if receipt["scope_expansion"]:
        _fail("scope_expansion", "scope/acceptance changes are a baseline change, not correction")
    if receipt["approved"] is not True:
        _fail("approved", "correction needs an accepted VALIDATOR_CORRECTION decision")
    if receipt["subject_type"] != "VALIDATOR_CORRECTION":
        _fail("subject_type", "correction must use the shared Technical Review decision subject")
    return receipt


def validate_stage_assessment(value: Mapping[str, Any]) -> dict[str, Any]:
    assessment = _schema(value, "stage_assessment")
    expected = assessment_identity(
        stage_id=assessment["stage_id"],
        baseline_digest=assessment["baseline_digest"],
        iteration_id=assessment["iteration_id"],
        attempt_id=assessment["attempt_id"],
        provider_result_digest=assessment["provider_result_digest"],
        evidence_manifest_digest=assessment["evidence_manifest_digest"],
        validator_code_digest=assessment["validator_code_digest"],
        validation_contract_revision=assessment["validation_contract_revision"],
        correction_receipt_digest=assessment["correction_receipt_digest"],
    )
    if assessment["assessment_id"] != expected:
        _fail("assessment_id", "must be the hash of the complete immutable assessment tuple")
    _one_of(assessment["verdict"], "verdict", ASSESSMENT_VERDICTS)
    _mapping(assessment["checks"], "checks")
    _list(assessment["limitations"], "limitations")
    if assessment["revalidation"] and assessment["correction_receipt_digest"] is None:
        _fail("correction_receipt_digest", "required for revalidation")
    return assessment


def assess_observation(
    observation: Mapping[str, Any],
    evidence_manifest: Mapping[str, Any],
    *,
    baseline_digest: str,
    validator_code_digest: str,
    validation_contract_revision: str,
    correction_receipt: Mapping[str, Any] | None = None,
    revalidation: bool = False,
    supersedes_assessment_id: str | None = None,
    checks: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assess preserved evidence without executing or rewriting its provider result."""

    observed = validate_provider_observation(observation)
    manifest = validate_evidence_manifest(evidence_manifest)
    if manifest["manifest_id"] != observed["evidence_manifest_digest"]:
        _fail("evidence_manifest", "manifest identity does not match the observation")
    correction_digest = None
    if correction_receipt is not None:
        correction = validate_correction_receipt(correction_receipt)
        if correction["new_validator_code_digest"] != validator_code_digest:
            _fail("validator_code_digest", "does not match the approved correction receipt")
        correction_digest = sha256_json(correction)
    if revalidation and correction_digest is None:
        _fail("correction_receipt", "revalidation requires an approved correction receipt")
    if supersedes_assessment_id is not None:
        _digest(supersedes_assessment_id, "supersedes_assessment_id")
        if not revalidation:
            _fail("supersedes_assessment_id", "only a revalidation may supersede an assessment")
    if not manifest["complete"]:
        verdict = "INSUFFICIENT"
    elif observed["provider_terminal_status"] == "SUCCEEDED":
        verdict = "ADMISSIBLE"
    elif revalidation and correction_digest is not None:
        verdict = "ADMISSIBLE"
    else:
        verdict = "REJECTED"
    assessment = {
        "schema_version": "stage_assessment.v2",
        "assessment_id": assessment_identity(
            stage_id=observed["stage_id"],
            baseline_digest=baseline_digest,
            iteration_id=observed["iteration_id"],
            attempt_id=observed["attempt_id"],
            provider_result_digest=observed["provider_result_digest"],
            evidence_manifest_digest=observed["evidence_manifest_digest"],
            validator_code_digest=validator_code_digest,
            validation_contract_revision=validation_contract_revision,
            correction_receipt_digest=correction_digest,
        ),
        "stage_id": observed["stage_id"],
        "baseline_digest": baseline_digest,
        "iteration_id": observed["iteration_id"],
        "attempt_id": observed["attempt_id"],
        "provider_result_digest": observed["provider_result_digest"],
        "evidence_manifest_digest": observed["evidence_manifest_digest"],
        "validator_code_digest": validator_code_digest,
        "validation_contract_revision": validation_contract_revision,
        "correction_receipt_digest": correction_digest,
        "verdict": verdict,
        "checks": copy.deepcopy(dict(checks or {"scope": "PASS", "evidence": "PASS" if manifest["complete"] else "MISSING"})),
        "limitations": [] if manifest["complete"] else ["immutable evidence manifest is incomplete"],
        "supersedes_assessment_id": supersedes_assessment_id,
        "revalidation": revalidation,
    }
    return validate_stage_assessment(assessment)


def validate_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    decision = _schema(value, "decision")
    _digest(decision["decision_id"], "decision_id")
    _one_of(decision["actor_kind"], "actor_kind", DECISION_ACTORS)
    _one_of(decision["boundary"], "boundary", DECISION_BOUNDARIES)
    _text(decision["subject_id"], "subject_id")
    _digest(decision["subject_digest"], "subject_digest")
    choices = _list(decision["allowed_choices"], "allowed_choices", minimum=1)
    if len(set(choices)) != len(choices) or not all(isinstance(item, str) and item for item in choices):
        _fail("allowed_choices", "must be unique non-empty strings")
    _text(decision["requested_action"], "requested_action")
    provenance = _mapping(decision["provenance"], "provenance")
    if decision["actor_kind"] == "GPT":
        _int(provenance.get("request_count"), "provenance.request_count", minimum=1)
        for field in ("conversation_id", "response_digest", "packet_digest"):
            _digest(provenance.get(field), f"provenance.{field}")
    else:
        if provenance.get("source") not in ("human_receipt", "human_instruction"):
            _fail("provenance.source", "Human decisions require a sourced receipt/instruction")
        _digest(provenance.get("receipt_digest"), "provenance.receipt_digest")
    if decision.get("subject_type") == "VALIDATOR_CORRECTION" and (
        decision["actor_kind"] != "HUMAN" or decision["boundary"] != "TECHNICAL_REVIEW"
    ):
        _fail("subject_type", "VALIDATOR_CORRECTION requires a Human Technical Review decision")
    _int(decision["subject_version"], "subject_version", minimum=0)
    return decision


def validate_decision_subject(
    decision: Mapping[str, Any], *, subject_id: str, subject_digest: str, subject_version: int
) -> dict[str, Any]:
    checked = validate_decision(decision)
    if (checked["subject_id"], checked["subject_digest"], checked["subject_version"]) != (
        subject_id, subject_digest, subject_version
    ):
        _fail("decision.subject", "stale subject/version cannot be applied")
    return checked


def validate_typed_replan(decision: Mapping[str, Any], subtype: str) -> dict[str, Any]:
    checked = validate_decision(decision)
    if checked["actor_kind"] != "GPT" or checked["boundary"] != "TECHNICAL_REVIEW":
        _fail("decision", "REPLAN requires a subject-bound GPT technical review")
    subtype = _one_of(subtype, "replan_subtype", REPLAN_SUBTYPES)
    if f"REPLAN:{subtype}" not in checked["allowed_choices"]:
        _fail("replan_subtype", "review does not authorize this exact typed REPLAN")
    return {**checked, "replan_subtype": subtype}


def validate_dependency(value: Mapping[str, Any]) -> dict[str, Any]:
    edge = _schema(value, "dependency")
    for field in ("parent_id", "child_id", "output_contract_digest", "authorization_decision_id"):
        _digest(edge[field], field)
    if edge["parent_id"] == edge["child_id"]:
        _fail("child_id", "self-dependency is forbidden")
    if edge["child_status"] != "PLANNED":
        _fail("child_status", "dependency child must be newly registered PLANNED")
    _text(edge["predicate"], "predicate")
    _mapping(edge["input_binding"], "input_binding")
    return edge


def dependency_authorization_digest(value: Mapping[str, Any]) -> str:
    """Bind a planning approval to the exact dependency edge being added."""

    edge = validate_dependency(value)
    body = {
        key: copy.deepcopy(edge[key])
        for key in (
            "parent_id", "child_id", "predicate", "input_binding",
            "output_contract_digest", "child_status",
        )
    }
    return "dependency-" + sha256_json(body)


def validate_dependency_graph(
    edges: Iterable[Mapping[str, Any]], *, max_nodes: int, max_depth: int
) -> list[dict[str, Any]]:
    checked = [validate_dependency(edge) for edge in edges]
    _int(max_nodes, "max_nodes")
    _int(max_depth, "max_depth")
    if len({edge["child_id"] for edge in checked}) != len(checked):
        _fail("dependency_graph", "a child may have only one owning parent")
    children: dict[str, list[str]] = {}
    nodes: set[str] = set()
    for edge in checked:
        children.setdefault(edge["parent_id"], []).append(edge["child_id"])
        nodes.update((edge["parent_id"], edge["child_id"]))
    if len(nodes) > max_nodes:
        _fail("dependency_graph", "dependency node budget exhausted")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, depth: int) -> None:
        if depth > max_depth:
            _fail("dependency_graph", "dependency depth budget exhausted")
        if node in visiting:
            _fail("dependency_graph", "dependency cycle detected")
        if node in visited:
            return
        visiting.add(node)
        for child in children.get(node, []):
            visit(child, depth + 1)
        visiting.remove(node)
        visited.add(node)

    for node in nodes:
        visit(node, 0)
    return checked


def validate_command_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    envelope = _schema(value, "command_envelope")
    _text(envelope["workspace_id"], "workspace_id")
    _digest(envelope["command_id"], "command_id")
    _int(envelope["expected_revision"], "expected_revision")
    _one_of(envelope["command_type"], "command_type", PUBLIC_COMMANDS)
    _text(envelope["subject_id"], "subject_id")
    _digest(envelope["payload_digest"], "payload_digest")
    return envelope


def validate_operation_envelope(value: Mapping[str, Any]) -> dict[str, Any]:
    envelope = _schema(value, "operation_envelope")
    for field in ("operation_id", "workspace_id", "subject_id", "intent_digest"):
        _digest(envelope[field], field)
    _one_of(envelope["effect_state"], "effect_state", EFFECT_STATES)
    capabilities = _mapping(envelope["capability_manifest"], "capability_manifest")
    for field in ("query_by_operation_id", "idempotent_submit", "fence", "prove_not_sent"):
        _bool(capabilities.get(field), f"capability_manifest.{field}")
    _one_of(envelope["status"], "status", ("INTENT", "RECEIPT_OBSERVED", "SETTLED", "BLOCKED"))
    if envelope["status"] == "SETTLED" and envelope["effect_state"] != "SETTLED":
        _fail("status", "SETTLED requires a proven SETTLED effect state")
    if envelope["effect_state"] in {"UNKNOWN", "CONFLICT"} and envelope["status"] != "BLOCKED":
        _fail("status", "UNKNOWN/CONFLICT effects are fail-closed blockers")
    return envelope


def validate_evidence_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    manifest = _schema(value, "evidence_manifest")
    for field in ("manifest_id", "stage_id", "attempt_id"):
        _digest(manifest[field], field)
    allowed = [_ensure_relative_path(item, "allowed_paths") for item in manifest["allowed_paths"]]
    protected = [_ensure_relative_path(item, "protected_paths") for item in manifest["protected_paths"]]
    changed = [_ensure_relative_path(item, "changed_paths") for item in manifest["changed_paths"]]
    required = [_ensure_relative_path(item, "required_artifact_paths") for item in manifest["required_artifact_paths"]]
    for path in changed:
        if not any(path == scope or path.startswith(scope.rstrip("/") + "/") for scope in allowed):
            _fail("changed_paths", f"path is outside allowed scope: {path}")
        if any(path == scope or path.startswith(scope.rstrip("/") + "/") for scope in protected):
            _fail("changed_paths", f"path intersects protected scope: {path}")
    if not all(any(path == scope or path.startswith(scope.rstrip("/") + "/") for scope in allowed) for path in required):
        _fail("required_artifact_paths", "required artifacts must be inside allowed scope")
    inventory_paths = {
        _ensure_relative_path(item.get("path"), "path_inventory.path")
        for item in manifest["path_inventory"]
        if isinstance(item, Mapping) and item.get("path") is not None
    }
    if manifest["complete"] and not set(required).issubset(inventory_paths):
        _fail("path_inventory", "complete evidence must inventory every required artifact")
    _bool(manifest["complete"], "complete")
    return {**manifest, "allowed_paths": allowed, "protected_paths": protected, "changed_paths": changed, "required_artifact_paths": required}


__all__ = [
    "ASSESSMENT_VERDICTS", "ContractValidationError", "DECISION_ACTORS", "DECISION_BOUNDARIES",
    "DOMAIN_SCHEMA_ROOTS", "EFFECT_STATES", "FAILURE_CLASSES", "PUBLIC_COMMANDS", "REPLAN_SUBTYPES",
    "SHARED_SUPPORT_SCHEMA_ROOTS", "STAGE_STATES", "V2_SCHEMA_DIR", "assessment_identity",
    "assess_observation", "derive_objective_fingerprint", "load_v2_schema", "observation_identity", "validate_budget",
    "validate_command_envelope", "validate_correction_receipt", "validate_decision",
    "validate_decision_subject", "validate_dependency", "dependency_authorization_digest", "validate_dependency_graph",
    "validate_evidence_manifest", "validate_execution_attempt", "validate_operation_envelope",
    "validate_provider_observation", "validate_semantic_iteration", "validate_stage",
    "validate_stage_assessment", "validate_typed_failure", "validate_typed_replan",
]
