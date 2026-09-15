from __future__ import annotations

import copy
import tempfile

import pytest

from src.contracts import sha256_json
from src.workflow_v2_contracts import assess_observation, assessment_identity, observation_identity
from src.workflow_v2_controller import StageController, WorkflowV2ControllerError
import src.product_workflow_runtime as product_runtime
from src.product_workflow_runtime import ProductWorkflowRuntime
from src.runtime_composition import load_runtime_composition_config
from src.workflow_runtime import WorkflowRuntimeError

from tests.test_product_workflow_runtime import _approved_runtime, _write_machine_config


STAGE_ID = "stage-objective-maintenance-12345678"
PROJECT_ID = "project-objective-maintenance-12345678"
OBJECTIVE = "objective-objective-maintenance-12345678"


def _stage(*, state_path=None):
    return {
        "schema_version": "stage.v2",
        "stage_id": STAGE_ID,
        "workspace_id": "workspace-objective-maintenance-12345678",
        "project_id": PROJECT_ID,
        "objective_fingerprint": OBJECTIVE,
        "target_identity": "target/facade",
        "required_capabilities": ["python"],
        "purpose": "DOMAIN",
        "repair_depth": 0,
        "baseline_digest": "baseline-objective-maintenance-12345678",
        "status": "PLANNED",
        "budgets": {
            "max_iterations": 1,
            "max_attempts_per_iteration": 1,
            "max_attempts_total": 1,
            "max_revalidation_ops": 1,
            "max_validator_revisions": 1,
            "max_dependency_nodes": 1,
            "max_dependency_depth": 1,
            "max_descendant_attempts": 1,
        },
        "owner_stage_id": None,
        "current_iteration_id": None,
        "current_assessment_id": None,
        "predecessor_stage_id": None,
        "semantic_baseline_diff_digest": None,
    }


def _manifest(attempt_id, manifest_id):
    return {
        "schema_version": "evidence_manifest.v2",
        "manifest_id": manifest_id,
        "stage_id": STAGE_ID,
        "attempt_id": attempt_id,
        "required_artifact_paths": ["artifacts/result.json"],
        "changed_paths": ["artifacts/result.json"],
        "allowed_paths": ["artifacts"],
        "protected_paths": [".git", "secrets"],
        "path_inventory": [{"path": "artifacts/result.json", "sha256": "artifact-objective-12345678"}],
        "complete": True,
    }


def _observation(attempt, *, objective=OBJECTIVE):
    value = {
        "schema_version": "provider_observation.v2",
        "observation_id": "pending",
        "stage_id": STAGE_ID,
        "objective_identity": objective,
        "iteration_id": attempt["iteration_id"],
        "attempt_id": attempt["attempt_id"],
        "provider_operation_id": "operation-objective-maintenance-12345678",
        "result_identity": "provider-objective-maintenance-12345678",
        "provider_result_digest": "provider-objective-maintenance-12345678",
        "evidence_manifest_digest": "manifest-objective-maintenance-12345678",
        "provider_terminal_status": "SUCCEEDED",
        "raw_provider_claim": {"status": "SUCCEEDED"},
        "provenance": {"provider": "fixture-provider", "engine_digest": "engine-objective-12345678"},
        "failure": None,
        "outputs": {"files": ["artifacts/result.json"]},
        "immutable": True,
    }
    value["observation_id"] = observation_identity(value)
    return value


def _controller():
    controller = StageController(workspace_id="workspace-objective-maintenance-12345678")
    controller.register_stage(_stage())
    controller.start(STAGE_ID, command_id="command-objective-maintenance-start-12345678")
    return controller


def _run_observation(controller, *, objective=OBJECTIVE):
    requested = controller.request_execution(STAGE_ID, command_id="command-objective-maintenance-request-12345678")
    attempt = requested["attempt"]
    observation = _observation(attempt, objective=objective)
    observation["provider_operation_id"] = requested["operation"]["operation_id"]
    observation["observation_id"] = observation_identity(observation)
    manifest = _manifest(attempt["attempt_id"], observation["evidence_manifest_digest"])
    return requested, observation, manifest


