"""Run the bounded Workflow V2 Phase 7 scenarios A-E.

The scenarios are validation fixtures only.  They create new journals under a
caller-selected evidence directory and exercise the existing V2 controller;
they do not import historical state or alter production lifecycle code.  B and
C use the same real fresh ChatGPT browser bridge as the happy-path check.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.phase7_real_self_host import (  # noqa: E402
    OBJECTIVE,
    consult_real,
    fresh_pack,
    sha256_bytes,
    sha256_json,
    write_json,
)
from src.contracts import ContractValidationError  # noqa: E402
from src.workflow_v2_contracts import (  # noqa: E402
    assess_observation,
    dependency_authorization_digest,
    observation_identity,
)
from src.workflow_v2_controller import StageController, WorkflowV2ControllerError  # noqa: E402


def scenario_stage(workspace_id: str, stage_id: str, *, max_iterations: int = 2) -> dict[str, Any]:
    return {
        "schema_version": "stage.v2",
        "stage_id": stage_id,
        "workspace_id": workspace_id,
        "project_id": "project-phase7-scenarios",
        "objective_fingerprint": sha256_json({"objective": OBJECTIVE, "stage": stage_id}),
        "target_identity": "bounded/scenario-fixture",
        "required_capabilities": ["python"],
        "purpose": "DOMAIN",
        "repair_depth": 0,
        "baseline_digest": "baseline-phase7-scenario-20260912",
        "status": "PLANNED",
        "budgets": {
            "max_iterations": max_iterations,
            "max_attempts_per_iteration": 2,
            "max_attempts_total": 4,
            "max_revalidation_ops": 2,
            "max_validator_revisions": 3,
            "max_dependency_nodes": 4,
            "max_dependency_depth": 2,
            "max_descendant_attempts": 4,
        },
        "owner_stage_id": None,
        "current_iteration_id": None,
        "current_assessment_id": None,
        "predecessor_stage_id": None,
        "semantic_baseline_diff_digest": None,
    }


def manifest(stage_id: str, attempt_id: str, name: str) -> dict[str, Any]:
    body = {
        "stage_id": stage_id,
        "attempt_id": attempt_id,
        "required_artifact_paths": ["evidence/result.json"],
        "changed_paths": ["evidence/result.json"],
        "allowed_paths": ["evidence"],
        "protected_paths": [".workflow-v2", "tests", "credentials"],
        "path_inventory": [{"path": "evidence/result.json", "sha256": f"artifact-{name}-20260912"}],
        "complete": True,
    }
    return {"schema_version": "evidence_manifest.v2", "manifest_id": f"manifest-{name}-20260912", **body}


def observation(
    *,
    stage_id: str,
    attempt: Mapping[str, Any],
    name: str,
    terminal: str,
    failure_class: str | None = None,
    effect_state: str = "SETTLED",
) -> dict[str, Any]:
    failure = None
    if terminal != "SUCCEEDED":
        failure = {
            "schema_version": "typed_failure.v2",
            "failure_class": failure_class or "ENGINEERING_FAILURE",
            "code": f"bounded-{name}-failure",
            "operation_id": attempt["attempt_id"].replace("attempt-", "operation-", 1),
            "retryability": "RETRYABLE" if failure_class != "EXTERNAL_BLOCKER" else "UNKNOWN",
            "effect_state": effect_state,
            "evidence_refs": [f"evidence-{name}-20260912"],
            "authority": "CONTROLLER_POLICY",
        }
    value = {
        "schema_version": "provider_observation.v2",
        "observation_id": "pending",
        "stage_id": stage_id,
        "iteration_id": attempt["iteration_id"],
        "attempt_id": attempt["attempt_id"],
        "provider_result_digest": f"provider-result-{name}-20260912",
        "evidence_manifest_digest": f"manifest-{name}-20260912",
        "provider_terminal_status": terminal,
        "raw_provider_claim": {"status": terminal, "provider": "bounded-scenario-fixture"},
        "provenance": {"provider": "bounded-scenario-fixture", "engine_digest": f"engine-{name}-20260912"},
        "failure": failure,
        "outputs": {"scenario": name},
        "immutable": True,
    }
    value["observation_id"] = observation_identity(value)
    return value


def assessment_for(obs: Mapping[str, Any], *, baseline: str, validator: str, name: str, **kwargs: Any) -> dict[str, Any]:
    return assess_observation(
        obs,
        manifest(obs["stage_id"], obs["attempt_id"], name),
        baseline_digest=baseline,
        validator_code_digest=validator,
        validation_contract_revision="admission.v2",
        checks={"scenario": name, "immutable_observation": "PASS"},
        **kwargs,
    )


def gpt_decision(
    assessment_id: str,
    *,
    decision_id: str,
    allowed_choices: list[str],
    response_digest: str,
    packet_digest: str,
) -> dict[str, Any]:
    return {
        "schema_version": "decision.v2",
        "decision_id": decision_id,
        "actor_kind": "GPT",
        "boundary": "TECHNICAL_REVIEW",
        "subject_id": assessment_id,
        "subject_digest": assessment_id,
        "subject_version": 1,
        "allowed_choices": allowed_choices,
        "requested_action": "Apply the fresh real GPT technical review to the current assessment.",
        "provenance": {
            "request_count": 1,
            "conversation_id": f"conversation-{decision_id}",
            "response_digest": response_digest,
            "packet_digest": packet_digest,
        },
        "supersedes": None,
    }


def human_decision(*, decision_id: str, subject_id: str, subject_type: str, subject_digest: str, choices: list[str]) -> dict[str, Any]:
    return {
        "schema_version": "decision.v2",
        "decision_id": decision_id,
        "actor_kind": "HUMAN",
        "boundary": "TECHNICAL_REVIEW" if subject_type == "VALIDATOR_CORRECTION" else "STAGE_PLANNING",
        "subject_id": subject_id,
        "subject_type": subject_type,
        "subject_digest": subject_digest,
        "subject_version": 1,
        "allowed_choices": choices,
        "requested_action": f"Human authorization for {subject_type.lower()}.",
        "provenance": {"source": "human_instruction", "receipt_digest": f"human-receipt-{decision_id}"},
        "supersedes": None,
    }


def setup_controller(root: Path, workspace_id: str, stage: Mapping[str, Any]) -> StageController:
    root.mkdir(parents=True, exist_ok=False)
    (root / ".workflow-v2").mkdir()
    controller = StageController(workspace_id=workspace_id, state_path=root / ".workflow-v2" / "journal.json")
    controller.initialize()
    controller.register_stage(stage, command_id=f"command-{stage['stage_id']}-register-20260912")
    controller.start(stage["stage_id"], command_id=f"command-{stage['stage_id']}-start-20260912")
    return controller


def scenario_a(root: Path) -> dict[str, Any]:
    workspace_id = "workspace-phase7-scenario-a-20260912"
    stage_id = "stage-phase7-scenario-a-20260912"
    stage = scenario_stage(workspace_id, stage_id, max_iterations=1)
    controller = setup_controller(root, workspace_id, stage)
    first = controller.request_execution(stage_id, command_id="command-phase7-a-first-20260912", provenance={"provider": "bounded-scenario-fixture", "engine_digest": "engine-a-first-20260912"})
    first_obs = observation(stage_id=stage_id, attempt=first["attempt"], name="a-first", terminal="FAILED")
    controller.record_observation(stage_id, first_obs, effect_state="SETTLED", command_id="command-phase7-a-observe-first-20260912")
    first_assessment = assessment_for(first_obs, baseline=stage["baseline_digest"], validator="validator-phase7-a-v1-20260912", name="a-first")
    controller.assess_result(stage_id, first_assessment, command_id="command-phase7-a-assess-first-20260912")
    second = controller.request_execution(stage_id, command_id="command-phase7-a-second-20260912", reason="RETRY", provenance={"provider": "bounded-scenario-fixture", "engine_digest": "engine-a-second-20260912"})
    second_obs = observation(stage_id=stage_id, attempt=second["attempt"], name="a-second", terminal="SUCCEEDED")
    controller.record_observation(stage_id, second_obs, effect_state="SETTLED", command_id="command-phase7-a-observe-second-20260912")
    second_assessment = assessment_for(second_obs, baseline=stage["baseline_digest"], validator="validator-phase7-a-v1-20260912", name="a-second")
    controller.assess_result(stage_id, second_assessment, command_id="command-phase7-a-assess-second-20260912")
    first_before = copy.deepcopy(controller.state["observations"][first_obs["observation_id"]])
    same_iteration = first["attempt"]["iteration_id"] == second["attempt"]["iteration_id"]
    distinct_attempts = first["attempt"]["attempt_id"] != second["attempt"]["attempt_id"]
    if not same_iteration or not distinct_attempts or first_before != first_obs:
        raise RuntimeError("Scenario A did not preserve the failed observation and same-iteration retry identity")
    stopped = controller.stop(stage_id, command_id="command-phase7-a-stop-20260912", reason="scenario evidence complete")
    return {
        "status": "PASS",
        "scenario": "A",
        "name": "same-iteration retry after a retryable engineering failure",
        "journal": str(root / ".workflow-v2" / "journal.json"),
        "first_attempt_id": first["attempt"]["attempt_id"],
        "second_attempt_id": second["attempt"]["attempt_id"],
        "same_iteration": same_iteration,
        "distinct_attempts": distinct_attempts,
        "first_observation_preserved": first_before == first_obs,
        "first_assessment_verdict": first_assessment["verdict"],
        "second_assessment_verdict": second_assessment["verdict"],
        "attempt_count": controller.show_stage(stage_id)["attempt_count"],
        "final_status": stopped["stage"]["status"],
    }


def extract_replan_subtype(response_text: str) -> str:
    matches = re.findall(r"(?im)^\s*REPLAN_SUBTYPE\s*:\s*(ENGINEERING_FIX|NEXT_ITERATION|BASELINE_CHANGE)\s*$", response_text)
    if matches != ["NEXT_ITERATION"]:
        raise RuntimeError(f"real GPT did not provide exactly REPLAN_SUBTYPE: NEXT_ITERATION: {matches}")
    return matches[0]


def scenario_b_impl(root: Path, bridge_root: Path, profile_dir: Path, timeout_ms: int) -> dict[str, Any]:
    workspace_id = "workspace-phase7-scenario-b-20260912"
    stage_id = "stage-phase7-scenario-b-20260912"
    stage = scenario_stage(workspace_id, stage_id, max_iterations=2)
    controller = setup_controller(root / "controller", workspace_id, stage)
    requested = controller.request_execution(stage_id, command_id="command-phase7-b-request-20260912", provenance={"provider": "bounded-scenario-fixture", "engine_digest": "engine-b-20260912"})
    obs = observation(stage_id=stage_id, attempt=requested["attempt"], name="b-failed", terminal="FAILED")
    controller.record_observation(stage_id, obs, effect_state="SETTLED", command_id="command-phase7-b-observe-20260912")
    assessment = assessment_for(obs, baseline=stage["baseline_digest"], validator="validator-phase7-b-v1-20260912", name="b-failed")
    controller.assess_result(stage_id, assessment, command_id="command-phase7-b-assess-20260912")
    review_root = root / "gpt-review"
    review_root.mkdir(parents=True, exist_ok=False)
    (review_root / "evidence").mkdir()
    (review_root / "planning").mkdir()
    evidence_file = "evidence/scenario-b-review.json"
    write_json(review_root / evidence_file, {"stage_id": stage_id, "assessment_id": assessment["assessment_id"], "assessment_verdict": assessment["verdict"], "effect_state": "SETTLED", "provider_failure_inference": False, "raw_response_persisted": False})
    pack = fresh_pack(goal=OBJECTIVE, stage_goal="Fresh Phase 7 scenario B review.", latest={"assessment_id": assessment["assessment_id"], "assessment_verdict": assessment["verdict"], "provider_failure_inference": False, "effect_state": "SETTLED"}, evidence=[{"sourcePath": evidence_file, "logicalName": "scenario-b-review.json", "stagedName": evidence_file, "role": "result"}], git_commit="scenario-b-baseline-20260912")
    from src.stage_integration import parse_dialogue_decision  # noqa: E402
    from src.bridge_adapter import normalize_bridge_envelope  # noqa: E402
    from src.stage_integration import subprocess_bridge_runner  # noqa: E402
    raw = subprocess_bridge_runner(
        "Perform a fresh technical review of this bounded V2 assessment. The evidence is a settled, retryable failed attempt and no provider-success claim is being inferred. Request one semantic next iteration because the current attempt is not admissible. Do not execute or modify anything. Return exactly these two standalone lines and no other workflow marker: WORKFLOW_DECISION: REPLAN\nREPLAN_SUBTYPE: NEXT_ITERATION",
        mode="fresh", continue_from=None, context_pack=pack, root_dir=str(review_root), profile_dir=str(profile_dir), timeout_ms=timeout_ms, bridge_root=str(bridge_root), transport="homepage_fallback",
    )
    checked = normalize_bridge_envelope(raw, expected_mode="fresh", require_receipt=True)
    response_text = checked.get("response_text")
    if not isinstance(response_text, str) or parse_dialogue_decision(response_text) != "REPLAN":
        raise RuntimeError("Scenario B real GPT review did not return REPLAN")
    subtype = extract_replan_subtype(response_text)
    receipt = checked["receipt"]
    if receipt.get("status") != "complete" or receipt.get("request_count") != 1 or receipt.get("conversation_validated") is not True:
        raise RuntimeError("Scenario B bridge receipt is not a single validated request")
    response_digest = sha256_bytes(response_text.encode("utf-8"))
    packet_digest = receipt["context_pack"]["pack_sha256"]
    decision = gpt_decision(assessment["assessment_id"], decision_id=f"decision-phase7-b-{response_digest[:24]}", allowed_choices=["REPLAN:NEXT_ITERATION"], response_digest=response_digest, packet_digest=packet_digest)
    result = controller.apply_gpt_decision(stage_id, decision, choice="REPLAN", replan_subtype=subtype, technical_change_digest="technical-change-phase7-b-20260912", solution_fingerprint="solution-phase7-b-next-20260912", command_id="command-phase7-b-replan-20260912")
    old_iteration = requested["attempt"]["iteration_id"]
    new_iteration = result["stage"]["current_iteration_id"]
    if result["stage"]["iteration_count"] != 2 or old_iteration == new_iteration or len(controller.state["attempts"]) != 1:
        raise RuntimeError("Scenario B did not create exactly one new semantic iteration")
    stopped = controller.stop(stage_id, command_id="command-phase7-b-stop-20260912", reason="scenario evidence complete")
    return {"status": "PASS", "scenario": "B", "name": "real GPT typed NEXT_ITERATION replan", "journal": str(root / "controller" / ".workflow-v2" / "journal.json"), "review": {"consultation_id": receipt["consultation_id"], "conversation_id": receipt["conversation_id"], "request_count": receipt["request_count"], "decision": "REPLAN", "replan_subtype": subtype, "response_sha256": response_digest, "packet_sha256": packet_digest, "receipt_path": checked.get("receipt_path"), "raw_response_persisted": False}, "assessment_id": assessment["assessment_id"], "assessment_verdict": assessment["verdict"], "provider_failure_inference": False, "old_iteration_id": old_iteration, "new_iteration_id": new_iteration, "iteration_count": result["stage"]["iteration_count"], "final_status": stopped["stage"]["status"]}


def scenario_c(root: Path, bridge_root: Path, profile_dir: Path, timeout_ms: int) -> dict[str, Any]:
    workspace_id = "workspace-phase7-scenario-c-20260912"
    stage_id = "stage-phase7-scenario-c-20260912"
    stage = scenario_stage(workspace_id, stage_id, max_iterations=1)
    controller = setup_controller(root / "controller", workspace_id, stage)
    requested = controller.request_execution(stage_id, command_id="command-phase7-c-request-20260912", provenance={"provider": "bounded-scenario-fixture", "engine_digest": "engine-c-20260912"})
    obs = observation(stage_id=stage_id, attempt=requested["attempt"], name="c-failed", terminal="FAILED")
    controller.record_observation(stage_id, obs, effect_state="SETTLED", command_id="command-phase7-c-observe-20260912")
    old_assessment = assessment_for(obs, baseline=stage["baseline_digest"], validator="validator-phase7-c-old-20260912", name="c-failed")
    controller.assess_result(stage_id, old_assessment, command_id="command-phase7-c-assess-old-20260912")
    correction_decision = human_decision(decision_id="decision-phase7-c-correction-20260912", subject_id="VALIDATOR_CORRECTION", subject_type="VALIDATOR_CORRECTION", subject_digest="correction-subject-phase7-c-20260912", choices=["ACCEPT_CORRECTION", "REJECT_CORRECTION"])
    controller.request_decision(correction_decision, command_id="command-phase7-c-request-human-20260912")
    controller.apply_decision("VALIDATOR_CORRECTION", payload={"decision_id": correction_decision["decision_id"], "subject_digest": correction_decision["subject_digest"], "subject_version": 1, "choice": "ACCEPT_CORRECTION"}, command_id="command-phase7-c-accept-human-20260912")
    correction = {
        "schema_version": "correction_receipt.v2",
        "correction_id": "correction-phase7-c-20260912",
        "old_validator_code_digest": "validator-phase7-c-old-20260912",
        "new_validator_code_digest": "validator-phase7-c-new-20260912",
        "bug_evidence_digest": "bug-evidence-phase7-c-20260912",
        "unchanged_contract_digest": "contract-phase7-c-20260912",
        "review_identity": "review-phase7-c-20260912",
        "human_decision_id": correction_decision["decision_id"],
        "subject_type": "VALIDATOR_CORRECTION",
        "scope_expansion": False,
        "approved": True,
    }
    new_assessment = assessment_for(obs, baseline=stage["baseline_digest"], validator=correction["new_validator_code_digest"], name="c-failed", correction_receipt=correction, revalidation=True, supersedes_assessment_id=old_assessment["assessment_id"])
    controller.assess_result(stage_id, new_assessment, correction_receipt=correction, command_id="command-phase7-c-assess-new-20260912")
    if new_assessment["verdict"] != "ADMISSIBLE" or controller.state["observations"][obs["observation_id"]] != obs or len(controller.state["attempts"]) != 1:
        raise RuntimeError("Scenario C did not preserve evidence and avoid a provider rerun")
    review_root = root / "gpt-review"
    review_root.mkdir(parents=True, exist_ok=False)
    (review_root / "evidence").mkdir()
    (review_root / "planning").mkdir()
    evidence_file = "evidence/scenario-c-review.json"
    write_json(review_root / evidence_file, {"stage_id": stage_id, "old_assessment_id": old_assessment["assessment_id"], "assessment_id": new_assessment["assessment_id"], "assessment_verdict": "ADMISSIBLE", "revalidation": True, "correction_receipt_digest": sha256_json(correction), "provider_rerun": False, "raw_response_persisted": False})
    pack = fresh_pack(goal=OBJECTIVE, stage_goal="Fresh Phase 7 scenario C technical review after validator correction.", latest={"assessment_id": new_assessment["assessment_id"], "assessment_verdict": "ADMISSIBLE", "revalidation": True, "provider_rerun": False}, evidence=[{"sourcePath": evidence_file, "logicalName": "scenario-c-review.json", "stagedName": evidence_file, "role": "result"}], git_commit="scenario-c-baseline-20260912")
    from src.bridge_adapter import normalize_bridge_envelope  # noqa: E402
    from src.stage_integration import parse_dialogue_decision, subprocess_bridge_runner  # noqa: E402
    raw = subprocess_bridge_runner(
        "Perform a fresh technical review of this bounded V2 revalidation. The original immutable provider observation remains FAILED; an accepted Human validator-correction receipt authorizes revalidation, the new assessment is ADMISSIBLE, the provider was not rerun, and the scope is unchanged. STAGE_READY means technically ready only; do not execute, modify, integrate, or grant Human approval. Return one final standalone line exactly: WORKFLOW_DECISION: STAGE_READY",
        mode="fresh", continue_from=None, context_pack=pack, root_dir=str(review_root), profile_dir=str(profile_dir), timeout_ms=timeout_ms, bridge_root=str(bridge_root), transport="homepage_fallback",
    )
    checked = normalize_bridge_envelope(raw, expected_mode="fresh", require_receipt=True)
    response_text = checked.get("response_text")
    if not isinstance(response_text, str) or parse_dialogue_decision(response_text) != "STAGE_READY":
        raise RuntimeError("Scenario C real GPT review did not return STAGE_READY")
    receipt = checked["receipt"]
    if receipt.get("status") != "complete" or receipt.get("request_count") != 1 or receipt.get("conversation_validated") is not True:
        raise RuntimeError("Scenario C bridge receipt is not a single validated request")
    response_digest = sha256_bytes(response_text.encode("utf-8"))
    packet_digest = receipt["context_pack"]["pack_sha256"]
    decision = gpt_decision(new_assessment["assessment_id"], decision_id=f"decision-phase7-c-{response_digest[:24]}", allowed_choices=["STAGE_READY"], response_digest=response_digest, packet_digest=packet_digest)
    ready = controller.apply_gpt_decision(stage_id, decision, choice="STAGE_READY", command_id="command-phase7-c-ready-20260912")
    stopped = controller.stop(stage_id, command_id="command-phase7-c-stop-20260912", reason="scenario evidence complete")
    return {"status": "PASS", "scenario": "C", "name": "Human validator correction and real GPT revalidation review", "journal": str(root / "controller" / ".workflow-v2" / "journal.json"), "old_assessment_id": old_assessment["assessment_id"], "new_assessment_id": new_assessment["assessment_id"], "old_assessment_preserved": old_assessment["assessment_id"] in controller.state["assessments"], "new_assessment_verdict": new_assessment["verdict"], "provider_attempt_count": len(controller.state["attempts"]), "review": {"consultation_id": receipt["consultation_id"], "conversation_id": receipt["conversation_id"], "request_count": receipt["request_count"], "decision": "STAGE_READY", "response_sha256": response_digest, "packet_sha256": packet_digest, "receipt_path": checked.get("receipt_path"), "raw_response_persisted": False}, "ready_status": ready["stage"]["status"], "final_status": stopped["stage"]["status"]}


def scenario_d(root: Path) -> dict[str, Any]:
    workspace_id = "workspace-phase7-scenario-d-20260912"
    parent_id = "stage-phase7-scenario-d-parent-20260912"
    child_id = "stage-phase7-scenario-d-child-20260912"
    parent = scenario_stage(workspace_id, parent_id)
    child = scenario_stage(workspace_id, child_id)
    root.mkdir(parents=True, exist_ok=False)
    (root / ".workflow-v2").mkdir()
    controller = StageController(workspace_id=workspace_id, state_path=root / ".workflow-v2" / "journal.json")
    controller.initialize()
    controller.register_stage(parent, command_id="command-phase7-d-parent-register-20260912")
    parent_decision = human_decision(decision_id="decision-phase7-d-parent-pending-20260912", subject_id=parent_id, subject_type="STAGE_PLANNING", subject_digest="parent-plan-phase7-d-20260912", choices=["APPROVE", "REJECT"])
    controller.request_decision(parent_decision, command_id="command-phase7-d-parent-request-20260912")
    edge = {"schema_version": "dependency.v2", "parent_id": parent_id, "child_id": child_id, "predicate": "child closed output", "input_binding": {"digest": "input-phase7-d-20260912"}, "output_contract_digest": "output-phase7-d-20260912", "authorization_decision_id": "decision-phase7-d-dependency-20260912", "child_status": "PLANNED"}
    dependency_decision = human_decision(decision_id=edge["authorization_decision_id"], subject_id=child_id, subject_type="DEPENDENCY", subject_digest=dependency_authorization_digest(edge), choices=["ACCEPT_DEPENDENCY", "REJECT_DEPENDENCY"])
    controller.request_decision(dependency_decision, command_id="command-phase7-d-dependency-request-20260912")
    controller.apply_decision(child_id, payload={"decision_id": dependency_decision["decision_id"], "subject_digest": dependency_decision["subject_digest"], "subject_version": 1, "choice": "ACCEPT_DEPENDENCY"}, command_id="command-phase7-d-dependency-accept-20260912")
    added = controller.add_dependency(parent_id, edge, child, command_id="command-phase7-d-add-20260912")
    controller.start(child_id, command_id="command-phase7-d-child-start-20260912")
    requested = controller.request_execution(child_id, command_id="command-phase7-d-child-request-20260912", provenance={"provider": "bounded-scenario-fixture", "engine_digest": "engine-d-20260912"})
    obs = observation(stage_id=child_id, attempt=requested["attempt"], name="d-child", terminal="SUCCEEDED")
    controller.record_observation(child_id, obs, effect_state="SETTLED", command_id="command-phase7-d-child-observe-20260912")
    assessment = assessment_for(obs, baseline=child["baseline_digest"], validator="validator-phase7-d-20260912", name="d-child")
    controller.assess_result(child_id, assessment, command_id="command-phase7-d-child-assess-20260912")
    child_decision = gpt_decision(assessment["assessment_id"], decision_id="decision-phase7-d-child-ready-20260912", allowed_choices=["STAGE_READY"], response_digest="response-phase7-d-child-20260912", packet_digest="packet-phase7-d-child-20260912")
    controller.apply_gpt_decision(child_id, child_decision, choice="STAGE_READY", command_id="command-phase7-d-child-ready-20260912")
    closed = controller.closeout(child_id, no_integration_required=True, verification_digest="verification-phase7-d-child-20260912", command_id="command-phase7-d-child-close-20260912")
    satisfied = controller.satisfy_dependency(parent_id, payload={"child_id": child_id, "output_contract_digest": edge["output_contract_digest"], "closeout_identity": closed["closeout"]["closeout_id"]}, command_id="command-phase7-d-satisfy-20260912")
    parent_after = satisfied["parent"]
    if parent_after["status"] != "PLANNED" or controller.state["executable_owner_stage_id"] != parent_id or controller.state["decisions"][parent_decision["decision_id"]]["resolution"] is not None:
        raise RuntimeError("Scenario D did not preserve the parent pending decision and return ownership")
    return {"status": "PASS", "scenario": "D", "name": "dependency child normal closeout and ownership return", "journal": str(root / ".workflow-v2" / "journal.json"), "parent_id": parent_id, "child_id": child_id, "parent_pending_decision_id": parent_decision["decision_id"], "parent_status_before": added["stage"]["status"], "parent_status_after": parent_after["status"], "child_status": satisfied["child"]["status"], "dependency_status": satisfied["dependency"]["status"], "executable_owner_after": controller.state["executable_owner_stage_id"], "pending_parent_decision_preserved": controller.state["decisions"][parent_decision["decision_id"]]["resolution"] is None}


def scenario_e(root: Path) -> dict[str, Any]:
    workspace_id = "workspace-phase7-scenario-e-20260912"
    stage_id = "stage-phase7-scenario-e-20260912"
    stage = scenario_stage(workspace_id, stage_id, max_iterations=1)
    controller = setup_controller(root, workspace_id, stage)
    requested = controller.request_execution(stage_id, command_id="command-phase7-e-request-20260912", provenance={"provider": "bounded-scenario-fixture", "engine_digest": "engine-e-20260912"})
    operation_id = requested["operation"]["operation_id"]
    attempt_id = requested["attempt"]["attempt_id"]
    reloaded = StageController.from_state(root / ".workflow-v2" / "journal.json")
    restored = reloaded.show_stage(stage_id)
    same_identity = reloaded.state["stage_runtime"][stage_id]["in_flight_operation_id"] == operation_id and reloaded.state["stage_runtime"][stage_id]["current_attempt_id"] == attempt_id
    if not same_identity:
        raise RuntimeError("Scenario E reload changed the in-flight operation or attempt identity")
    receipt = {"query": "external effect unavailable after simulated crash", "operation_id": operation_id}
    unknown = reloaded.apply_receipt(stage_id, operation_id=operation_id, effect_state="UNKNOWN", receipt=receipt, receipt_digest=sha256_json(receipt), command_id="command-phase7-e-unknown-receipt-20260912")
    blocked = unknown["operation"]["effect_state"] == "UNKNOWN" and operation_id in reloaded.state["blockers"]
    assess_blocked = False
    try:
        reloaded.assess_result(stage_id, {"not": "replay"}, command_id="command-phase7-e-forbidden-assess-20260912")
    except (WorkflowV2ControllerError, ContractValidationError):
        assess_blocked = True
    stop_blocked = False
    try:
        reloaded.stop(stage_id, command_id="command-phase7-e-stop-20260912")
    except WorkflowV2ControllerError:
        stop_blocked = True
    if not blocked or not assess_blocked or not stop_blocked:
        raise RuntimeError("Scenario E did not fail closed on UNKNOWN external effect")
    return {"status": "PASS", "scenario": "E", "name": "crash/reload with unknown external effect blocks replay", "journal": str(root / ".workflow-v2" / "journal.json"), "operation_id": operation_id, "attempt_id": attempt_id, "reloaded_operation_id": reloaded.state["stage_runtime"][stage_id]["in_flight_operation_id"], "reloaded_attempt_id": reloaded.state["stage_runtime"][stage_id]["current_attempt_id"], "same_identity_after_reload": same_identity, "effect_state": unknown["operation"]["effect_state"], "unknown_external_effect_blocker": blocked, "assessment_blocked": assess_blocked, "stop_blocked": stop_blocked, "final_status": restored["status"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 7 Workflow V2 scenarios A-E")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--bridge-root", required=True)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--timeout-ms", type=int, default=300_000)
    args = parser.parse_args(argv)
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    output = Path(args.output_dir).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise RuntimeError(f"refusing to reuse non-empty scenario output directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    scenarios = [
        scenario_a(output / "A"),
        scenario_b_impl(output / "B", Path(args.bridge_root).resolve(), Path(args.profile_dir).resolve(), args.timeout_ms),
        scenario_c(output / "C", Path(args.bridge_root).resolve(), Path(args.profile_dir).resolve(), args.timeout_ms),
        scenario_d(output / "D"),
        scenario_e(output / "E"),
    ]
    result = {"evidence_kind": "REAL_SELF_HOST_VALIDATION_SCENARIOS", "status": "PASS", "external_provider_or_gpt_called": True, "scenarios": scenarios}
    write_json(output / "SCENARIOS_A_E.json", result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
