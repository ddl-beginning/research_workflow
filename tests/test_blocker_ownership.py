import copy

import pytest

from src.workflow_v2_contracts import (
    classify_blocker,
    validate_genuine_blocked_evidence,
)
from tests.test_blocker_maintenance import _blocked_controller, _blocker_record


def _ownership(derived: dict) -> dict:
    return copy.deepcopy(derived["ownership_validation"])


def _blocked_payload(ownership: dict) -> dict:
    return {
        "blocker_still_true": "YES",
        "auto_recovery_exhausted": True,
        "gpt_technical_escalation_completed": True,
        "no_legal_automated_next_action": True,
        "ownership_validation": ownership,
    }


def test_missing_stage_owned_artifact_is_not_genuine_blocker():
    _controller, _attempt, observation, assessment, _decision = _blocked_controller()
    derived = classify_blocker(
        assessment={**assessment, "checks": {"required_artifact": "MISSING"}},
        observation=observation,
    )
    assert derived["ownership_validation"]["stage_owned_work_available"] is True
    assert derived["ownership_validation"]["genuine_blocked_valid"] is False
    assert derived["recommended_action"] == "WORK_REMAINING"


def test_missing_stage_owned_capability_is_not_genuine_blocker():
    _controller, _attempt, observation, assessment, _decision = _blocked_controller()
    derived = classify_blocker(
        assessment={**assessment, "checks": {"capability": "MISSING"}},
        observation=observation,
    )
    assert derived["ownership_validation"]["stage_owned_work_available"] is True
    assert derived["recommended_action"] == "WORK_REMAINING"


def test_missing_generator_is_work_remaining():
    _controller, _attempt, observation, assessment, _decision = _blocked_controller()
    derived = classify_blocker(
        assessment=assessment,
        observation=observation,
        supplemental={"benchmark_inventory": {"missing_required_scenes": ["S4"]}},
    )
    items = derived["ownership_validation"]["items"]
    assert any(item["item"] == "S4 synthetic scene" and item["class"] == "STAGE_OWNED_WORK" for item in items)
    assert derived["recommended_action"] == "WORK_REMAINING"


def test_missing_evaluator_is_work_remaining():
    _controller, _attempt, observation, assessment, _decision = _blocked_controller()
    derived = classify_blocker(
        assessment=assessment,
        observation=observation,
        supplemental={"benchmark_inventory": {"absent_required_capabilities": ["sensor scanline provenance"]}},
    )
    items = derived["ownership_validation"]["items"]
    assert any(item["item"] == "sensor scanline provenance" and item["class"] == "PROVIDER_IMPLEMENTABLE" for item in items)
    assert derived["recommended_action"] == "WORK_REMAINING"


def test_unknown_blocker_ownership_escalates_to_gpt():
    _controller, _attempt, observation, assessment, _decision = _blocked_controller()
    derived = classify_blocker(assessment=assessment, observation=observation)
    assert derived["ownership_validation"]["blocker_ownership_validated"] is False
    assert derived["recommended_action"] == "TECHNICAL_GPT_ESCALATION"


def test_genuine_blocker_requires_ownership_proof():
    payload = _blocked_payload({})
    payload.pop("ownership_validation")
    with pytest.raises(Exception, match="ownership proof is required"):
        validate_genuine_blocked_evidence(payload)


def test_private_human_only_input_can_remain_blocked():
    _controller, _attempt, observation, assessment, _decision = _blocked_controller()
    derived = classify_blocker(assessment=assessment, observation=observation, missing_thing_owner="HUMAN")
    ownership = _ownership(derived)
    assert ownership["human_only_input_found"] is True
    assert ownership["genuine_blocked_valid"] is True
    assert validate_genuine_blocked_evidence(_blocked_payload(ownership))["blocker_still_true"] == "YES"


def test_public_or_synthetic_alternative_prevents_genuine_blocked():
    _controller, _attempt, observation, assessment, _decision = _blocked_controller()
    derived = classify_blocker(
        assessment=assessment,
        observation=observation,
        supplemental={"benchmark_inventory": {"missing_required_scenes": ["S4"]}},
    )
    ownership = _ownership(derived)
    assert ownership["authorized_alternative_available"] is True
    assert ownership["genuine_blocked_valid"] is False
    with pytest.raises(Exception, match="does not prove genuine blocked"):
        validate_genuine_blocked_evidence(_blocked_payload(ownership))


def test_termination_validator_rejects_stage_owned_blocker():
    controller, _attempt, _observation, _assessment, _decision = _blocked_controller()
    validation = controller.validate_termination("stage-maintenance-12345678")
    assert validation["allowed"] is False
    assert validation["reason"] == "UNFINISHED_OBJECTIVE"
    assert validation["legal_next_action"]["action"] == "TECHNICAL_GPT_ESCALATION"


def test_misclassified_genuine_blocker_auto_revalidates_and_continues():
    controller, attempt, observation, assessment, _decision = _blocked_controller()
    result = controller.revalidate_blocker(
        "stage-maintenance-12345678",
        blocker_record=_blocker_record(controller, assessment, observation),
        blocker_still_true="NO",
        released_attempt_id=attempt["attempt_id"],
        reason="MISCLASSIFIED_STAGE_OWNED_WORK",
        command_id="command-maintenance-ownership-revalidate-12345678",
    )
    assert result["stage"]["status"] == "ACTIVE"
    assert result["stage"]["next_action"] == "REQUEST_EXECUTION"
    assert controller.state["blocker_revalidations"][-1]["reason"] == "MISCLASSIFIED_STAGE_OWNED_WORK"


def test_s4_missing_generator_auto_continues_without_human():
    _controller, _attempt, observation, assessment, _decision = _blocked_controller()
    derived = classify_blocker(
        assessment=assessment,
        observation=observation,
        supplemental={"benchmark_inventory": {"missing_required_scenes": ["S4"]}},
    )
    assert derived["recommended_action"] == "WORK_REMAINING"
    assert derived["ownership_validation"]["human_only_input_found"] is False
    assert derived["ownership_validation"]["human_intervention_count"] == 0