def _gpt(assessment, *, objective=None, choice="BLOCKED"):
    return {
        "schema_version": "decision.v2",
        "decision_id": "decision-objective-maintenance-12345678",
        "actor_kind": "GPT",
        "boundary": "TECHNICAL_REVIEW",
        "subject_id": assessment["assessment_id"],
        "subject_digest": assessment["assessment_id"],
        "subject_version": 1,
        "allowed_choices": [choice],
        "requested_action": "Apply the technical review result.",
        "provenance": {
            "request_count": 1,
            "conversation_id": "conversation-objective-maintenance-12345678",
            "response_digest": "response-objective-maintenance-12345678",
            "packet_digest": "packet-objective-maintenance-12345678",
        },
        "supersedes": None,
        **({"objective_identity": objective} if objective is not None else {}),
    }


def test_new_request_and_observation_are_objective_bound_and_mismatch_fails_fast():
    controller = _controller()
    requested, observation, manifest = _run_observation(controller, objective="objective-other-12345678")
    with pytest.raises(WorkflowV2ControllerError, match="OBSERVATION_OBJECTIVE_MISMATCH"):
        controller.record_observation(STAGE_ID, observation, effect_state="SETTLED", command_id="command-objective-mismatch-12345678")
    assert requested["attempt"]["project_id"] == PROJECT_ID
    assert requested["attempt"]["objective_identity"] == OBJECTIVE
    assert manifest["manifest_id"] == observation["evidence_manifest_digest"]


def test_assessment_objective_mismatch_is_rejected_before_review():
    controller = _controller()
    requested, observation, manifest = _run_observation(controller)
    controller.record_observation(STAGE_ID, observation, effect_state="SETTLED", command_id="command-objective-observe-12345678")
    assessment = assess_observation(observation, manifest, baseline_digest="baseline-objective-maintenance-12345678", validator_code_digest="validator-objective-12345678", validation_contract_revision="admission.v2")
    wrong = copy.deepcopy(assessment)
    wrong["objective_identity"] = "objective-other-12345678"
    wrong["assessment_id"] = assessment_identity(
        stage_id=wrong["stage_id"], baseline_digest=wrong["baseline_digest"], iteration_id=wrong["iteration_id"],
        attempt_id=wrong["attempt_id"], provider_result_digest=wrong["provider_result_digest"],
        evidence_manifest_digest=wrong["evidence_manifest_digest"], validator_code_digest=wrong["validator_code_digest"],
        validation_contract_revision=wrong["validation_contract_revision"],
        correction_receipt_digest=wrong["correction_receipt_digest"], objective_identity=wrong["objective_identity"],
    )
    with pytest.raises(WorkflowV2ControllerError, match="ASSESSMENT_OBJECTIVE_MISMATCH"):
        controller.assess_result(STAGE_ID, wrong, command_id="command-objective-assessment-mismatch-12345678")


