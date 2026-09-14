"""Value-independent release gates for bounded GPT technical reviews."""

from __future__ import annotations

from typing import Any


GPT_REVIEW_DECISIONS = frozenset(
    {"CONTINUE", "REPLAN", "STAGE_READY", "HUMAN_GATE", "BLOCKED"}
)


def evaluate_gpt_review_pipeline(
    *,
    transport_status: str,
    response_received: bool,
    decision: str | None,
    decision_parsed: bool,
    decision_bound: bool,
) -> dict[str, Any]:
    """Separate review transport/binding health from the research decision.

    Every closed GPT decision is a valid Engine review result.  The decision
    value remains business-stage state; only transport, response, parsing, and
    subject binding determine whether the review pipeline is release-ready.
    """

    transport_pass = transport_status == "PASS"
    decision_value = str(decision) if decision is not None else None
    checks = {
        "transport": transport_pass,
        "response": response_received is True,
        "parsed": decision_parsed is True and decision_value in GPT_REVIEW_DECISIONS,
        "bound": decision_bound is True,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "engine_release": all(checks.values()),
        "business_decision": decision_value,
        "checks": checks,
        "allowed_decisions": sorted(GPT_REVIEW_DECISIONS),
    }


__all__ = ["GPT_REVIEW_DECISIONS", "evaluate_gpt_review_pipeline"]
