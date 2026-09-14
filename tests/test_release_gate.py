from __future__ import annotations

from src.release_gate import evaluate_gpt_review_pipeline


def _gate(decision: str, *, response_received: bool = True) -> dict:
    return evaluate_gpt_review_pipeline(
        transport_status="PASS",
        response_received=response_received,
        decision=decision,
        decision_parsed=response_received,
        decision_bound=True,
    )


def test_real_gpt_blocked_decision_does_not_fail_engine_release() -> None:
    result = _gate("BLOCKED")
    assert result["status"] == "PASS"
    assert result["engine_release"] is True
    assert result["business_decision"] == "BLOCKED"


def test_real_gpt_replan_decision_does_not_fail_engine_release() -> None:
    result = _gate("REPLAN")
    assert result["status"] == "PASS"
    assert result["engine_release"] is True
    assert result["business_decision"] == "REPLAN"


def test_real_gpt_stage_ready_decision_passes_engine_release() -> None:
    result = _gate("STAGE_READY")
    assert result["status"] == "PASS"
    assert result["engine_release"] is True


def test_missing_gpt_response_does_fail_engine_release() -> None:
    result = _gate("BLOCKED", response_received=False)
    assert result["status"] == "FAIL"
    assert result["engine_release"] is False
    assert result["checks"]["response"] is False