def test_misaligned_assessment_is_superseded_append_only_and_restores_execution():
    with tempfile.TemporaryDirectory() as directory:
        state_path = f"{directory}/journal.json"
        controller = StageController(workspace_id="workspace-objective-maintenance-12345678", state_path=state_path)
        controller.register_stage(_stage(), command_id="command-objective-history-register-12345678")
        controller.start(STAGE_ID, command_id="command-objective-history-start-12345678")
        requested, observation, manifest = _run_observation(controller)
        # Build the historical assessment before the controller canonicalizes
        # the legacy observation with the newly enforced binding fields.
        legacy_observation = copy.deepcopy(observation)
        for key in ("objective_identity", "provider_operation_id", "result_identity"):
            legacy_observation.pop(key)
        legacy_observation["observation_id"] = observation_identity(legacy_observation)
        legacy_assessment = assess_observation(legacy_observation, manifest, baseline_digest="baseline-objective-maintenance-12345678", validator_code_digest="validator-objective-12345678", validation_contract_revision="admission.v2")
        controller.record_observation(STAGE_ID, legacy_observation, effect_state="SETTLED", command_id="command-objective-history-observe-12345678")
        controller.assess_result(STAGE_ID, legacy_assessment, command_id="command-objective-history-assess-12345678")
        blocked = _gpt(legacy_assessment)
        controller.apply_gpt_decision(STAGE_ID, blocked, choice="BLOCKED", command_id="command-objective-history-block-12345678")
        before = copy.deepcopy(controller.state)
        proof = {
            "assessment_id": legacy_assessment["assessment_id"],
            "consultation_id": "CONSULT-objective-maintenance-12345678",
            "old_objective_identity": None,
            "current_objective_identity": OBJECTIVE,
            "objective_mismatch_proven": True,
            "validation_evidence_only": True,
            "real_current_objective_scientific_result_present": False,
        }
        result = controller.supersede_assessment(
            STAGE_ID,
            assessment_id=legacy_assessment["assessment_id"],
            consultation_id=proof["consultation_id"],
            reason="STAGE_OBJECTIVE_EVIDENCE_MISALIGNMENT",
            misalignment_proof=proof,
            misalignment_evidence_digest=sha256_json(proof),
            maintenance_authority="maintenance-authority-objective-12345678",
            supersession_id="assessment-supersession-objective-12345678",
            command_id="command-objective-supersede-12345678",
        )
        assert result["stage"]["stage_id"] == STAGE_ID
        assert result["stage"]["next_action"] == "REQUEST_EXECUTION"
        assert result["supersession"]["status"] == "NOT_APPLICABLE_TO_CURRENT_OBJECTIVE"
        current = controller.state
        assert current["observations"] == before["observations"]
        assert current["assessments"] == before["assessments"]
        assert current["decisions"] == before["decisions"]
        assert current["stages"][STAGE_ID]["current_assessment_id"] is None
        epoch = result["supersession"]["new_assessment_epoch_id"]
        requested_again = controller.request_execution(STAGE_ID, reason="OBJECTIVE_SUPERSESSION_RECOVERY", command_id="command-objective-recovery-request-12345678")
        assert requested_again["attempt"]["assessment_epoch_id"] == epoch
        assert controller.show_stage(STAGE_ID)["next_action"] == "RECORD_OBSERVATION"
        assert StageController.from_state(state_path).state["assessment_supersessions"] == current["assessment_supersessions"]


def test_true_scientific_blocked_cannot_be_superseded():
    controller = _controller()
    requested, observation, manifest = _run_observation(controller)
    controller.record_observation(STAGE_ID, observation, effect_state="SETTLED", command_id="command-objective-true-observe-12345678")
    assessment = assess_observation(observation, manifest, baseline_digest="baseline-objective-maintenance-12345678", validator_code_digest="validator-objective-12345678", validation_contract_revision="admission.v2")
    controller.assess_result(STAGE_ID, assessment, command_id="command-objective-true-assess-12345678")
    controller.apply_gpt_decision(STAGE_ID, _gpt(assessment, objective=OBJECTIVE), choice="BLOCKED", command_id="command-objective-true-block-12345678")
    proof = {
        "assessment_id": assessment["assessment_id"],
        "consultation_id": "CONSULT-objective-true-12345678",
        "old_objective_identity": OBJECTIVE,
        "current_objective_identity": OBJECTIVE,
        "objective_mismatch_proven": True,
        "validation_evidence_only": True,
        "real_current_objective_scientific_result_present": False,
    }
    with pytest.raises(WorkflowV2ControllerError, match="TRUE_SCIENTIFIC_BLOCKED_NOT_SUPERSEDABLE"):
        controller.supersede_assessment(
            STAGE_ID, assessment_id=assessment["assessment_id"], consultation_id=proof["consultation_id"],
            reason="STAGE_OBJECTIVE_EVIDENCE_MISALIGNMENT", misalignment_proof=proof,
            misalignment_evidence_digest=sha256_json(proof), maintenance_authority="maintenance-authority-objective-true-12345678",
            command_id="command-objective-true-supersede-12345678",
        )


