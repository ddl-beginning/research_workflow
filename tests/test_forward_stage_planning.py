from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.contracts import canonical_json, sha256_json
from src.forward_stage_planning import (
    FORWARD_STAGE_STATE_RELATIVE_PATH,
    ForwardStagePlanningError,
    HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH,
    UNIFIED_WORKFLOW_ENTRY_STAGE_ID,
    load_unified_workflow_entry_stage_plan,
    plan_unified_workflow_entry_stage,
    start_unified_workflow_entry_stage,
    verify_unified_workflow_entry_stage_plan,
)
from src.stage_controller import StageController, StageState


def _contract(root: Path, stage_id: str, stage_name: str) -> dict[str, object]:
    return {
        "schema_version": "stage_contract.v1",
        "plan_id": f"plan-{stage_id}",
        "project_id": "project-forward-fixture",
        "repository_root": root.as_posix(),
        "stage_id": stage_id,
        "stage_name": stage_name,
        "project_goal": "a bounded workflow",
        "stage_goal": "produce a reviewed result",
        "user_visible_goal": "show the reviewed result",
        "inputs": ["input.txt"],
        "protected_paths": [".git", ".research/stage-state.json"],
        "allowed_paths": [f".research/stages/{stage_id}"],
        "acceptance_description": "the bounded result is reviewed",
        "required_checks": ["unit"],
        "review_artifact_requirements": ["summary"],
        "baseline": {
            "schema_version": "forward_fixture_baseline.v1",
            "project_id": "project-forward-fixture",
        },
        "status": "PLANNED",
    }


