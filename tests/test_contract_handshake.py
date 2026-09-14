from __future__ import annotations

import copy

import pytest

from src.contract_handshake import (
    compare_handshake,
    compare_stage_action_handshake,
    payload_snapshot,
    stage_action_handshake,
    supervisor_handshake,
)
from src.contracts import ContractValidationError
from src.workflow_v2_controller import StageController, WorkflowV2ControllerError, build_registration_payload
from src.workflow_v2_contracts import validate_stage


def _stage() -> dict[str, object]:
    return {
        "schema_version": "stage.v2",
        "stage_id": "stage-contract-repair-12345678",
        "workspace_id": "workspace-contract-repair-12345678",
        "project_id": "project-contract-repair",
        "objective_fingerprint": "objective-contract-repair-12345678",
        "target_identity": "facade/ACQUISITION_REALISM_BRIDGE_S4_S7",
        "required_capabilities": ["python"],
        "purpose": "INFRASTRUCTURE_REPAIR",
        "repair_depth": 1,
        "baseline_digest": "baseline-contract-repair-12345678",
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
        "predecessor_stage_id": "stage-parent-12345678",
        "semantic_baseline_diff_digest": "baseline-diff-contract-repair-12345678",
    }


def test_stage_registration_preserves_schema_version() -> None:
    controller = StageController(workspace_id="workspace-contract-repair-12345678")
    controller.register_stage(_stage(), command_id="command-contract-repair-12345678")
    assert controller.state["stages"][_stage()["stage_id"]]["schema_version"] == "stage.v2"


def test_client_payload_matches_transport_payload() -> None:
    client_payload = build_registration_payload(_stage())
    transport_payload = copy.deepcopy(client_payload)
    assert payload_snapshot(client_payload) == payload_snapshot(transport_payload)


def test_supervisor_receives_schema_version() -> None:
    controller = StageController(workspace_id="workspace-contract-repair-12345678")
    controller.register_stage(_stage(), command_id="command-contract-repair-12345678")
    trace = controller._last_registration_trace
    assert trace is not None
    assert trace["transport_payload"]["root_has_schema_version"] is False
    assert trace["supervisor_received_payload"]["root_has_schema_version"] is True
    assert trace["supervisor_received_payload"]["schema_version_value"] == "stage.v2"


def test_supervisor_accepts_minimal_current_registration() -> None:
    controller = StageController(workspace_id="workspace-contract-repair-12345678")
    result = controller.register_stage(_stage(), command_id="command-contract-repair-12345678")
    assert result["stage"]["status"] == "PLANNED"
    assert result["stage"]["schema_version"] == "stage.v2"


def test_rejects_missing_schema_version_at_expected_level() -> None:
    with pytest.raises(ContractValidationError, match=r"\$: missing required property 'schema_version'"):
        validate_stage({})


def test_schema_version_at_wrong_nesting_is_rejected() -> None:
    controller = StageController(workspace_id="workspace-contract-repair-12345678")
    with pytest.raises(WorkflowV2ControllerError, match="canonical payload envelope"):
        controller.dispatch(
            "REGISTER_STAGE",
            subject_id="stage-contract-repair-12345678",
            payload={"stage_contract": _stage()},
            command_id="command-contract-repair-12345678",
        )


def test_contract_handshake_detects_schema_version_skew() -> None:
    current = supervisor_handshake()
    assert compare_handshake(current)["compatible"] is True
    skewed = {**current, "contract_version": "stage_contract.v1"}
    result = compare_handshake(skewed)
    assert result["compatible"] is False
    assert result["client_contract_version"] == "stage.v2"


def test_stage_action_handshake_detects_plan_boundary_skew() -> None:
    current = supervisor_handshake()
    assert compare_stage_action_handshake(current)["compatible"] is True
    skewed = copy.deepcopy(current)
    skewed["stage_action_handshake"]["actions"]["PLAN_STAGE"]["stage_location"] = "payload.stage"
    result = compare_stage_action_handshake(skewed)
    assert result["compatible"] is False
    assert result["actions"]["PLAN_STAGE"]["compatible"] is False


def test_stage_action_handshake_has_one_canonical_invariant() -> None:
    handshake = stage_action_handshake()
    assert handshake["invariant"] == "STAGE_SCOPED_ACTION_MUST_USE_CANONICAL_STAGE"
    assert handshake["actions"]["REGISTER_STAGE"]["stage_location"] == "payload.stage"
    assert handshake["actions"]["PLAN_STAGE"]["stage_location"] == "request.stage"
    assert handshake["actions"]["START_STAGE"]["canonical_source"] == "resolve_canonical_stage(stage_id)"
