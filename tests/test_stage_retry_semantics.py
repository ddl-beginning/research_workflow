from __future__ import annotations

import copy

import pytest

from src.contracts import ContractValidationError, validate_against_schema
from src.stage_controller import (
    RETRY_SEMANTICS_VERSION,
    StageController,
    StageControllerError,
)


def _contract(*, retry_budget: int = 3, max_iterations: int = 2) -> dict:
    return {
        "schema_version": "stage_contract.v1",
        "project_id": "retry-semantics-fixture",
        "repository_root": "D:/work/retry-semantics-fixture",
        "stage_id": "stage-retry",
        "stage_name": "retry semantics fixture",
        "project_goal": "preserve execution provenance",
        "stage_goal": "produce one valid bounded execution result",
        "user_visible_goal": "show the valid execution result",
        "inputs": ["fixtures/input.txt"],
        "protected_paths": [".git", "production"],
        "allowed_paths": [".research", "tests"],
        "acceptance_description": "all checks pass",
        "required_checks": ["unit"],
        "review_artifact_requirements": ["summary"],
        "baseline": {"fixture": "retry"},
        "status": "PLANNED",
        "max_iterations": max_iterations,
        "retry_budget": retry_budget,
    }


def _result(request: dict, *, status: str = "SUCCEEDED", **fields: object) -> dict:
    value: dict[str, object] = {
        "stage_id": request["stage_id"],
        "iteration_index": request["iteration_index"],
        "status": status,
        "summary": "bounded fixture result",
        "provider": "fixture-provider",
        "model": "gpt-5.6-luna",
        "executable": "fixture-executable",
    }
    value.update(fields)
    return value


def test_retry_attempts_share_stage_iteration_and_keep_provenance() -> None:
    controller = StageController(_contract())
    controller.start_stage()

    first = controller.request_executor()["request"]
    first_outcome = controller.record_executor_result(
        _result(
            first,
            status="ERROR",
            execution_failure={
                "kind": "PROVIDER_FAILURE",
                "code": "PROVIDER_TIMEOUT",
                "retryable": True,
            },
        ),
        request_id=first["request_id"],
    )
    assert first_outcome["decision"] == "EXECUTION_PROVIDER_FAILURE"

    second = controller.request_engineering_retry(previous_request_id=first["request_id"], rationale="Retry provider failure")["request"]
    second_outcome = controller.record_executor_failure(
        {
            "kind": "ADAPTER_FAILURE",
            "code": "RUNNER_UNAVAILABLE",
            "retryable": True,
        },
        request_id=second["request_id"],
        provider={"adapter": "fixture-adapter", "runner": "fixture-runner"},
    )
    assert second_outcome["decision"] == "EXECUTION_ATTEMPT_FAILURE"

    third = controller.request_engineering_retry(previous_request_id=second["request_id"], rationale="Retry adapter failure")["request"]
    success = controller.record_executor_result(
        _result(third, evidence_refs=["artifact://bounded-result"]),
        request_id=third["request_id"],
    )

    assert [item["iteration_index"] for item in (first, second, third)] == [1, 1, 1]
    assert [item["attempt_index"] for item in (first, second, third)] == [1, 2, 3]
    assert len({item["request_id"] for item in (first, second, third)}) == 3
    assert success["stage"]["iteration_index"] == 1
    assert success["stage"]["attempt_count"] == 3
    assert success["stage"]["execution_evidence_complete"] is True
    attempts = success["stage"]["execution_attempts"]
    assert [item["status"] for item in attempts] == ["ERROR", "ERROR", "SUCCEEDED"]
    assert attempts[0]["execution_failure"]["code"] == "PROVIDER_TIMEOUT"
    assert attempts[1]["provenance"]["adapter"] == "fixture-adapter"
    assert attempts[2]["provenance"]["model"] == "gpt-5.6-luna"
    assert success["stage"]["latest_stage_result"]["attempt_index"] == 3