def _fixture(tmp_path: Path) -> tuple[Path, str, dict[str, object]]:
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "input.txt").write_text("fixture\n", encoding="utf-8")
    source_id = "source-stage"
    source = _contract(root, source_id, "Source Stage")
    state_path = root / FORWARD_STAGE_STATE_RELATIVE_PATH
    controller = StageController(source, state_path=state_path)
    controller.start_stage(source_id, actor="fixture", rationale="fixture start")
    controller.mark_stage_ready(
        {
            "status": "SUCCEEDED",
            "baseline_digest": controller.state["baseline_digest"],
            "required_checks": {"unit": "PASS"},
            "review_artifacts": [{"type": "summary", "uri": "summary.txt"}],
        },
        stage_id=source_id,
    )
    controller.approve_stage(source_id, actor="fixture", rationale="fixture closeout")
    acceptance_path = root / HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH
    acceptance_path.parent.mkdir(parents=True, exist_ok=True)
    acceptance_path.write_text(
        json.dumps(
            {
                "schema_version": "human_design_accept_receipt.v1",
                "decision": "ACCEPT",
                "project_id": "project-forward-fixture",
                "actor": "fixture-human",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    acceptance_hash = hashlib.sha256(acceptance_path.read_bytes()).hexdigest()
    forward = _contract(root, UNIFIED_WORKFLOW_ENTRY_STAGE_ID, "Unified Workflow Entry V1")
    forward["source_stage_id"] = source_id
    forward["source_stage_result_digest"] = controller.show_stage(source_id)["stage_result_binding"]["result_digest"]
    forward["metadata"] = {"stage_kind": "unified_workflow_entry", "one_shot_dispatch": True}
    return root, acceptance_hash, forward


def test_plans_through_existing_controller_without_overwriting_legacy_plan(tmp_path: Path) -> None:
    root, acceptance_hash, contract = _fixture(tmp_path)
    legacy = root / ".research" / "stage-planning" / "STAGE_PLAN.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"legacy":true}\n', encoding="utf-8")
    before = legacy.read_bytes()

    plan = plan_unified_workflow_entry_stage(
        root,
        stage_contract=contract,
        acceptance_source_hash=acceptance_hash,
        source_stage_id="source-stage",
    )

    assert plan["stage_id"] == UNIFIED_WORKFLOW_ENTRY_STAGE_ID
    assert plan["stage_status"] == StageState.PLANNED.value
    assert plan["source_stage_status"] == StageState.APPROVED.value
    assert plan["acceptance_source_hash"] == acceptance_hash
    assert legacy.read_bytes() == before
    state = StageController.from_state(root / FORWARD_STAGE_STATE_RELATIVE_PATH)
    assert state.show_stage("source-stage")["status"] == StageState.APPROVED.value
    assert state.show_stage(UNIFIED_WORKFLOW_ENTRY_STAGE_ID)["status"] == StageState.PLANNED.value


def test_start_uses_controller_and_preserves_source_stage(tmp_path: Path) -> None:
    root, acceptance_hash, contract = _fixture(tmp_path)
    plan_unified_workflow_entry_stage(
        root,
        stage_contract=contract,
        acceptance_source_hash=acceptance_hash,
        source_stage_id="source-stage",
    )
    started = start_unified_workflow_entry_stage(root, actor="fixture", rationale="explicit start")
    assert started["stage"]["status"] == StageState.ACTIVE.value
    state = StageController.from_state(root / FORWARD_STAGE_STATE_RELATIVE_PATH)
    assert state.show_stage("source-stage")["status"] == StageState.APPROVED.value
    assert state.show_stage(UNIFIED_WORKFLOW_ENTRY_STAGE_ID)["status"] == StageState.ACTIVE.value
    assert verify_unified_workflow_entry_stage_plan(root)["passed"] is True


def test_same_contract_and_acceptance_is_idempotent(tmp_path: Path) -> None:
    root, acceptance_hash, contract = _fixture(tmp_path)
    first = plan_unified_workflow_entry_stage(
        root,
        stage_contract=contract,
        acceptance_source_hash=acceptance_hash,
        source_stage_id="source-stage",
    )
    plan_path = root / first["stage_plan_path"]
    contract_path = root / first["stage_contract_path"]
    state_path = root / FORWARD_STAGE_STATE_RELATIVE_PATH
    snapshots = (plan_path.read_bytes(), contract_path.read_bytes(), state_path.read_bytes())

    second = plan_unified_workflow_entry_stage(
        root,
        stage_contract=copy.deepcopy(contract),
        acceptance_source_hash=acceptance_hash,
        source_stage_id="source-stage",
    )
    assert second["idempotent_reuse"] is True
    assert (plan_path.read_bytes(), contract_path.read_bytes(), state_path.read_bytes()) == snapshots


def test_non_approved_source_and_missing_acceptance_fail_before_registration(tmp_path: Path) -> None:
    root, acceptance_hash, contract = _fixture(tmp_path)
    state_path = root / FORWARD_STAGE_STATE_RELATIVE_PATH
    controller = StageController.from_state(state_path)
    # A distinct unapproved source proves the source gate without changing the
    # already approved fixture source.
    pending = _contract(root, "pending-source", "Pending Source")
    controller.prepare_stage(pending)
    with pytest.raises(ForwardStagePlanningError) as error:
        plan_unified_workflow_entry_stage(
            root,
            stage_contract={**contract, "source_stage_id": "pending-source"},
            acceptance_source_hash=acceptance_hash,
            source_stage_id="pending-source",
        )
    assert error.value.code == "FORWARD_STAGE_SOURCE_NOT_CLOSED"
    assert UNIFIED_WORKFLOW_ENTRY_STAGE_ID not in StageController.from_state(state_path).state["stages"]

    with pytest.raises(ForwardStagePlanningError) as missing:
        plan_unified_workflow_entry_stage(
            root,
            stage_contract=contract,
            acceptance_source_hash="0" * 64,
            source_stage_id="source-stage",
        )
    assert missing.value.code == "FORWARD_STAGE_ACCEPTANCE_DIGEST_MISMATCH"


def test_provenance_and_acceptance_are_immutable_bindings(tmp_path: Path) -> None:
    root, acceptance_hash, contract = _fixture(tmp_path)
    bad = copy.deepcopy(contract)
    bad["source_stage_result_digest"] = "f" * 64
    with pytest.raises(ForwardStagePlanningError) as error:
        plan_unified_workflow_entry_stage(
            root,
            stage_contract=bad,
            acceptance_source_hash=acceptance_hash,
            source_stage_id="source-stage",
        )
    assert error.value.code == "FORWARD_STAGE_PROVENANCE_MISMATCH"
    assert not (root / ".research" / "stages" / UNIFIED_WORKFLOW_ENTRY_STAGE_ID).exists()

    acceptance = root / HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH
    acceptance.write_text('{"decision":"REJECT"}\n', encoding="utf-8")
    with pytest.raises(ForwardStagePlanningError) as rejected:
        plan_unified_workflow_entry_stage(
            root,
            stage_contract=contract,
            acceptance_source_hash=acceptance_hash,
            source_stage_id="source-stage",
        )
    assert rejected.value.code == "FORWARD_STAGE_ACCEPTANCE_INVALID"


def test_explicit_contract_is_not_mutated_and_missing_contract_is_rejected(tmp_path: Path) -> None:
    root, acceptance_hash, contract = _fixture(tmp_path)
    before = canonical_json(contract)
    plan_unified_workflow_entry_stage(
        root,
        stage_contract=contract,
        acceptance_source_hash=acceptance_hash,
        source_stage_id="source-stage",
    )
    assert canonical_json(contract) == before
    with pytest.raises(ForwardStagePlanningError) as missing:
        plan_unified_workflow_entry_stage(root, acceptance_source_hash=acceptance_hash, source_stage_id="source-stage")
    assert missing.value.code == "FORWARD_STAGE_CONTRACT_REQUIRED"


def test_loading_plan_does_not_create_a_second_state_machine(tmp_path: Path) -> None:
    root, acceptance_hash, contract = _fixture(tmp_path)
    plan = plan_unified_workflow_entry_stage(
        root,
        stage_contract=contract,
        acceptance_source_hash=acceptance_hash,
        source_stage_id="source-stage",
    )
    loaded = load_unified_workflow_entry_stage_plan(root)
    assert loaded["plan_id"] == plan["plan_id"]
    assert loaded["authority"]["existing_stage_controller"] is True
    assert loaded["authority"]["no_second_state_machine"] is True
    assert loaded["authority"]["one_shot_dispatch"] is True