def test_objective_bound_rejected_assessment_reaches_gpt_with_visible_binding(
    tmp_path, monkeypatch
):
    root = tmp_path / "objective-review"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    runtime = _approved_runtime(root, config_path)
    stage = _stage()
    stage["workspace_id"] = runtime.controller.workspace_id
    stage["project_id"] = runtime.intake.state["project_id"]
    runtime.controller.register_stage(stage, command_id="command-objective-review-register-12345678")
    runtime.controller.start(STAGE_ID, command_id="command-objective-review-start-12345678")
    requested = runtime.controller.request_execution(
        STAGE_ID,
        request={"objective": "objective-bound scientific attempt"},
        command_id="command-objective-review-request-12345678",
    )
    attempt = requested["attempt"]
    manifest = _manifest(attempt["attempt_id"], "manifest-objective-review-12345678")
    observation = {
        "schema_version": "provider_observation.v2",
        "observation_id": "pending",
        "stage_id": STAGE_ID,
        "objective_identity": OBJECTIVE,
        "iteration_id": attempt["iteration_id"],
        "attempt_id": attempt["attempt_id"],
        "provider_operation_id": requested["operation"]["operation_id"],
        "result_identity": "provider-objective-review-12345678",
        "provider_result_digest": "provider-objective-review-12345678",
        "evidence_manifest_digest": manifest["manifest_id"],
        "provider_terminal_status": "FAILED",
        "raw_provider_claim": {"status": "FAILED"},
        "provenance": {"provider": "fixture-provider", "engine_digest": "engine-objective-review-12345678"},
        "failure": None,
        "outputs": {"files": []},
        "immutable": True,
    }
    observation["observation_id"] = observation_identity(observation)
    runtime.controller.record_observation(
        STAGE_ID, observation, effect_state="SETTLED", command_id="command-objective-review-observe-12345678"
    )
    assessment = assess_observation(
        observation,
        manifest,
        baseline_digest=stage["baseline_digest"],
        validator_code_digest="validator-objective-review-12345678",
        validation_contract_revision="admission.v2",
    )
    runtime.controller.assess_result(
        STAGE_ID, assessment, command_id="command-objective-review-assess-12345678"
    )
    assert assessment["verdict"] == "REJECTED"

    calls = []

    def run_bridge(_prompt, **kwargs):
        calls.append(kwargs)
        return {
            "status": "complete",
            "mode": "fresh",
            "response_text": "WORKFLOW_DECISION: BLOCKED",
            "consultation_id": "consultation-objective-review-12345678",
            "request_count": 1,
            "receipt": {
                "status": "complete",
                "mode": "fresh",
                "consultation_id": "consultation-objective-review-12345678",
                "request_count": 1,
                "conversation_id": "conversation-objective-review-12345678",
                "conversation_validated": True,
                "context_pack": {"pack_sha256": "packet-objective-review-12345678"},
            },
        }

    monkeypatch.setattr(product_runtime, "subprocess_bridge_runner", run_bridge)
    pack = {
        "mode": "fresh",
        "projectGoal": "objective-bound scientific review",
        "currentStageGoal": "review the current scientific attempt",
        "latestResult": {"actualWork": "one bounded attempt", "success": [], "failure": ["scientific input absent"]},
        "evidence": [],
        "evidenceRoots": ["."],
        "PROJECT_ID": stage["project_id"],
        "STAGE_ID": STAGE_ID,
        "OBJECTIVE_IDENTITY": OBJECTIVE,
        "STAGE_GOAL": "review the current scientific attempt",
        "ASSESSMENT_ID": assessment["assessment_id"],
        "OBSERVATION_ID": observation["observation_id"],
    }
    result = runtime.run(
        {
            "operation": "CONSULT_REVIEW",
            "stage_id": STAGE_ID,
            "prompt": "Review only the current objective-bound scientific result.",
            "context_pack": pack,
        }
    )
    assert result["technical_review"]["decision"] == "BLOCKED"
    assert len(calls) == 1
    excerpt = calls[0]["context_pack"]["sourceContext"][-1]["excerpt"]
    for key, value in {
        "PROJECT_ID": stage["project_id"],
        "STAGE_ID": STAGE_ID,
        "OBJECTIVE_IDENTITY": OBJECTIVE,
        "STAGE_GOAL": "review the current scientific attempt",
        "ASSESSMENT_ID": assessment["assessment_id"],
        "OBSERVATION_ID": observation["observation_id"],
    }.items():
        assert f"{key}: {value}" in excerpt
