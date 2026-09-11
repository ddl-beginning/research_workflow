"""Focused coverage for the canonical same-iteration engineering retry.

The worker used here is deliberately a local Python stub.  These tests exercise
the controller's request/receipt boundary and never start a model or a bridge.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.contracts import ContractValidationError, sha256_json
from src.stage_controller import StageController, StageControllerError
from src.state_continuation import derive_next
from tests.test_stage_retry_semantics import _contract, _result


STAGE_ID = "stage-retry"


def _active_controller(
    *,
    max_iterations: int = 2,
    retry_budget: int = 3,
    state_path: Path | None = None,
) -> StageController:
    controller = StageController(
        _contract(max_iterations=max_iterations, retry_budget=retry_budget),
        state_path=state_path,
    )
    controller.start_stage()
    return controller


def _bare_failed(
    controller: StageController,
    *,
    summary: str = "The bounded engineering worker reported a failed result.",
) -> tuple[dict, dict, dict]:
    request = controller.request_executor()["request"]
    result = _result(request, status="FAILED", summary=summary)
    outcome = controller.record_executor_result(result, request_id=request["request_id"])
    return request, result, outcome


def _retry(controller: StageController, previous_request: dict, *, objective: str | None = None) -> dict:
    kwargs: dict[str, object] = {
        "previous_request_id": previous_request["request_id"],
        "rationale": "Retry the unchanged engineering objective after the bounded worker failure.",
    }
    if objective is not None:
        kwargs["objective"] = objective
    return controller.request_engineering_retry(**kwargs)


def test_a_explicit_retry_reopens_closed_iteration_without_rewriting_bare_replan() -> None:
    controller = _active_controller()
    first_request, failed_result, failed_outcome = _bare_failed(controller)

    assert failed_outcome["decision"] == "REPLAN"
    assert controller.state["iteration_index"] == 1
    assert controller.state["open_iteration_index"] is None
    old_attempt = copy.deepcopy(controller.state["execution_attempts"][0])
    old_events = copy.deepcopy(controller.state["events"])

    retry = _retry(controller, first_request)
    request = retry["request"]

    assert retry["idempotent_reuse"] is False
    assert retry["dispatch_required"] is True
    assert request["request_id"] != first_request["request_id"]
    assert request["iteration_index"] == first_request["iteration_index"] == 1
    assert request["attempt_index"] == 2
    assert retry["stage"]["status"] == "ACTIVE"
    assert retry["stage"]["open_iteration_index"] == 1
    assert retry["stage"]["latest_result"] == failed_result
    assert retry["stage"]["execution_attempts"][0] == old_attempt
    assert retry["stage"]["events"][: len(old_events)] == old_events


def test_b_error_failed_succeeded_are_three_attempts_in_iteration_one() -> None:
    controller = _active_controller(retry_budget=4)

    first = controller.request_executor()["request"]
    error = _result(
        first,
        status="ERROR",
        execution_failure={
            "kind": "PROVIDER_FAILURE",
            "code": "LOCAL_STUB_TIMEOUT",
            "retryable": True,
        },
    )
    error_outcome = controller.record_executor_result(error, request_id=first["request_id"])
    assert error_outcome["decision"] == "EXECUTION_PROVIDER_FAILURE"

    second = _retry(controller, first)["request"]
    failed = _result(second, status="FAILED", summary="The first local assertion was false.")
    failed_outcome = controller.record_executor_result(failed, request_id=second["request_id"])
    assert failed_outcome["decision"] == "REPLAN"

    retry = _retry(controller, second)
    third = retry["request"]
    succeeded = _result(third, status="SUCCEEDED", evidence_refs=["artifact://retry-success"])
    success_outcome = controller.record_executor_result(succeeded, request_id=third["request_id"])

    assert success_outcome["decision"] == "CONTINUE"
    assert [first["iteration_index"], second["iteration_index"], third["iteration_index"]] == [1, 1, 1]
    assert [first["attempt_index"], second["attempt_index"], third["attempt_index"]] == [1, 2, 3]
    stage = success_outcome["stage"]
    assert stage["iteration_index"] == 1
    assert stage["open_iteration_index"] is None
    assert stage["attempt_count"] == 3
    assert [attempt["status"] for attempt in stage["execution_attempts"]] == [
        "ERROR",
        "FAILED",
        "SUCCEEDED",
    ]


def test_c_success_binds_latest_result_stage_result_and_digest_to_retry_request() -> None:
    controller = _active_controller(retry_budget=3)
    first_request, _, _ = _bare_failed(controller)
    retry = _retry(controller, first_request)
    request = retry["request"]
    result = _result(
        request,
        status="SUCCEEDED",
        summary="The local stub succeeded on the engineering retry.",
        evidence_refs=["artifacts/retry.txt"],
    )

    outcome = controller.record_executor_result(result, request_id=request["request_id"])
    stage = outcome["stage"]
    digest = sha256_json(result)
    binding = stage["stage_result_binding"]
    latest_stage_result = stage["latest_stage_result"]

    assert stage["latest_result"] == result
    assert stage["execution_evidence_complete"] is True
    assert binding["result_digest"] == digest
    assert binding["request_id"] == request["request_id"]
    assert binding["iteration_index"] == 1
    assert latest_stage_result["result"] == result
    assert latest_stage_result["result_digest"] == digest
    assert latest_stage_result["request_id"] == request["request_id"]
    assert stage["execution_attempts"][-1]["result_digest"] == digest


def test_d_max_iterations_one_allows_retry_but_rejects_semantic_iteration_two() -> None:
    controller = _active_controller(max_iterations=1, retry_budget=3)
    first_request, _, _ = _bare_failed(controller)
    retry = _retry(controller, first_request)
    success = _result(retry["request"], status="SUCCEEDED")
    controller.record_executor_result(success, request_id=retry["request"]["request_id"])

    before = controller.snapshot()
    with pytest.raises(StageControllerError, match="max_iterations"):
        controller.record_technical_review(
            {
                "decision": "REPLAN",
                "requires_next_iteration": True,
                "review_id": "semantic-review-max-one",
                "rationale": "A second semantic iteration would exceed the fixture budget.",
            }
        )
    assert controller.snapshot() == before

    controller_two = _active_controller(max_iterations=2)
    request_two, _, _ = _bare_failed(controller_two)
    before_invalid_review = controller_two.snapshot()
    with pytest.raises(ContractValidationError, match="requires_next_iteration"):
        controller_two.record_technical_review(
            {"decision": "REPLAN", "requires_next_iteration": False}
        )
    assert controller_two.snapshot() == before_invalid_review
    review = controller_two.record_technical_review(
        {
            "decision": "REPLAN",
            "requires_next_iteration": True,
            "review_id": "semantic-review-two",
            "rationale": "This is an explicit semantic change.",
        }
    )
    assert review["stage"]["iteration_index"] == 2
    with pytest.raises((StageControllerError, ContractValidationError)):
        controller_two.request_engineering_retry(
            previous_request_id=request_two["request_id"],
            rationale="Attempt to bypass the explicit semantic iteration.",
        )


def test_e_semantic_replan_blocks_retry_and_direct_executor_bypass() -> None:
    controller = _active_controller()
    first_request, _, _ = _bare_failed(controller)
    decision = controller.apply_decision("REPLAN", rationale="semantic change requires a new objective")
    assert decision["decision"] == "REPLAN"
    before = controller.snapshot()

    with pytest.raises((StageControllerError, ContractValidationError)):
        controller.request_engineering_retry(
            previous_request_id=first_request["request_id"],
            rationale="Try to treat the semantic replan as an engineering retry.",
        )
    assert controller.snapshot() == before

    with pytest.raises((StageControllerError, ContractValidationError)):
        controller.request_executor()
    assert controller.snapshot() == before


def test_f_human_gate_changed_objective_inflight_and_non_retryable_guards() -> None:
    gated = _active_controller()
    gated_request, _, _ = _bare_failed(gated)
    gated.apply_decision("HUMAN_GATE", rationale="human must decide whether to continue")
    gated_before = gated.snapshot()
    with pytest.raises((StageControllerError, ContractValidationError)):
        gated.request_engineering_retry(
            previous_request_id=gated_request["request_id"],
            rationale="Try to bypass the pending Human Gate.",
        )
    assert gated.snapshot() == gated_before

    changed = _active_controller()
    changed_request, _, _ = _bare_failed(changed)
    changed_before = changed.snapshot()
    with pytest.raises((StageControllerError, ContractValidationError)):
        changed.request_engineering_retry(
            previous_request_id=changed_request["request_id"],
            objective="A materially changed requirement/design objective.",
            rationale="The caller must not silently change the objective.",
        )
    assert changed.snapshot() == changed_before

    inflight = _active_controller(retry_budget=3)
    inflight_request, _, _ = _bare_failed(inflight)
    committed = _retry(inflight, inflight_request)
    inflight_before = inflight.snapshot()
    with pytest.raises((StageControllerError, ContractValidationError)):
        inflight.request_executor()
    assert inflight.snapshot() == inflight_before
    # A duplicate of the already committed transition is the one permitted
    # inflight operation; changing its objective must not be hidden by reuse.
    reused = _retry(inflight, inflight_request)
    assert reused["idempotent_reuse"] is True
    assert reused["dispatch_required"] is False
    with pytest.raises((StageControllerError, ContractValidationError)):
        _retry(inflight, inflight_request, objective="changed while retry is inflight")
    assert inflight.snapshot() == inflight_before
    assert committed["request"] == reused["request"]

    non_retryable = _active_controller()
    non_retryable_request = non_retryable.request_executor()["request"]
    non_retryable_result = _result(
        non_retryable_request,
        status="FAILED",
        execution_failure={
            "kind": "CONTRACT_FAILURE",
            "code": "FIXED_CONTRACT_DEFECT",
            "retryable": False,
        },
    )
    non_retryable.record_executor_result(
        non_retryable_result,
        request_id=non_retryable_request["request_id"],
    )
    non_retryable_before = non_retryable.snapshot()
    with pytest.raises((StageControllerError, ContractValidationError)):
        non_retryable.request_engineering_retry(
            previous_request_id=non_retryable_request["request_id"],
            rationale="retryable=false must not be bypassed",
        )
    assert non_retryable.snapshot() == non_retryable_before


def test_g_retry_budget_exhaustion_transitions_stage_to_blocked() -> None:
    controller = _active_controller(max_iterations=1, retry_budget=2)
    first_request, _, _ = _bare_failed(controller)
    retry = _retry(controller, first_request)
    second_request = retry["request"]
    second_failed = _result(second_request, status="FAILED", summary="The retry also failed.")
    controller.record_executor_result(second_failed, request_id=second_request["request_id"])

    try:
        exhausted = controller.request_engineering_retry(
            previous_request_id=second_request["request_id"],
            rationale="There is no remaining bounded attempt.",
        )
    except StageControllerError as exc:
        assert "exhaust" in str(exc).lower()
        stage = controller.state
    else:
        stage = exhausted["stage"]
        assert exhausted["dispatch_required"] is False
    assert stage["status"] == "BLOCKED"
    assert controller.state["active_stage_id"] is None
    assert controller.state["iteration_index"] == 1
    assert controller.state["attempt_count"] == 2


def test_h_history_preserves_requests_results_digests_and_event_prefix() -> None:
    controller = _active_controller(retry_budget=4)
    first_request = controller.request_executor()["request"]
    first_result = _result(
        first_request,
        status="ERROR",
        execution_failure={"kind": "RUNNER_FAILURE", "code": "STUB_CRASH", "retryable": True},
    )
    controller.record_executor_result(first_result, request_id=first_request["request_id"])
    second_request = _retry(controller, first_request)["request"]
    second_result = _result(second_request, status="FAILED", summary="The stub assertion failed.")
    controller.record_executor_result(second_result, request_id=second_request["request_id"])

    requests_before_retry = copy.deepcopy(controller.state["executor_requests"])
    attempts_before_retry = copy.deepcopy(controller.state["execution_attempts"])
    events_before_retry = copy.deepcopy(controller.state["events"])
    retry = _retry(controller, second_request)
    third_request = retry["request"]
    third_result = _result(third_request, status="SUCCEEDED", evidence_refs=["artifact://final"])
    controller.record_executor_result(third_result, request_id=third_request["request_id"])
    stage = controller.state

    assert stage["executor_requests"][: len(requests_before_retry)] == requests_before_retry
    assert stage["events"][: len(events_before_retry)] == events_before_retry
    for before_attempt, after_attempt in zip(attempts_before_retry, stage["execution_attempts"]):
        assert after_attempt["request"] == before_attempt["request"]
        assert after_attempt["result"] == before_attempt["result"]
        assert after_attempt["result_digest"] == before_attempt["result_digest"]
        assert after_attempt["result_digest"] == sha256_json(after_attempt["result"])
    assert stage["execution_attempts"][-1]["request_id"] == third_request["request_id"]
    assert stage["execution_attempts"][-1]["result"] == third_result
    assert stage["execution_attempts"][-1]["result_digest"] == sha256_json(third_result)


def test_process_retry_commit_resume_is_idempotent_and_does_not_duplicate_dispatch(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "stage-state.json"
    dispatch_log = tmp_path / "dispatch.jsonl"
    controller = _active_controller(state_path=state_path)
    first_request, _, _ = _bare_failed(controller)
    dispatch_log.write_text(
        json.dumps({"request_id": first_request["request_id"], "attempt_index": 1}) + "\n",
        encoding="utf-8",
    )

    child = r'''
import json
import os
import sys
from pathlib import Path

from src.stage_controller import StageController

state_path = Path(sys.argv[1])
previous_request_id = sys.argv[2]
dispatch_log = Path(sys.argv[3])
controller = StageController.from_state(state_path)
retry = controller.request_engineering_retry(
    previous_request_id=previous_request_id,
    rationale="Commit the same local engineering retry before simulated crash.",
)
assert retry["dispatch_required"] is True
with dispatch_log.open("a", encoding="utf-8") as stream:
    stream.write(json.dumps({"request_id": retry["request"]["request_id"], "attempt_index": retry["request"]["attempt_index"]}) + "\n")
os._exit(73)
'''
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            child,
            str(state_path),
            first_request["request_id"],
            str(dispatch_log),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert completed.returncode == 73, completed.stderr

    recovered = StageController.from_state(state_path)
    before = recovered.snapshot()
    old_event_count = len(before["events"])
    old_request_count = len(before["stages"][STAGE_ID]["executor_requests"])
    reused = recovered.request_engineering_retry(
        previous_request_id=first_request["request_id"],
        rationale="Commit the same local engineering retry before simulated crash.",
    )
    after = recovered.snapshot()

    assert reused["idempotent_reuse"] is True
    assert reused["dispatch_required"] is False
    assert len(after["events"]) == old_event_count
    assert len(after["stages"][STAGE_ID]["executor_requests"]) == old_request_count
    assert after == before
    dispatches = [
        json.loads(line)
        for line in dispatch_log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(dispatches) == 2
    assert len({item["request_id"] for item in dispatches}) == 2
    assert reused["request"]["request_id"] == dispatches[-1]["request_id"]


def test_i_direct_executor_cannot_change_objective_after_error() -> None:
    changed = _active_controller()
    first = changed.request_executor()["request"]
    changed.record_executor_result(
        _result(
            first,
            status="ERROR",
            execution_failure={
                "kind": "RUNNER_FAILURE",
                "code": "STUB_ERROR",
                "retryable": True,
            },
        ),
        request_id=first["request_id"],
    )
    before = changed.snapshot()
    with pytest.raises((StageControllerError, ContractValidationError)):
        changed.request_executor(objective="a changed requirement/design objective")
    assert changed.snapshot() == before


def test_i2_direct_executor_cannot_bypass_non_retryable_error() -> None:
    non_retryable = _active_controller()
    request = non_retryable.request_executor()["request"]
    non_retryable.record_executor_result(
        _result(
            request,
            status="ERROR",
            execution_failure={
                "kind": "PROVIDER_FAILURE",
                "code": "PERMANENT_PROVIDER_REJECTION",
                "retryable": False,
            },
        ),
        request_id=request["request_id"],
    )
    before = non_retryable.snapshot()
    with pytest.raises((StageControllerError, ContractValidationError)):
        non_retryable.request_executor()
    assert non_retryable.snapshot() == before


def test_j_explicit_retry_coordinates_legacy_error_then_failed_retry_on_iteration_one() -> None:
    controller = _active_controller(max_iterations=1, retry_budget=3)
    first = controller.request_executor()["request"]
    error = _result(
        first,
        status="ERROR",
        execution_failure={
            "kind": "PROVIDER_FAILURE",
            "code": "LEGACY_ERROR",
            "retryable": True,
        },
    )
    controller.record_executor_result(error, request_id=first["request_id"])
    assert controller.state["iteration_index"] == 0
    assert controller.state["open_iteration_index"] == 1

    retry_after_error = _retry(controller, first)
    second = retry_after_error["request"]
    assert second["iteration_index"] == 1
    assert second["attempt_index"] == 2
    assert retry_after_error["stage"]["iteration_index"] == 0
    assert retry_after_error["stage"]["open_iteration_index"] == 1

    failed = _result(second, status="FAILED", summary="The legacy retry still failed.")
    controller.record_executor_result(failed, request_id=second["request_id"])
    retry_after_failed = _retry(controller, second)
    third = retry_after_failed["request"]
    assert third["iteration_index"] == 1
    assert third["attempt_index"] == 3
    succeeded = _result(third, status="SUCCEEDED", evidence_refs=["artifact://legacy-recovery"])
    outcome = controller.record_executor_result(succeeded, request_id=third["request_id"])

    assert outcome["stage"]["iteration_index"] == 1
    assert [item["iteration_index"] for item in outcome["stage"]["executor_requests"]] == [1, 1, 1]
    assert [item["status"] for item in outcome["stage"]["execution_attempts"]] == [
        "ERROR",
        "FAILED",
        "SUCCEEDED",
    ]


@pytest.mark.parametrize(
    "marker",
    ["semantic_change_required", "requirement_changed", "design_changed"],
)
def test_k_semantic_change_markers_cannot_be_bypassed_by_bare_failed_retry(marker: str) -> None:
    controller = _active_controller()
    request = controller.request_executor()["request"]
    failed = _result(request, status="FAILED", **{marker: True})
    controller.record_executor_result(failed, request_id=request["request_id"])
    before = controller.snapshot()

    with pytest.raises((StageControllerError, ContractValidationError)):
        controller.request_engineering_retry(
            previous_request_id=request["request_id"],
            rationale="A semantic marker requires the explicit semantic route.",
        )
    assert controller.snapshot() == before
    with pytest.raises((StageControllerError, ContractValidationError)):
        controller.request_executor()
    assert controller.snapshot() == before


def test_l_non_active_unknown_and_success_attempts_cannot_request_engineering_retry() -> None:
    stopped = _active_controller()
    stopped_request, _, _ = _bare_failed(stopped)
    stopped.stop_stage(rationale="the Stage was explicitly stopped")
    before = stopped.snapshot()
    with pytest.raises((StageControllerError, ContractValidationError)):
        stopped.request_engineering_retry(
            previous_request_id=stopped_request["request_id"],
            rationale="A stopped Stage cannot be retried.",
        )
    assert stopped.snapshot() == before

    unknown = _active_controller()
    _bare_failed(unknown)
    before = unknown.snapshot()
    with pytest.raises((StageControllerError, ContractValidationError)):
        unknown.request_engineering_retry(
            previous_request_id="executor-does-not-exist",
            rationale="The request id is not in the immutable request log.",
        )
    assert unknown.snapshot() == before

    succeeded = _active_controller()
    request = succeeded.request_executor()["request"]
    result = _result(request, status="SUCCEEDED")
    succeeded.record_executor_result(result, request_id=request["request_id"])
    before = succeeded.snapshot()
    with pytest.raises((StageControllerError, ContractValidationError)):
        succeeded.request_engineering_retry(
            previous_request_id=request["request_id"],
            rationale="A successful attempt is not an engineering retry source.",
        )
    assert succeeded.snapshot() == before


def test_m_budget_exhaustion_is_persisted_as_blocked_after_disk_reload(tmp_path: Path) -> None:
    state_path = tmp_path / "stage-state.json"
    controller = _active_controller(max_iterations=1, retry_budget=2, state_path=state_path)
    first, _, _ = _bare_failed(controller)
    retry = _retry(controller, first)
    second = retry["request"]
    controller.record_executor_result(
        _result(second, status="FAILED", summary="The bounded budget is now exhausted."),
        request_id=second["request_id"],
    )

    with pytest.raises(StageControllerError, match="exhaust"):
        controller.request_engineering_retry(
            previous_request_id=second["request_id"],
            rationale="Persist the bounded exhaustion decision.",
        )
    reloaded = StageController.from_state(state_path)
    stage = reloaded.show_stage(STAGE_ID)
    assert stage["status"] == "BLOCKED"
    assert reloaded.state["active_stage_id"] is None
    assert stage["iteration_index"] == 1
    assert stage["attempt_count"] == 2


def test_n_derive_next_reloaded_routes_committed_retry_to_execution_recovery_without_new_request(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "stage-state.json"
    controller = _active_controller(state_path=state_path)
    first, _, _ = _bare_failed(controller)
    committed = _retry(controller, first)
    before_bytes = state_path.read_bytes()
    before_snapshot = StageController.from_state(state_path).snapshot()

    reloaded = StageController.from_state(state_path)
    route = derive_next(reloaded)
    after_snapshot = StageController.from_state(state_path).snapshot()

    assert route["position"] == "execution_recovery"
    assert route["capability"] == "stage.execution"
    assert route["request_id"] == committed["request"]["request_id"]
    assert route["expected_revision"] == before_snapshot["revision"]
    assert len(after_snapshot["stages"][STAGE_ID]["executor_requests"]) == 2
    assert after_snapshot == before_snapshot
    assert state_path.read_bytes() == before_bytes


def test_o_consumed_semantic_replan_opens_iteration_two_where_engineering_retry_remains_legal() -> None:
    controller = _active_controller(max_iterations=2, retry_budget=3)
    first = controller.request_executor()["request"]
    first_result = _result(first, status="SUCCEEDED", evidence_refs=["artifact://iteration-one"])
    controller.record_executor_result(first_result, request_id=first["request_id"])
    review = controller.record_technical_review(
        {
            "decision": "REPLAN",
            "requires_next_iteration": True,
            "review_id": "semantic-review-opens-two",
            "rationale": "One explicit semantic change opens the second iteration.",
        }
    )
    assert review["stage"]["iteration_index"] == 2
    assert review["stage"]["open_iteration_index"] == 2

    second = controller.request_executor()["request"]
    assert second["iteration_index"] == 2
    assert second["attempt_index"] == 1
    second_failed = _result(second, status="FAILED", summary="Iteration two's first stub attempt failed.")
    controller.record_executor_result(second_failed, request_id=second["request_id"])

    retry = _retry(controller, second)
    third = retry["request"]
    assert third["iteration_index"] == 2
    assert third["attempt_index"] == 2
    result = _result(third, status="SUCCEEDED", evidence_refs=["artifact://iteration-two"])
    outcome = controller.record_executor_result(result, request_id=third["request_id"])

    stage = outcome["stage"]
    assert stage["iteration_index"] == 2
    assert stage["latest_stage_result"]["request_id"] == third["request_id"]
    assert [request["iteration_index"] for request in stage["executor_requests"]] == [1, 2, 2]
    assert [attempt["status"] for attempt in stage["execution_attempts"]] == [
        "SUCCEEDED",
        "FAILED",
        "SUCCEEDED",
    ]


def test_p_initial_inflight_primary_request_cannot_be_duplicated() -> None:
    controller = _active_controller()
    controller.request_executor()
    before = controller.snapshot()
    with pytest.raises(StageControllerError):
        controller.request_executor()
    assert controller.snapshot() == before


def test_q_legacy_error_retry_exhaustion_blocks_and_reloads(tmp_path: Path) -> None:
    state_path = tmp_path / "stage-state.json"
    controller = _active_controller(retry_budget=2, state_path=state_path)
    request = controller.request_executor()["request"]
    for index in range(2):
        if index:
            request = _retry(controller, request)["request"]
        controller.record_executor_failure(
            {"kind": "RUNNER_FAILURE", "code": "LOCAL_RUNNER_FAILURE", "retryable": True},
            request_id=request["request_id"],
        )
    with pytest.raises(StageControllerError, match="exhaust"):
        _retry(controller, request)
    reloaded = StageController.from_state(state_path)
    assert reloaded.state["status"] == "BLOCKED"
    assert reloaded.state["active_stage_id"] is None
    assert reloaded.state["attempt_count"] == 2


def test_r_omitted_objective_preserves_custom_error_request_objective() -> None:
    controller = _active_controller()
    first = controller.request_executor(objective="A specific unchanged implementation task")["request"]
    controller.record_executor_failure({"kind": "RUNNER_FAILURE", "code": "RETRY"}, request_id=first["request_id"])
    before = controller.snapshot()
    with pytest.raises(StageControllerError):
        controller.request_executor()
    assert controller.snapshot() == before
    second = _retry(controller, first)["request"]
    assert second["objective"] == first["objective"]


@pytest.mark.parametrize("marker", ["semantic_change_required", "requirement_changed", "design_changed", "human_gate_required"])
def test_s_error_markers_block_both_retry_entry_points(marker: str) -> None:
    controller = _active_controller()
    first = controller.request_executor()["request"]
    controller.record_executor_result(_result(first, status="ERROR", **{marker: True}), request_id=first["request_id"])
    before = controller.snapshot()
    with pytest.raises(StageControllerError):
        controller.request_executor()
    with pytest.raises(StageControllerError):
        _retry(controller, first)
    assert controller.snapshot() == before


@pytest.mark.parametrize("maximum", [1, 2])
def test_t_semantic_review_after_error_advances_beyond_used_open_iteration(maximum: int) -> None:
    controller = _active_controller(max_iterations=maximum)
    first = controller.request_executor()["request"]
    controller.record_executor_failure({"kind": "RUNNER_FAILURE", "code": "ERROR_BEFORE_REPLAN"}, request_id=first["request_id"])
    assert controller.state["iteration_index"] == 0
    assert controller.state["open_iteration_index"] == 1
    review = {"decision": "REPLAN", "requires_next_iteration": True, "review_id": "real-semantic-review", "rationale": "A new representation is required"}
    before = controller.snapshot()
    if maximum == 1:
        with pytest.raises(StageControllerError, match="max_iterations"):
            controller.record_technical_review(review)
        assert controller.snapshot() == before
    else:
        outcome = controller.record_technical_review(review)
        assert outcome["stage"]["iteration_index"] == 2
        assert outcome["stage"]["open_iteration_index"] == 2
        assert outcome["event"]["details"]["from_iteration_index"] == 1
        with pytest.raises(StageControllerError):
            _retry(controller, first)
        next_request = controller.request_executor(objective="New reviewed representation")["request"]
        assert next_request["iteration_index"] == 2 and next_request["attempt_index"] == 1