def test_retry_budget_is_bounded_without_advancing_stage_iteration() -> None:
    controller = StageController(_contract(retry_budget=2))
    controller.start_stage()
    request = controller.request_executor()["request"]
    for index in range(2):
        if index:
            request = controller.request_engineering_retry(previous_request_id=request["request_id"], rationale="Retry runner failure")["request"]
        controller.record_executor_failure(
            {"kind": "RUNNER_FAILURE", "code": "RUNNER_TIMEOUT"},
            request_id=request["request_id"],
        )

    with pytest.raises(StageControllerError, match="retry budget exhausted"):
        controller.request_engineering_retry(previous_request_id=request["request_id"], rationale="Budget exhausted")
    assert controller.state["iteration_index"] == 0
    assert controller.state["attempt_count"] == 2
    assert controller.state["retry_count"] == 1
    assert controller.state["retry_budget"] == 2


def test_explicit_contract_failure_does_not_advance_even_with_success_status() -> None:
    controller = StageController(_contract())
    controller.start_stage()
    request = controller.request_executor()["request"]
    outcome = controller.record_executor_result(
        _result(request, contract_satisfied=False),
        request_id=request["request_id"],
    )
    assert outcome["decision"] == "EXECUTION_ATTEMPT_FAILURE"
    assert outcome["execution_failure"]["code"] == "EXECUTION_CONTRACT_UNSATISFIED"
    assert outcome["stage"]["iteration_index"] == 0
    assert outcome["stage"]["execution_attempts"][0]["status"] == "SUCCEEDED"


def test_only_explicit_technical_review_advances_a_new_iteration() -> None:
    controller = StageController(_contract(max_iterations=3))
    controller.start_stage()
    request = controller.request_executor()["request"]
    controller.record_executor_result(_result(request), request_id=request["request_id"])
    assert controller.state["iteration_index"] == 1

    with pytest.raises(ContractValidationError):
        controller.record_technical_review({"decision": "CONTINUE"})
    review = controller.record_technical_review(
        {
            "decision": "TECHNICAL_MODIFICATION_REQUIRED",
            "requires_next_iteration": True,
            "review_id": "gpt-review-1",
            "rationale": "the technical review requests one bounded modification",
        }
    )
    assert review["stage"]["iteration_index"] == 2
    assert review["stage"]["attempt_count"] == 1
    next_request = controller.request_executor()["request"]
    assert next_request["iteration_index"] == 2
    assert next_request["attempt_index"] == 1


