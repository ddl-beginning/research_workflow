from __future__ import annotations

import pytest

from src.codexpro_adapter import normalize_codexpro_handoff_result
from src.contracts import ContractValidationError


def _evidence(**overrides):
    value = {
        "state": "completed",
        "plan_hash": "plan-hash-1",
        "iteration": 1,
        "exit_code": 0,
        "timed_out": False,
        "changed_files": ["src/adapter.py"],
        "tests": [{"name": "offline adapter test", "status": "PASS"}],
        "status_excerpt": "ignored raw excerpt",
        "diff_excerpt": "ignored raw diff",
    }
    value.update(overrides)
    return value


def _normalize(evidence):
    return normalize_codexpro_handoff_result(
        evidence,
        plan_id="plan-1",
        stage_id="stage-1",
        task_id="task-1",
        iteration_index=1,
        abstraction_layer="adapter",
        baseline_digest="baseline-1",
        expected_plan_hash="plan-hash-1",
        allowed_paths=["src"],
        protected_paths=["src/protected"],
    )


def test_completed_handoff_maps_to_bounded_result_without_stage_promotion():
    result = _normalize(_evidence())

    assert result["status"] == "SUCCEEDED"
    assert result["changed_files"] == ["src/adapter.py"]
    assert result["tests"] == [{"name": "offline adapter test", "status": "PASS"}]
    assert result["stage_ready"] is False
    assert result["human_gate_required"] is False
    assert result["evidence_refs"] == [
        ".ai-bridge/handoff-run-state.json",
        ".ai-bridge/agent-status.md",
        ".ai-bridge/implementation-diff.patch",
        ".ai-bridge/execution-log.jsonl",
    ]
    assert "status_excerpt" not in result
    assert "diff_excerpt" not in result


def test_plan_hash_and_iteration_are_bound_to_the_awaited_task():
    with pytest.raises(ContractValidationError, match="plan_hash"):
        _normalize(_evidence(plan_hash="other"))
    with pytest.raises(ContractValidationError, match="iteration"):
        _normalize(_evidence(iteration=2))


def test_non_terminal_handoff_is_not_accepted_as_execution_evidence():
    with pytest.raises(ContractValidationError, match="terminal"):
        _normalize(_evidence(state="running"))


def test_timeout_and_failure_remain_non_success_results():
    timeout = _normalize(_evidence(state="timed_out", exit_code=None, timed_out=True))
    failed = _normalize(_evidence(state="failed", exit_code=2))

    assert timeout["status"] == "BLOCKED"
    assert failed["status"] == "FAILED"
    assert timeout["user_visible_failure"] is True
    assert failed["user_visible_failure"] is True


def test_changed_files_must_stay_inside_allowed_and_outside_protected_scope():
    with pytest.raises(ContractValidationError, match="allowed_paths/protected_paths"):
        _normalize(_evidence(changed_files=["outside.py"]))
    with pytest.raises(ContractValidationError, match="allowed_paths/protected_paths"):
        _normalize(_evidence(changed_files=["src/protected/file.py"]))


def test_malformed_tests_are_rejected_instead_of_inferred():
    with pytest.raises(ContractValidationError, match=r"tests\[0\].status"):
        _normalize(_evidence(tests=[{"name": "check", "status": "UNKNOWN"}]))
