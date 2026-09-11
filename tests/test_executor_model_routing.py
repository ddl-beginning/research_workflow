from __future__ import annotations

import pytest

from src.contracts import ContractValidationError
from src.executor_model_routing import (
    FRONTIER,
    STANDARD,
    derive_execution_profile,
    derive_routing_decision,
)


def test_default_and_recovery_work_stay_standard_without_heuristics():
    assert derive_execution_profile({}) == (STANDARD, "default_standard")
    assert derive_execution_profile({"task_length": 99999, "keywords": ["architecture"]}) == (
        STANDARD,
        "default_standard",
    )
    assert derive_execution_profile({"standard_execution_failures": 2}) == (
        STANDARD,
        "default_standard",
    )


def test_authoritative_frontier_request_binds_only_the_frontier_profile():
    decision = derive_routing_decision(
        {
            "execution_profile": FRONTIER,
            "execution_profile_authority": "authoritative_executor_request",
            "executor_request_id": "executor-42",
        }
    )
    assert decision.bounded_view() == {
        "execution_profile": FRONTIER,
        "actual_model": "gpt-6-astra",
        "reasoning_effort": "low",
        "auth_mode": "chatgpt",
        "profile_derivation_reason": "authoritative_executor_request",
        "executor_request_id": "executor-42",
    }


def test_frontier_requires_authority_and_same_objective_review_for_recovery_escalation():
    with pytest.raises(ContractValidationError, match="authoritative"):
        derive_execution_profile({"execution_profile": FRONTIER})

    assert derive_execution_profile(
        {
            "standard_execution_failures": 2,
            "technical_review": {
                "authoritative": True,
                "same_semantic_objective": True,
                "unresolved": True,
            },
        }
    ) == (
        FRONTIER,
        "same_semantic_objective_two_standard_failures_authoritative_review",
    )


def test_routing_provenance_is_request_scoped_and_chatgpt_only():
    standard = derive_routing_decision({}, executor_request_id="executor-standard")
    assert standard.execution_profile == STANDARD
    assert standard.model == "gpt-5.6-luna"
    assert standard.reasoning_effort == "max"
    assert standard.auth_mode == "chatgpt"
    with pytest.raises(ContractValidationError, match="ChatGPT auth"):
        derive_routing_decision({}, executor_request_id="executor-bad", auth_mode="api_key")
