from __future__ import annotations

import copy

import pytest

from src.execution_profile import default_execution_profile
from src.workflow_v2_controller import StageController, WorkflowV2ControllerError


def _stage(stage_id: str = "stage-loop-12345678", *, all_ones: bool = True) -> dict:
    budget = {
        "max_iterations": 1 if all_ones else 2,
        "max_attempts_per_iteration": 1,
        "max_attempts_total": 1 if all_ones else 2,
        "max_revalidation_ops": 1 if all_ones else 2,
        "max_validator_revisions": 1 if all_ones else 2,
        "max_dependency_nodes": 1 if all_ones else 4,
        "max_dependency_depth": 1 if all_ones else 2,
        "max_descendant_attempts": 1 if all_ones else 4,
    }
    return {
        "schema_version": "stage.v2",
        "stage_id": stage_id,
        "workspace_id": "workspace-loop-12345678",
        "project_id": "project-loop",
        "objective_fingerprint": "objective-loop-12345678",
        "target_identity": "local/bounded-loop",
        "required_capabilities": ["python"],
        "purpose": "DOMAIN",
        "repair_depth": 0,
        "baseline_digest": "baseline-loop-12345678",
        "status": "PLANNED",
        "budgets": budget,
        "owner_stage_id": None,
        "current_iteration_id": None,
        "current_assessment_id": None,
        "predecessor_stage_id": None,
        "semantic_baseline_diff_digest": None,
    }


def _controller(stage_id: str = "stage-loop-12345678", *, all_ones: bool = True) -> StageController:
    controller = StageController(workspace_id="workspace-loop-12345678")
    controller.register_stage(_stage(stage_id, all_ones=all_ones), command_id="command-loop-register-12345678")
    controller.start(stage_id, command_id="command-loop-start-12345678")
    return controller


def test_legacy_budget_is_audited_and_migrated_by_one_canonical_command():
    controller = _controller()
    status = controller.budget_authority_status(
        "stage-loop-12345678",
        execution_profile=default_execution_profile(),
    )
    assert status["BUDGET_ORIGIN"] == "LEGACY_UNATTRIBUTED_STAGE_REGISTRATION"
    assert status["migratable"] is True

    receipt = controller.migrate_budget(
        "stage-loop-12345678",
        payload={
            "authorization": "CURRENT_EXECUTION_PROFILE_MIGRATION",
            "authority": "PROJECT_BRIEF",
            "profile_name": "autonomous_research",
            "profile_version": "1",
            "budget_policy": default_execution_profile()["budget_policy"],
        },
        command_id="command-loop-budget-migration-12345678",
    )
    assert receipt["migrated"] is True
    assert controller.show_stage("stage-loop-12345678")["budgets"]["max_iterations"] == 2
    assert controller.state["budget_audits"][0]["human_intervention_count"] == 0


def test_unfinished_objective_cannot_stop_without_explicit_human_cancellation():
    controller = _controller(all_ones=False)
    validation = controller.validate_termination("stage-loop-12345678")
    assert validation["allowed"] is False
    assert validation["reason"] == "UNFINISHED_OBJECTIVE"
    with pytest.raises(WorkflowV2ControllerError, match="AUTOMATED_STOP_FORBIDDEN"):
        controller.stop("stage-loop-12345678", command_id="command-loop-stop-forbidden-12345678")

    stopped = controller.stop(
        "stage-loop-12345678",
        explicit_human_termination=True,
        termination_reason="EXPLICIT_HUMAN_CANCELLATION",
        command_id="command-loop-stop-human-12345678",
    )
    assert stopped["stage"]["status"] == "STOPPED"


def test_legal_next_action_is_not_a_technical_failure_terminal_state():
    controller = _controller(all_ones=False)
    action = controller.resolve_legal_next_action("stage-loop-12345678")
    assert action["action"] == "REQUEST_EXECUTION"
    assert action["no_legal_automated_next_action"] is False
    assert controller.validate_termination("stage-loop-12345678")["allowed"] is False


def test_unvalidated_blocked_decision_is_not_genuine_blocked_termination():
    from tests.test_blocker_maintenance import _blocked_controller

    controller, _attempt, _observation, assessment, _decision = _blocked_controller()
    assert controller.validate_termination("stage-maintenance-12345678")["allowed"] is False

    decision = {
        "schema_version": "decision.v2",
        "decision_id": "decision-loop-strict-blocked-12345678",
        "actor_kind": "GPT",
        "boundary": "TECHNICAL_REVIEW",
        "subject_id": assessment["assessment_id"],
        "subject_digest": assessment["assessment_id"],
        "subject_version": 1,
        "allowed_choices": ["BLOCKED"],
        "requested_action": "APPLY_GPT_DECISION",
        "provenance": {
            "request_count": 1,
            "conversation_id": "conversation-loop-strict-12345678",
            "response_digest": "response-loop-strict-12345678",
            "packet_digest": "packet-loop-strict-12345678",
        },
        "supersedes": None,
    }
    controller.apply_gpt_decision(
        "stage-maintenance-12345678",
        decision,
        choice="BLOCKED",
        strict_blocked_validation=True,
        blocked_validation={
            "blocker_still_true": "YES",
            "auto_recovery_exhausted": True,
            "gpt_technical_escalation_completed": True,
            "no_legal_automated_next_action": True,
        },
        command_id="command-loop-strict-blocked-apply-12345678",
    )
    validation = controller.validate_termination("stage-maintenance-12345678")
    assert validation["allowed"] is True
    assert validation["reason"] == "GENUINE_BLOCKED"

