from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

from src.delivery_integration import (
    DeliveryIntegrationError,
    integrate_delivery,
    resolve_authoritative_target,
)
from src.execution_integration_planning import (
    EXECUTION_INTEGRATION_STAGE_ID,
    plan_execution_integration_stage,
    start_execution_integration_stage,
)
from src.stage_controller import StageController


REQUIREMENT_DIGEST = "requirement-digest-v1"
DESIGN_DIGEST = "design-digest-v1"
PLAN_ID = "delivery-plan-v1"
STAGE_ID = "delivery-stage"
TARGET_RELATIVE = ".research/integration/delivered.bin"
RECEIPT_RELATIVE = ".research/integration/delivery-receipt.json"


def _contract(root: Path) -> dict:
    return {
        "schema_version": "stage_contract.v1",
        "project_id": "delivery-fixture",
        "repository_root": str(root),
        "stage_id": STAGE_ID,
        "stage_name": "Execution Integration Delivery V1",
        "project_goal": "deliver one reviewed execution artifact",
        "stage_goal": "integrate and verify one reviewed artifact",
        "user_visible_goal": "deliver the reviewed result",
        "inputs": [".research/reviewed-result.json"],
        "protected_paths": [".git", "src", ".research/stage-state.json"],
        "allowed_paths": [".research"],
        "acceptance_description": "delivery verification passes",
        "required_checks": ["unit"],
        "review_artifact_requirements": ["review"],
        "baseline": {
            "requirement_digest": REQUIREMENT_DIGEST,
            "design_digest": DESIGN_DIGEST,
        },
        "status": "PLANNED",
        "max_iterations": 1,
        "retry_budget": 3,
        "plan_id": PLAN_ID,
    }


def _stage_definition(*, target: str = TARGET_RELATIVE, receipt: str = RECEIPT_RELATIVE) -> dict:
    return {
        "schema_version": "execution_integration_delivery.v1",
        "stage_id": STAGE_ID,
        "stage_name": "Execution Integration Delivery V1",
        "requirement_digest": REQUIREMENT_DIGEST,
        "design_digest": DESIGN_DIGEST,
        "authoritative_integration_target": target,
        "delivery_artifact_path": receipt,
        "allowed_paths": [".research"],
        "protected_paths": [".git", "src", ".research/stage-state.json"],
        "verification": {"command": [sys.executable, "-c", "import sys; sys.exit(0)"]},
    }


def _review_receipt(*, decision: str = "STAGE_READY") -> dict:
    return {
        "schema_version": "gpt_technical_review_receipt.v1",
        "status": "complete",
        "stage_id": STAGE_ID,
        "consultation_id": "CONSULT-DELIVERY-001",
        "workflow_decision": decision,
        "requirement_digest": REQUIREMENT_DIGEST,
        "design_digest": DESIGN_DIGEST,
    }


def _ready_controller(tmp_path: Path) -> tuple[StageController, dict, Path]:
    root = tmp_path.resolve()
    (root / ".research").mkdir(parents=True)
    state_path = root / ".research" / "stage-state.json"
    controller = StageController(_contract(root), state_path=state_path)
    controller.start_stage()
    request = controller.request_executor()["request"]
    result = {
        "stage_id": STAGE_ID,
        "iteration_index": request["iteration_index"],
        "status": "SUCCEEDED",
        "summary": "reviewed execution result",
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "executable": "fixture-codex",
        "plan_id": PLAN_ID,
        "requirement_digest": REQUIREMENT_DIGEST,
        "design_digest": DESIGN_DIGEST,
        "gpt_review_consultation_id": "CONSULT-DELIVERY-001",
        "baseline_digest": controller.show_stage(STAGE_ID)["baseline_digest"],
        "required_checks": {"unit": "PASS"},
        "review_artifacts": [{"type": "review", "uri": "review/receipt.json"}],
        "stage_ready": True,
    }
    controller.record_executor_result(result, request_id=request["request_id"])
    source = root / ".research" / "reviewed-result.json"
    source.write_bytes(b"reviewed execution artifact\n")
    return controller, result, source


