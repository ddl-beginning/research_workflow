from __future__ import annotations

import copy
from pathlib import Path

import pytest

from src.contracts import sha256_json
from src.executor import ExecutionResult
from src.product_workflow_runtime import ProductWorkflowRuntime
from src.runtime_composition import load_runtime_composition_config
from src.workflow_v2_contracts import (
    assess_observation,
    classify_blocker,
    classify_blocker_ownership,
    validate_genuine_blocked_evidence,
    validate_human_gate,
    observation_identity,
)
from src.workflow_v2_controller import StageController, WorkflowV2ControllerError


def _stage() -> dict:
    return {
        "schema_version": "stage.v2",
        "stage_id": "stage-maintenance-12345678",
        "workspace_id": "workspace-maintenance-12345678",
        "project_id": "project-maintenance",
        "objective_fingerprint": "objective-maintenance-12345678",
        "target_identity": "local/facade",
        "required_capabilities": ["python"],
        "purpose": "DOMAIN",
        "repair_depth": 0,
        "baseline_digest": "baseline-maintenance-12345678",
        "status": "PLANNED",
        "budgets": {
            "max_iterations": 2,
            "max_attempts_per_iteration": 2,
            "max_attempts_total": 2,
            "max_revalidation_ops": 1,
            "max_validator_revisions": 2,
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


def _manifest(attempt_id: str) -> dict:
    return {
        "schema_version": "evidence_manifest.v2",
        "manifest_id": "manifest-maintenance-12345678",
        "stage_id": "stage-maintenance-12345678",
        "attempt_id": attempt_id,
        "required_artifact_paths": ["artifacts/result.json"],
        "changed_paths": ["artifacts/result.json"],
        "allowed_paths": ["artifacts"],
        "protected_paths": [".git", ".workflow-v2"],
        "path_inventory": [{"path": "artifacts/result.json", "sha256": "artifact-maintenance-12345678"}],
        "complete": True,
    }


def _failed_observation(attempt: dict, operation_id: str) -> dict:
    value = {
        "schema_version": "provider_observation.v2",
        "stage_id": attempt["stage_id"],
        "objective_identity": attempt["objective_identity"],
        "iteration_id": attempt["iteration_id"],
        "attempt_id": attempt["attempt_id"],
        "provider_operation_id": operation_id,
        "result_identity": "provider-result-maintenance-12345678",
        "provider_result_digest": "provider-result-maintenance-12345678",
        "evidence_manifest_digest": "manifest-maintenance-12345678",
        "provider_terminal_status": "FAILED",
        "raw_provider_claim": {"status": "FAILED", "summary": "bounded provider failure"},
        "provenance": {"provider": "fixture-provider", "engine_digest": "engine-maintenance-12345678"},
        "failure": {
            "schema_version": "typed_failure.v2",
            "failure_class": "PROVIDER_FAILURE",
            "code": "PROVIDER_FAILURE",
            "operation_id": operation_id,
            "retryability": "RETRYABLE",
            "effect_state": "SETTLED",
            "evidence_refs": ["evidence-maintenance-12345678"],
            "authority": "CONTROLLER_POLICY",
        },
        "outputs": {},
        "immutable": True,
    }
    value["observation_id"] = observation_identity(value)
    return value


def _blocked_controller() -> tuple[StageController, dict, dict, dict, dict]:
    controller = StageController(workspace_id="workspace-maintenance-12345678")
    controller.register_stage(_stage())
    controller.start("stage-maintenance-12345678", command_id="command-maintenance-start-12345678")
    requested = controller.request_execution("stage-maintenance-12345678", command_id="command-maintenance-request-12345678")
    attempt = requested["attempt"]
    operation = requested["operation"]
    observation = _failed_observation(attempt, operation["operation_id"])
    controller.record_observation("stage-maintenance-12345678", observation, effect_state="SETTLED", command_id="command-maintenance-observe-12345678")
    assessment = assess_observation(
        observation, _manifest(attempt["attempt_id"]), baseline_digest="baseline-maintenance-12345678",
        validator_code_digest="validator-maintenance-12345678", validation_contract_revision="admission.v2",
    )
    controller.assess_result("stage-maintenance-12345678", assessment, command_id="command-maintenance-assess-12345678")
    decision = {
        "schema_version": "decision.v2",
        "decision_id": "decision-maintenance-blocked-12345678",
        "actor_kind": "GPT",
        "boundary": "TECHNICAL_REVIEW",
        "subject_id": assessment["assessment_id"],
        "subject_digest": assessment["assessment_id"],
        "subject_version": 1,
        "allowed_choices": ["CONTINUE", "REPLAN:ENGINEERING_FIX", "BLOCKED", "HUMAN_GATE"],
        "requested_action": "APPLY_GPT_DECISION",
        "provenance": {"request_count": 1, "conversation_id": "conversation-maintenance-12345678", "response_digest": "response-maintenance-12345678", "packet_digest": "packet-maintenance-12345678"},
        "supersedes": None,
    }
    controller.apply_gpt_decision("stage-maintenance-12345678", decision, choice="BLOCKED", command_id="command-maintenance-blocked-12345678")
    return controller, attempt, observation, assessment, decision


def _blocker_record(controller: StageController, assessment: dict, observation: dict, *, owner: str | None = None) -> dict:
    derived = classify_blocker(assessment=assessment, observation=observation, missing_thing_owner=owner, evidence_refs=[observation["evidence_manifest_digest"]])
    return {
        "blocker_id": "blocker-maintenance-12345678",
        "project_id": "project-maintenance",
        "stage_id": "stage-maintenance-12345678",
        "assessment_id": assessment["assessment_id"],
        "revision": controller.revision,
        "failure_class": derived["failure_class"],
        "failure_signature": derived["failure_signature"],
        "evidence_refs": derived["evidence_refs"],
        "recoverability": derived["recoverability"],
        "origin": "TEST",
        "created_at": "2026-09-15T00:00:00+00:00",
        "last_validated_at": "2026-09-15T00:00:00+00:00",
        "resolved_at": None,
        "missing_thing_owner": owner,
    }


def test_stale_technical_blocked_auto_reassesses_without_relaying_to_human():
    controller, attempt, observation, assessment, _decision = _blocked_controller()
    original = copy.deepcopy(controller.state)
    result = controller.revalidate_blocker(
        "stage-maintenance-12345678",
        blocker_record=_blocker_record(controller, assessment, observation),
        blocker_still_true="NO",
        released_attempt_id=attempt["attempt_id"],
        command_id="command-maintenance-revalidate-12345678",
    )
    assert result["stage"]["next_action"] == "REQUEST_EXECUTION"
    assert result["stage"]["status"] == "ACTIVE"
    assert controller.state["assessments"][assessment["assessment_id"]] == original["assessments"][assessment["assessment_id"]]
    assert controller.state["decisions"]["decision-maintenance-blocked-12345678"] == original["decisions"]["decision-maintenance-blocked-12345678"]
    assert len(controller.state["blocker_records"]) == 2


def test_resume_stale_projection_releases_exactly_one_bounded_attempt():
    controller, attempt, observation, assessment, _decision = _blocked_controller()
    controller.revalidate_blocker("stage-maintenance-12345678", blocker_record=_blocker_record(controller, assessment, observation), blocker_still_true="NO", released_attempt_id=attempt["attempt_id"], command_id="command-maintenance-revalidate-budget-12345678")
    next_request = controller.request_execution("stage-maintenance-12345678", reason="CONTINUE", command_id="command-maintenance-second-request-12345678")
    assert next_request["attempt"]["attempt_index"] == 2
    with pytest.raises(WorkflowV2ControllerError, match="execution operation"):
        controller.request_execution("stage-maintenance-12345678", reason="CONTINUE", command_id="command-maintenance-third-request-12345678")


def test_missing_stage_owned_output_is_work_remaining():
    _controller_instance, _attempt, observation, assessment, _decision = _blocked_controller()
    derived = classify_blocker(assessment={**assessment, "checks": {"s4": "BLOCKED_BY_MISSING_INPUT_AND_CAPABILITY"}}, observation=observation)
    assert derived["failure_class"] == "SCIENTIFIC_BLOCKER"
    assert derived["recommended_action"] == "WORK_REMAINING"
    assert derived["blocker_still_true"] == "NO"


def test_external_blocker_remains_genuine_and_not_stage_owned():
    _controller_instance, _attempt, observation, assessment, _decision = _blocked_controller()
    derived = classify_blocker(assessment=assessment, observation=observation, missing_thing_owner="EXTERNAL")
    assert derived["failure_class"] == "EXTERNAL_BLOCKER"
    assert derived["recoverability"] == "EXTERNAL_UNAVAILABLE"
    assert derived["recommended_action"] == "HUMAN_REQUIRED"


def test_automatic_technical_gpt_escalation_classification():
    _controller_instance, _attempt, observation, assessment, _decision = _blocked_controller()
    derived = classify_blocker(assessment=assessment, observation=observation)
    assert derived["recoverability"] == "TECHNICAL_NEEDS_GPT"
    assert derived["recommended_action"] == "TECHNICAL_GPT_ESCALATION"


def test_bare_provider_blocked_status_is_not_scientific_human_blocked():
    _controller_instance, _attempt, observation, assessment, _decision = _blocked_controller()
    provider_blocked = copy.deepcopy(observation)
    provider_blocked["failure"]["failure_class"] = "PROVIDER_FAILURE"
    provider_blocked["failure"]["code"] = "PROVIDER_BLOCKED"
    derived = classify_blocker(assessment=assessment, observation=provider_blocked)
    assert derived["failure_class"] == "PROVIDER_FAILURE"
    assert derived["recoverability"] == "TECHNICAL_NEEDS_GPT"
    assert derived["recommended_action"] == "TECHNICAL_GPT_ESCALATION"


def test_gpt_continue_is_legal_after_reassessment_release():
    controller, attempt, observation, assessment, _decision = _blocked_controller()
    controller.revalidate_blocker("stage-maintenance-12345678", blocker_record=_blocker_record(controller, assessment, observation), blocker_still_true="NO", released_attempt_id=attempt["attempt_id"], command_id="command-maintenance-revalidate-continue-12345678")
    decision = {
        "schema_version": "decision.v2", "decision_id": "decision-maintenance-continue-12345678", "actor_kind": "GPT", "boundary": "TECHNICAL_REVIEW",
        "subject_id": assessment["assessment_id"], "subject_digest": assessment["assessment_id"], "subject_version": 1,
        "allowed_choices": ["CONTINUE"], "requested_action": "APPLY_GPT_DECISION",
        "provenance": {"request_count": 1, "conversation_id": "conversation-maintenance-continue-12345678", "response_digest": "response-maintenance-continue-12345678", "packet_digest": "packet-maintenance-continue-12345678"}, "supersedes": None,
    }
    applied = controller.apply_gpt_decision("stage-maintenance-12345678", decision, choice="CONTINUE", command_id="command-maintenance-apply-continue-12345678")
    assert applied["choice"] == "CONTINUE"
    assert applied["stage"]["next_action"] == "REQUEST_EXECUTION"


def test_gpt_replan_is_typed_and_automatically_applicable():
    controller, attempt, observation, assessment, _decision = _blocked_controller()
    controller.revalidate_blocker("stage-maintenance-12345678", blocker_record=_blocker_record(controller, assessment, observation), blocker_still_true="NO", released_attempt_id=attempt["attempt_id"], command_id="command-maintenance-revalidate-replan-12345678")
    decision = {
        "schema_version": "decision.v2", "decision_id": "decision-maintenance-replan-12345678", "actor_kind": "GPT", "boundary": "TECHNICAL_REVIEW",
        "subject_id": assessment["assessment_id"], "subject_digest": assessment["assessment_id"], "subject_version": 1,
        "allowed_choices": ["REPLAN:ENGINEERING_FIX"], "requested_action": "APPLY_GPT_DECISION",
        "provenance": {"request_count": 1, "conversation_id": "conversation-maintenance-replan-12345678", "response_digest": "response-maintenance-replan-12345678", "packet_digest": "packet-maintenance-replan-12345678"}, "supersedes": None,
    }
    applied = controller.apply_gpt_decision("stage-maintenance-12345678", decision, choice="REPLAN", replan_subtype="ENGINEERING_FIX", command_id="command-maintenance-apply-replan-12345678")
    assert applied["decision"]["decision_id"] == "decision-maintenance-replan-12345678"
    assert applied["stage"]["next_action"] == "REQUEST_EXECUTION"


def test_human_gate_requires_question_why_options_consequence_and_explicit_need():
    with pytest.raises(Exception):
        validate_human_gate({"QUESTION_FOR_HUMAN": "choose"})
    gate = validate_human_gate({"QUESTION_FOR_HUMAN": "choose", "WHY_AI_CANNOT_DECIDE": "irreducible choice", "OPTIONS": ["A", "B"], "CONSEQUENCE": "route changes", "HUMAN_DECISION_REQUIRED": True})
    assert gate["HUMAN_DECISION_REQUIRED"] is True


def test_gpt_human_gate_is_projected_to_human_only_actor():
    controller, _attempt, _observation, assessment, _decision = _blocked_controller()
    decision = {
        "schema_version": "decision.v2", "decision_id": "decision-maintenance-gate-12345678", "actor_kind": "GPT", "boundary": "TECHNICAL_REVIEW",
        "subject_id": assessment["assessment_id"], "subject_digest": assessment["assessment_id"], "subject_version": 1,
        "allowed_choices": ["HUMAN_GATE"], "requested_action": "APPLY_GPT_DECISION",
        "provenance": {"request_count": 1, "conversation_id": "conversation-maintenance-gate-12345678", "response_digest": "response-maintenance-gate-12345678", "packet_digest": "packet-maintenance-gate-12345678"}, "supersedes": None,
    }
    controller.apply_gpt_decision("stage-maintenance-12345678", decision, choice="HUMAN_GATE", human_gate={"QUESTION_FOR_HUMAN": "choose", "WHY_AI_CANNOT_DECIDE": "irreducible", "OPTIONS": ["A", "B"], "CONSEQUENCE": "different route", "HUMAN_DECISION_REQUIRED": True}, command_id="command-maintenance-apply-gate-12345678")
    projection = controller.resume_projection()
    assert projection["next_action"] == "HUMAN_GATE"
    assert projection["next_actor"] == "Human"


def test_genuine_blocked_validator_rejects_human_relay_shortcut():
    with pytest.raises(Exception):
        validate_genuine_blocked_evidence({"blocker_still_true": "YES"})
    ownership = {
        "schema_version": "blocker_ownership.v1",
        "items": [{
            "item": "private input", "class": "HUMAN_ONLY_PRIVATE_INPUT", "owner": "Human",
            "expected_producer": "Human", "can_codex_create": False, "can_gpt_design_route": False,
            "can_workflow_obtain": False, "requires_human": True, "why": "private input is unavailable",
            "evidence_refs": ["evidence-maintenance-12345678"],
        }],
        "stage_owned_work_available": False, "provider_implementable_route_available": False,
        "gpt_designable_route_available": False, "canonical_lifecycle_route_available": False,
        "authorized_alternative_available": False, "human_only_input_found": True,
        "blocker_ownership_validated": True, "genuine_blocked_valid": True,
        "validated_at": "2026-09-15T00:00:00+00:00", "human_intervention_count": 0,
    }
    assert validate_genuine_blocked_evidence({"blocker_still_true": "YES", "auto_recovery_exhausted": True, "gpt_technical_escalation_completed": True, "no_legal_automated_next_action": True, "ownership_validation": ownership})["auto_recovery_exhausted"] is True


def test_blocker_history_preserves_original_observation_assessment_and_decision():
    controller, attempt, observation, assessment, decision = _blocked_controller()
    before = controller.state
    controller.revalidate_blocker("stage-maintenance-12345678", blocker_record=_blocker_record(controller, assessment, observation), blocker_still_true="YES", command_id="command-maintenance-preserve-12345678")
    after = controller.state
    assert after["observations"] == before["observations"]
    assert after["assessments"] == before["assessments"]
    assert after["decisions"] == before["decisions"]


def test_bounded_revalidation_cannot_be_repeated():
    controller, attempt, observation, assessment, _decision = _blocked_controller()
    controller.revalidate_blocker("stage-maintenance-12345678", blocker_record=_blocker_record(controller, assessment, observation), blocker_still_true="YES", command_id="command-maintenance-once-12345678")
    with pytest.raises(WorkflowV2ControllerError, match="budget exhausted"):
        controller.revalidate_blocker("stage-maintenance-12345678", blocker_record={**_blocker_record(controller, assessment, observation), "revision": controller.revision}, blocker_still_true="YES", command_id="command-maintenance-twice-12345678")


def test_transport_and_semantic_failure_classes_are_not_collapsed():
    _controller_instance, _attempt, observation, assessment, _decision = _blocked_controller()
    transport = copy.deepcopy(observation)
    transport["failure"]["failure_class"] = "TRANSPORT_FAILURE"
    transport["failure"]["code"] = "TRANSPORT_TIMEOUT"
    transport["observation_id"] = observation_identity(transport)
    assert classify_blocker(assessment=assessment, observation=transport, failure=transport["failure"])["failure_class"] == "TRANSPORT_FAILURE"
    assert classify_blocker(assessment=assessment, observation=observation, missing_thing_owner="EXTERNAL")["failure_class"] == "EXTERNAL_BLOCKER"


def test_no_human_relay_is_encoded_for_automatic_routes():
    _controller_instance, _attempt, observation, assessment, _decision = _blocked_controller()
    derived = classify_blocker(assessment={**assessment, "checks": {"missing": "BLOCKED_BY_MISSING_INPUT_AND_CAPABILITY"}}, observation=observation)
    assert derived["recommended_action"] == "WORK_REMAINING"
    assert derived["recoverability"] == "TECHNICAL_RECOVERABLE"


def test_product_resume_starts_stage_owned_generation_without_human_relay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tests.test_product_workflow_runtime import _approved_runtime, _stage, _write_machine_config

    root = tmp_path / "product-maintenance"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    runtime = _approved_runtime(root, config_path)
    stage = _stage(runtime, stage_id="stage-product-maintenance")
    runtime.controller.register_stage(stage, command_id="command-product-maintenance-register-12345678")
    runtime.controller.start(stage["stage_id"], command_id="command-product-maintenance-start-12345678")
    request = {
        "stage_id": stage["stage_id"], "objective": "bounded stage-owned generation", "iteration_index": 1,
        "allowed_paths": [".tmp"], "protected_paths": [".git", ".workflow-v2", ".research"],
        "required_changed_path": ".tmp/product-maintenance-result.json",
    }
    submitted = runtime.controller.request_execution(stage["stage_id"], request=request, command_id="command-product-maintenance-request-12345678")
    attempt = submitted["attempt"]
    operation = submitted["operation"]
    observation = {
        "schema_version": "provider_observation.v2", "stage_id": stage["stage_id"], "objective_identity": stage["objective_fingerprint"],
        "iteration_id": attempt["iteration_id"], "attempt_id": attempt["attempt_id"], "provider_operation_id": operation["operation_id"],
        "result_identity": "provider-result-product-maintenance-12345678", "provider_result_digest": "provider-result-product-maintenance-12345678",
        "evidence_manifest_digest": "manifest-product-maintenance-12345678", "provider_terminal_status": "SUCCEEDED",
        "raw_provider_claim": {"status": "SUCCEEDED"}, "provenance": {"provider": "fixture-provider", "engine_digest": "engine-product-maintenance-12345678"},
        "failure": None, "outputs": {}, "immutable": True,
    }
    observation["observation_id"] = observation_identity(observation)
    manifest = {
        "schema_version": "evidence_manifest.v2", "manifest_id": "manifest-product-maintenance-12345678", "stage_id": stage["stage_id"], "attempt_id": attempt["attempt_id"],
        "required_artifact_paths": [".tmp/product-maintenance-result.json"], "changed_paths": [".tmp/product-maintenance-result.json"], "allowed_paths": [".tmp"],
        "protected_paths": [".git", ".workflow-v2", ".research"], "path_inventory": [{"path": ".tmp/product-maintenance-result.json", "sha256": "artifact-product-maintenance-12345678"}], "complete": True,
    }
    runtime.controller.record_observation(stage["stage_id"], observation, effect_state="SETTLED", command_id="command-product-maintenance-observe-12345678")
    assessment = assess_observation(observation, manifest, baseline_digest=stage["baseline_digest"], validator_code_digest="validator-product-maintenance-12345678", validation_contract_revision="admission.v2", checks={"s4": "BLOCKED_BY_MISSING_INPUT_AND_CAPABILITY"})
    runtime.controller.assess_result(stage["stage_id"], assessment, command_id="command-product-maintenance-assess-12345678")
    decision = {"schema_version": "decision.v2", "decision_id": "decision-product-maintenance-12345678", "actor_kind": "GPT", "boundary": "TECHNICAL_REVIEW", "subject_id": assessment["assessment_id"], "subject_digest": assessment["assessment_id"], "subject_version": 1, "allowed_choices": ["BLOCKED"], "requested_action": "APPLY_GPT_DECISION", "provenance": {"request_count": 1, "conversation_id": "conversation-product-maintenance-12345678", "response_digest": "response-product-maintenance-12345678", "packet_digest": "packet-product-maintenance-12345678"}, "supersedes": None}
    runtime.controller.apply_gpt_decision(stage["stage_id"], decision, choice="BLOCKED", command_id="command-product-maintenance-blocked-12345678")

    class FakeProvider:
        def execute(self, execution_request):
            output = Path(execution_request.workspace_root) / execution_request.metadata["required_changed_path"]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("generation started\n", encoding="utf-8")
            return ExecutionResult(provider_id="fixture-provider", result={
                "schema_version": "codex_result.v1", "plan_id": execution_request.plan_id, "stage_id": execution_request.stage_id,
                "task_id": execution_request.task_id, "iteration_index": execution_request.iteration_index, "status": "SUCCEEDED",
                "summary": "bounded generation marker", "changed_files": [], "tests": [], "measurements": {}, "evidence_refs": [],
                "review_artifacts": [], "problems_discovered": [], "abstraction_layer": "fixture", "stage_ready": False,
                "user_visible_failure": False, "human_gate_required": False, "decision_reason": "marker only", "stop_reason": "test",
            })

    config = load_runtime_composition_config(root, config_path=config_path)
    resumed = ProductWorkflowRuntime(root, config=config, maintenance_capability=FakeProvider()).resume()
    assert resumed["generation_started"] is True
    assert resumed["human_intervention_count"] == 0
    assert resumed["stage_owned_output_missing"] is True
    assert resumed["technical_recovery"]["status"] == "PASS"


def test_product_resume_auto_advances_exhausted_iteration_after_continue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tests.test_product_workflow_runtime import _approved_runtime, _stage as product_stage, _write_machine_config

    root = tmp_path / "product-continue-handoff"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    runtime = _approved_runtime(root, config_path)
    stage = product_stage(runtime, stage_id="stage-product-continue-handoff")
    stage["budgets"] = {**stage["budgets"], "max_attempts_per_iteration": 1, "max_attempts_total": 2}
    runtime.controller.register_stage(stage, command_id="command-product-continue-register-12345678")
    runtime.controller.start(stage["stage_id"], command_id="command-product-continue-start-12345678")
    request = {
        "stage_id": stage["stage_id"], "objective": "bounded provider recovery", "iteration_index": 1,
        "allowed_paths": [".tmp"], "protected_paths": [".git", ".workflow-v2", ".research"],
        "required_changed_path": ".tmp/product-continue-result.json",
    }
    submitted = runtime.controller.request_execution(stage["stage_id"], request=request, command_id="command-product-continue-request-12345678")
    attempt = submitted["attempt"]
    observation = _failed_observation(attempt, submitted["operation"]["operation_id"])
    manifest = {
        **_manifest(attempt["attempt_id"]),
        "manifest_id": "manifest-product-continue-12345678",
        "stage_id": stage["stage_id"],
        "required_artifact_paths": [".tmp/product-continue-result.json"],
        "changed_paths": [".tmp/product-continue-result.json"],
        "allowed_paths": [".tmp"],
        "protected_paths": [".git", ".workflow-v2", ".research"],
        "path_inventory": [{"path": ".tmp/product-continue-result.json", "sha256": "artifact-product-continue-12345678"}],
    }
    observation["evidence_manifest_digest"] = manifest["manifest_id"]
    observation["observation_id"] = observation_identity(observation)
    runtime.controller.record_observation(stage["stage_id"], observation, effect_state="SETTLED", command_id="command-product-continue-observe-12345678")
    assessment = assess_observation(
        observation, manifest, baseline_digest=stage["baseline_digest"],
        validator_code_digest="validator-product-continue-12345678", validation_contract_revision="admission.v2",
    )
    runtime.controller.assess_result(stage["stage_id"], assessment, command_id="command-product-continue-assess-12345678")

    resumed_runtime = ProductWorkflowRuntime(
        root, config=load_runtime_composition_config(root, config_path=config_path),
        maintenance_capability=None,
    )
    monkeypatch.setattr(
        resumed_runtime,
        "_consult",
        lambda _request, *, purpose: {
            "purpose": purpose, "decision": "CONTINUE", "response_digest": "response-product-continue-12345678",
            "consultation_id": "consultation-product-continue-12345678", "receipt_path": None,
            "request_count": 1, "conversation_id": "conversation-product-continue-12345678",
            "conversation_validated": True, "packet_digest": "packet-product-continue-12345678",
        },
    )

    class FakeProvider:
        def execute(self, execution_request):
            output = Path(execution_request.workspace_root) / execution_request.metadata["required_changed_path"]
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("generation started\n", encoding="utf-8")
            return ExecutionResult(provider_id="fixture-provider", result={
                "schema_version": "codex_result.v1", "plan_id": execution_request.plan_id, "stage_id": execution_request.stage_id,
                "task_id": execution_request.task_id, "iteration_index": execution_request.iteration_index, "status": "SUCCEEDED",
                "summary": "bounded generation marker", "changed_files": [], "tests": [], "measurements": {}, "evidence_refs": [],
                "review_artifacts": [], "problems_discovered": [], "abstraction_layer": "fixture", "stage_ready": False,
                "user_visible_failure": False, "human_gate_required": False, "decision_reason": "marker only", "stop_reason": "test",
            })

    resumed_runtime.maintenance_capability = FakeProvider()
    resumed = resumed_runtime.resume()

    assert resumed["technical_gpt_escalation"]["decision"] == "CONTINUE"
    assert resumed["technical_recovery"]["status"] == "PASS"
    assert resumed.get("auto_next_iteration") is not None or resumed["canonical"]["stage"]["iteration_count"] == 2
    assert resumed["canonical"]["stage"]["iteration_count"] == 2
    assert resumed["provider_execution"]["attempt"]["iteration_id"] != attempt["iteration_id"]
    assert resumed["human_intervention_count"] == 0