def test_reconcile_preserves_legacy_history_then_binds_success_without_rewriting_result() -> None:
    controller = StageController(_contract())
    controller.start_stage()
    failed_request = controller.request_executor()["request"]
    controller.record_executor_failure(
        {"kind": "PROVIDER_FAILURE", "code": "MODEL_UNAVAILABLE"},
        request_id=failed_request["request_id"],
    )
    successful_request = controller.request_engineering_retry(previous_request_id=failed_request["request_id"], rationale="Retry provider failure")["request"]
    completed = _result(successful_request, task_id="legacy-task", iteration_index=2)
    completed["adapter_iteration_binding"] = {
        "adapter": "legacy-rebind",
        "provider_request_id": successful_request["request_id"],
        "provider_request_iteration_index": 1,
        "controller_result_iteration_index": 2,
    }
    controller.record_executor_result(
        _result(successful_request), request_id=successful_request["request_id"]
    )

    legacy = controller.snapshot()
    stage = legacy["stages"]["stage-retry"]
    # Simulate the old adapter's artificial rebind while retaining the actual
    # request/event bodies exactly as persisted.
    stage["iteration_index"] = 2
    stage["latest_result"] = copy.deepcopy(completed)
    stage["latest_stage_result"] = None
    stage["stage_result_binding"] = None
    stage["execution_evidence_complete"] = False
    stage.pop("retry_semantics_version", None)
    stage.pop("execution_attempts", None)
    stage.pop("attempt_count", None)
    stage.pop("retry_count", None)
    stage.pop("retry_budget", None)
    original_requests = copy.deepcopy(stage["executor_requests"])
    original_events = copy.deepcopy(legacy["events"])

    recovered = StageController.from_snapshot(legacy)
    reconciled = recovered.reconcile_retry_semantics(persist=False)
    stage_after = reconciled["stage"]
    assert stage_after["retry_semantics_version"] == RETRY_SEMANTICS_VERSION
    assert stage_after["iteration_index"] == 0
    assert stage_after["attempt_count"] == 2
    assert stage_after["stage_result_binding"]["awaiting_explicit_bind"] is True
    assert stage_after["execution_evidence_complete"] is False
    assert stage_after["executor_requests"] == original_requests
    assert recovered.state["events"] == original_events
    assert stage_after["latest_result"]["iteration_index"] == 2

    bound = recovered.bind_existing_execution_result(
        completed,
        request_id=successful_request["request_id"],
    )
    bound_stage = bound["stage"]
    assert bound_stage["iteration_index"] == 1
    assert bound_stage["execution_evidence_complete"] is True
    assert bound_stage["latest_result"]["iteration_index"] == 2
    assert bound_stage["latest_stage_result"]["iteration_index"] == 1
    assert bound_stage["latest_stage_result"]["legacy_result_iteration_index"] == 2
    assert bound_stage["stage_result_binding"]["request_id"] == successful_request["request_id"]
    assert bound_stage["executor_requests"] == original_requests
    assert bound_stage["execution_attempts"][1]["attempt_index"] == 2

    # Reconciliation is safe to run again after the explicit bind: the raw
    # legacy result index remains 2, but the durable binding proves it belongs
    # to Stage iteration 1 and must not be reopened as a mismatch.
    again = recovered.reconcile_retry_semantics(persist=False)["stage"]
    assert again["iteration_index"] == 1
    assert again["execution_evidence_complete"] is True
    assert again["stage_result_binding"]["evidence_complete"] is True


def test_invalid_result_is_a_persisted_attempt_and_can_only_be_retried_as_new_request() -> None:
    controller = StageController(_contract(retry_budget=2))
    controller.start_stage()
    request = controller.request_executor()["request"]

    with pytest.raises(ContractValidationError, match="iteration_index"):
        controller.record_executor_result(
            _result(request, iteration_index=request["iteration_index"] + 1),
            request_id=request["request_id"],
        )

    stage = controller.state
    assert stage["iteration_index"] == 0
    assert stage["open_iteration_index"] == 1
    assert stage["execution_attempts"][0]["status"] == "INVALID"
    assert stage["execution_attempts"][0]["execution_failure"]["code"] == "ITERATION_BINDING_MISMATCH"
    assert stage["events"][-1]["details"]["stage_iteration_advanced"] is False

    with pytest.raises(StageControllerError, match="already been recorded"):
        controller.record_executor_result(
            _result(request),
            request_id=request["request_id"],
        )
    retry = controller.request_executor()["request"]
    assert retry["iteration_index"] == request["iteration_index"]
    assert retry["attempt_index"] == 2


def test_current_state_schema_requires_explicit_attempt_and_open_iteration_coordinates() -> None:
    controller = StageController(_contract())
    controller.start_stage()
    request = controller.request_executor()["request"]
    controller.record_executor_failure(
        {"kind": "ADAPTER_FAILURE", "code": "ADAPTER_TIMEOUT"},
        request_id=request["request_id"],
    )
    validate_against_schema(controller.snapshot(), "stage_controller_state.v1")
    attempt = controller.state["execution_attempts"][0]
    assert attempt["stage_iteration_index"] == request["iteration_index"]
    assert attempt["attempt_number"] == request["attempt_index"]