def _sources(controller: StageController, *, target: str = TARGET_RELATIVE, receipt: str = RECEIPT_RELATIVE):
    definition = _stage_definition(target=target, receipt=receipt)
    artifact = {
        "stage_id": STAGE_ID,
        "authoritative_target": target,
        "delivery_artifact_path": receipt,
        "allowed_paths": [".research"],
        "protected_paths": [".git", "src", ".research/stage-state.json"],
        "verification": definition["verification"],
    }
    return definition, artifact


def _source_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_happy_path_integrates_verifies_and_closes_through_controller(tmp_path: Path) -> None:
    controller, result, source = _ready_controller(tmp_path)
    definition, artifact = _sources(controller)

    delivery = integrate_delivery(
        controller,
        stage_definition=definition,
        stage_contract=controller.show_stage(STAGE_ID)["contract"],
        integration_artifact=artifact,
        reviewed_result=result,
        review_receipt=_review_receipt(),
        source_artifact_path=source,
        source_artifact_digest=_source_digest(source),
        controller_state=controller.state,
        verification_runner=lambda target: {
            "status": "PASS",
            "passed": True,
            "command": target.verification["command"],
            "returncode": 0,
        },
    )

    assert delivery.status == "CLOSED"
    assert controller.show_stage(STAGE_ID)["status"] == "APPROVED"
    assert delivery.receipt_path.read_bytes() == json.dumps(
        delivery.receipt, ensure_ascii=False, sort_keys=True, indent=2
    ).encode("utf-8") + b"\n"
    assert (tmp_path / TARGET_RELATIVE).read_bytes() == source.read_bytes()
    assert delivery.receipt["requirement_digest"] == REQUIREMENT_DIGEST
    assert delivery.receipt["design_digest"] == DESIGN_DIGEST
    assert delivery.receipt["gpt_review_consultation_id"] == "CONSULT-DELIVERY-001"
    assert delivery.receipt["target_after_digest"] == _source_digest(source)
    assert delivery.receipt["verification_result"]["status"] == "PASS"
    assert delivery.receipt["reviewed_execution_result_identity"]["attempt_index"] == 1
    assert delivery.receipt["git_commit_diff_provenance"]["after"]["phase"] == "after"


def test_resume_after_close_reuses_receipt_without_second_delivery(tmp_path: Path) -> None:
    controller, result, source = _ready_controller(tmp_path)
    definition, artifact = _sources(controller)
    kwargs = dict(
        stage_definition=definition,
        stage_contract=controller.show_stage(STAGE_ID)["contract"],
        integration_artifact=artifact,
        reviewed_result=result,
        review_receipt=_review_receipt(),
        source_artifact_path=source,
        source_artifact_digest=_source_digest(source),
        controller_state=controller.state,
        verification_runner=lambda _target: {"status": "PASS", "passed": True},
    )
    first = integrate_delivery(controller, **kwargs)
    receipt_bytes = first.receipt_path.read_bytes()
    resumed = integrate_delivery(controller, **kwargs)

    assert resumed.reused is True
    assert resumed.status == "CLOSED"
    assert resumed.receipt_path == first.receipt_path
    assert resumed.receipt_path.read_bytes() == receipt_bytes
    assert controller.show_stage(STAGE_ID)["status"] == "APPROVED"


def test_human_gate_and_stale_result_fail_closed_before_target_write(tmp_path: Path) -> None:
    controller, result, source = _ready_controller(tmp_path)
    definition, artifact = _sources(controller)
    common = dict(
        stage_definition=definition,
        stage_contract=controller.show_stage(STAGE_ID)["contract"],
        integration_artifact=artifact,
        source_artifact_path=source,
        source_artifact_digest=_source_digest(source),
        controller_state=controller.state,
    )
    with pytest.raises(DeliveryIntegrationError) as human_gate:
        integrate_delivery(
            controller,
            reviewed_result=result,
            review_receipt=_review_receipt(decision="HUMAN_GATE"),
            **common,
        )
    assert human_gate.value.code == "HUMAN_GATE_BLOCKS_PROMOTION"
    assert controller.show_stage(STAGE_ID)["status"] == "STAGE_READY"
    assert not (tmp_path / TARGET_RELATIVE).exists()

    stale = copy.deepcopy(result)
    stale["summary"] = "stale result"
    with pytest.raises(DeliveryIntegrationError) as mismatch:
        integrate_delivery(
            controller,
            reviewed_result=stale,
            review_receipt=_review_receipt(),
            **common,
        )
    assert mismatch.value.code == "RESULT_BINDING_MISMATCH"
    assert controller.show_stage(STAGE_ID)["status"] == "STAGE_READY"
    assert not (tmp_path / TARGET_RELATIVE).exists()


def test_verification_failure_rolls_back_target_and_keeps_stage_ready(tmp_path: Path) -> None:
    controller, result, source = _ready_controller(tmp_path)
    definition, artifact = _sources(controller)
    target = tmp_path / TARGET_RELATIVE
    target.parent.mkdir(parents=True)
    target.write_bytes(b"baseline")
    before = target.read_bytes()

    with pytest.raises(DeliveryIntegrationError) as failure:
        integrate_delivery(
            controller,
            stage_definition=definition,
            stage_contract=controller.show_stage(STAGE_ID)["contract"],
            integration_artifact=artifact,
            reviewed_result=result,
            review_receipt=_review_receipt(),
            source_artifact_path=source,
            source_artifact_digest=_source_digest(source),
            controller_state=controller.state,
            verification_runner=lambda _target: {"status": "FAIL", "passed": False},
        )

    assert failure.value.code == "VERIFICATION_FAILED"
    assert target.read_bytes() == before
    assert controller.show_stage(STAGE_ID)["status"] == "STAGE_READY"
    assert not (tmp_path / RECEIPT_RELATIVE).exists()


def test_target_resolution_is_unique_and_protected_paths_fail_closed(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    base = _stage_definition()
    contract = _contract(root)
    state = {"contract": contract}
    integration = {
        "authoritative_target": ".research/integration/other.bin",
        "delivery_artifact_path": RECEIPT_RELATIVE,
        "verification": base["verification"],
    }
    with pytest.raises(DeliveryIntegrationError) as ambiguous:
        resolve_authoritative_target(
            repository_root=root,
            stage_definition=base,
            stage_contract=contract,
            controller_state=state,
            integration_artifact=integration,
        )
    assert ambiguous.value.code == "TARGET_AMBIGUOUS"

    protected = _stage_definition(target=".research/stage-state.json")
    protected_artifact = {
        "authoritative_target": ".research/stage-state.json",
        "delivery_artifact_path": RECEIPT_RELATIVE,
        "verification": base["verification"],
    }
    with pytest.raises(DeliveryIntegrationError) as blocked:
        resolve_authoritative_target(
            repository_root=root,
            stage_definition=protected,
            stage_contract=contract,
            controller_state=state,
            integration_artifact=protected_artifact,
        )
    assert blocked.value.code == "TARGET_PROTECTED"


def test_mismatched_source_digest_is_rejected_without_mutation(tmp_path: Path) -> None:
    controller, result, source = _ready_controller(tmp_path)
    definition, artifact = _sources(controller)
    with pytest.raises(DeliveryIntegrationError) as mismatch:
        integrate_delivery(
            controller,
            stage_definition=definition,
            stage_contract=controller.show_stage(STAGE_ID)["contract"],
            integration_artifact=artifact,
            reviewed_result=result,
            review_receipt=_review_receipt(),
            source_artifact_path=source,
            source_artifact_digest="stale-source-digest",
            controller_state=controller.state,
        )
    assert mismatch.value.code == "SOURCE_ARTIFACT_MISMATCH"
    assert controller.show_stage(STAGE_ID)["status"] == "STAGE_READY"
    assert not (tmp_path / TARGET_RELATIVE).exists()


def test_planning_to_delivery_close_and_reload_e2e(tmp_path: Path) -> None:
    """Exercise the complete new Stage path in one isolated workspace."""

    root = tmp_path.resolve()
    (root / ".research" / "stages" / "implementation").mkdir(parents=True)
    target = root / TARGET_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"before-delivery\n")
    source_artifact = root / ".research" / "reviewed-artifact.bin"
    source_artifact.write_bytes(b"reviewed-delivery\n")
    source_contract = _contract(root)
    source_contract.update(
        {
            "stage_id": "implementation",
            "stage_name": "Codex Execution Evidence Gate V1",
                "plan_id": "implementation-plan-v1",
                "integration_target": TARGET_RELATIVE,
                "allowed_paths": [".research"],
                "review_artifact_requirements": ["summary"],
                "baseline": {
                "brief_digest": "1" * 64,
                "design_digest": "2" * 64,
                "design_package_digest": "3" * 64,
            },
        }
    )
    state_path = root / ".research" / "stage-state.json"
    controller = StageController(source_contract, state_path=state_path)
    controller.start_stage("implementation")
    controller.mark_stage_ready(
        {
            "status": "SUCCEEDED",
            "stage_id": "implementation",
            "baseline_digest": controller.show_stage("implementation")["baseline_digest"],
            "required_checks": {"unit": "PASS"},
            "review_artifacts": [{"type": "summary", "uri": "summary.txt"}],
            "stage_ready": True,
            "integration_target": TARGET_RELATIVE,
        },
        stage_id="implementation",
        required_checks={"unit": "PASS"},
        review_artifacts=[{"type": "summary", "uri": "summary.txt"}],
    )
    controller.approve_stage("implementation", actor="fixture", rationale="source closeout")
    (root / ".research" / "stages" / "implementation" / "contract.json").write_text(
        json.dumps(controller.show_stage("implementation")["contract"], sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    plan = plan_execution_integration_stage(root)
    assert plan["integration_target"] == TARGET_RELATIVE
    assert plan["delivery_artifact_path"].startswith(".research/integration/")
    started = start_execution_integration_stage(root)
    assert started["stage"]["status"] == "ACTIVE"

    controller = StageController.from_state(state_path)
    delivery_stage = controller.show_stage(EXECUTION_INTEGRATION_STAGE_ID)
    request = controller.request_executor(EXECUTION_INTEGRATION_STAGE_ID)["request"]
    delivery_result = {
        "stage_id": EXECUTION_INTEGRATION_STAGE_ID,
        "iteration_index": request["iteration_index"],
        "status": "SUCCEEDED",
        "summary": "delivery result is ready for integration",
        "provider": "fixture-provider",
        "model": "gpt-5.6-luna",
        "executable": "fixture-codex",
        "plan_id": delivery_stage["contract"]["plan_id"],
        "requirement_digest": "1" * 64,
        "design_digest": "2" * 64,
        "gpt_review_consultation_id": "CONSULT-DELIVERY-E2E",
        "baseline_digest": delivery_stage["baseline_digest"],
        "required_checks": {
            "integrated_result_identity": "PASS",
            "post_integration_verification": "PASS",
        },
        "review_artifacts": [
            {"type": "delivery_receipt", "uri": plan["delivery_artifact_path"]},
            {"type": "post_integration_verification", "uri": "verification"},
        ],
        "stage_ready": True,
    }
    controller.record_executor_result(delivery_result, request_id=request["request_id"])
    controller_state = controller.state
    delivery_contract = controller.show_stage(EXECUTION_INTEGRATION_STAGE_ID)["contract"]
    integration_artifact = {
        "stage_id": EXECUTION_INTEGRATION_STAGE_ID,
        "target_path": plan["integration_target"],
        "delivery_artifact_path": plan["delivery_artifact_path"],
        "allowed_paths": delivery_contract["allowed_paths"],
        "protected_paths": delivery_contract["protected_paths"],
        "verification": {"command": [sys.executable, "-c", "import sys; sys.exit(0)"]},
    }
    review = {
        "status": "complete",
        "stage_id": EXECUTION_INTEGRATION_STAGE_ID,
        "consultation_id": "CONSULT-DELIVERY-E2E",
        "workflow_decision": "STAGE_READY",
        "requirement_digest": "1" * 64,
        "design_digest": "2" * 64,
    }
    delivery = integrate_delivery(
        controller,
        stage_definition=plan,
        stage_contract=delivery_contract,
        integration_artifact=integration_artifact,
        reviewed_result=delivery_result,
        review_receipt=review,
        source_artifact_path=source_artifact,
        source_artifact_digest=_source_digest(source_artifact),
        controller_state=controller_state,
    )
    assert delivery.status == "CLOSED"
    reloaded = StageController.from_state(state_path)
    assert reloaded.show_stage(EXECUTION_INTEGRATION_STAGE_ID)["status"] == "APPROVED"
    assert (root / plan["integration_target"]).read_bytes() == source_artifact.read_bytes()
    assert delivery.receipt["target_after_digest"] == _source_digest(source_artifact)
