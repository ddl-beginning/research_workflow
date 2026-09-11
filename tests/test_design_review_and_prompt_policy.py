from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.contracts import validate_against_schema
from src.design_review import design_summary_digest
from src.human_artifacts import build_human_review, write_human_artifacts
from src.project_blueprint import _prompt_for as blueprint_prompt
from src.project_discovery import _prompt_for as discovery_prompt
from src.research_prompt_policy import (
    CANONICAL_METHOD_EVIDENCE_POLICY,
    METHOD_EVIDENCE_POLICY_ID,
    method_evidence_policy_reference,
)
from scripts.stage_cli import build_stage_consultation_policy
from src.supervisor import architecture_check, planner_decision
from src.workflow_runtime import WorkflowRuntime, WorkflowRuntimeError


def _brief() -> dict[str, object]:
    return {
        "project_goal": "prove a bounded design before execution",
        "observable_outcome": "a reviewer can inspect the accepted plan",
        "scope": ["local workspace"],
        "non_goals": ["automatic Stage creation"],
        "constraints": ["no raw prompts in artifacts"],
        "available_assets": ["local source"],
        "acceptance": ["design review is explicit"],
        "human_preferences": ["one bounded decision"],
    }


def _design() -> dict[str, object]:
    return {
        "project_final_goal": "prove a bounded project route before execution",
        "stage_count": 2,
        "stages": [
            {
                "stage_id": "stage-candidate-0",
                "name": "Candidate 0 local baseline",
                "goal": "prove the existing route is locally reproducible",
                "why_exists": "establish a bounded baseline before comparing routes",
                "inputs": ["local source", "approved brief"],
                "outputs": ["baseline evidence"],
                "acceptance": ["baseline check is reproducible"],
                "dependencies": [],
                "route_class": "CANDIDATE_0",
            },
            {
                "stage_id": "stage-alternative",
                "name": "Meaningful alternative route",
                "goal": "test a simpler route against Candidate 0",
                "why_exists": "avoid anchoring on the existing implementation",
                "inputs": ["baseline evidence"],
                "outputs": ["route comparison evidence"],
                "acceptance": ["the alternative has a bounded comparison"],
                "dependencies": ["stage-candidate-0"],
                "route_class": "ALTERNATIVE",
            },
        ],
        "major_risks": ["the local baseline may not generalize"],
        "recommended_overall_route": "compare Candidate 0 with the meaningful alternative before Step 15",
        "review_question": "Accept this bounded project design?",
        "confirmation_needed": True,
        "stage_goal": "prove one local result",
        "user_visible_goal": "show a reviewable result",
        "hypothesis": "one bounded change improves the check",
        "experiment": "change one allowed source and run the required check",
        "falsifier": "the required check fails",
        "acceptance": ["the required check passes"],
        "stop_rules": ["one iteration or a protected-path violation"],
        "allowed_paths": ["src"],
        "protected_paths": [".git", "tests"],
        "required_checks": ["pytest -q"],
        "review_artifacts": ["diff", "test report"],
        "baseline": {"digest": "baseline-digest"},
        "architecture_decision": "keep the existing local route",
    }


def _approved(root: Path) -> WorkflowRuntime:
    runtime = WorkflowRuntime(root)
    runtime.start(brief=_brief())
    runtime.answer(approve=True)
    return runtime


def test_initial_design_review_is_pending_before_step15_and_acceptance_exposes_planned(tmp_path: Path) -> None:
    runtime = _approved(tmp_path)
    pending = runtime.submit_design(_design(), trigger="INITIAL_ARCHITECTURE", consultation_ref="CONSULT-design-1")
    assert pending["phase"] == "AWAITING_DESIGN_REVIEW"
    assert pending["next_action"] == "REQUEST_DESIGN_REVIEW"
    assert pending["design_review"]["state"] == "AWAITING_DESIGN_REVIEW"
    assert pending["design_review"]["stage_created"] is False
    assert pending["design_review"]["human_decision"] is None
    assert pending["design_review"]["summary"]["stage_count"] == 2
    assert pending["design_review"]["summary"]["stages"][1]["dependencies"] == ["stage-candidate-0"]
    assert {item["route_class"] for item in pending["design_review"]["summary"]["stages"]} == {
        "CANDIDATE_0",
        "ALTERNATIVE",
    }
    assert not (tmp_path / ".research" / "stage-state.json").exists()

    accepted = runtime.accept_design_review()
    assert accepted["phase"] == "DESIGN_ACCEPTED"
    assert accepted["next_action"] == "RUN_WORKFLOW"
    assert accepted["design_review"]["step15_status"] == "READY"
    assert accepted["stage_created"] is False
    validate_against_schema(accepted, "workflow_runtime_result.v1")
    validate_against_schema(accepted["checkpoint"], "workflow_checkpoint.v1")
    assert runtime.resume()["phase"] == "DESIGN_ACCEPTED"


def test_design_acceptance_cannot_claim_planned_without_validated_step15_evidence(tmp_path: Path) -> None:
    runtime = _approved(tmp_path)
    runtime.submit_design(_design(), trigger="INITIAL_ARCHITECTURE", consultation_ref="CONSULT-design-1")
    accepted = runtime.accept_design_review()
    assert accepted["phase"] == "DESIGN_ACCEPTED"

    runtime.runner = lambda request: {
        "status": "PLANNED",
        "runner_outcome": {
            "schema_version": "workflow_runner_outcome.v1",
            "outcome": "PLANNED",
            "validated": True,
            "evidence_validated": True,
        },
    }
    with pytest.raises(WorkflowRuntimeError) as missing_evidence:
        runtime.run({"action_map": {"validated": True}})
    assert missing_evidence.value.code == "RUNNER_OUTCOME_INVALID"
    assert runtime.status()["phase"] == "DESIGN_ACCEPTED"

    runtime.runner = lambda request: {
        "status": "PLANNED",
        "stage_contract": {"schema_version": "stage_contract.v1", "stage_id": "stage-1"},
        "runner_outcome": {
            "schema_version": "workflow_runner_outcome.v1",
            "outcome": "PLANNED",
            "validated": True,
            "stage_contract_validated": True,
            "planned_evidence_validated": True,
        },
    }
    planned = runtime.run({"action_map": {"validated": True}})
    assert planned["phase"] == "PLANNED"
    assert planned["design_review"]["step15_status"] == "PLANNED"
    assert planned["design_review"]["stage_created"] is True
    assert planned["checkpoint"]["last_run"]["planned_evidence_validated"] is True
    assert len(planned["checkpoint"]["last_run"]["stage_contract_digest"]) == 64
    validate_against_schema(planned, "workflow_runtime_result.v1")
    validate_against_schema(planned["checkpoint"], "workflow_checkpoint.v1")


def test_design_feedback_requires_gpt_reconsultation_and_second_review(tmp_path: Path) -> None:
    runtime = _approved(tmp_path)
    runtime.submit_design(_design(), trigger="INITIAL_ARCHITECTURE", consultation_ref="CONSULT-design-1")
    feedback = runtime.record_design_feedback("tighten the falsifier")
    assert feedback["next_action"] == "REQUEST_DESIGN_REVIEW"
    assert feedback["design_review"]["feedback_required"] is True
    with pytest.raises(WorkflowRuntimeError) as blocked:
        runtime.accept_design_review()
    assert blocked.value.code == "DESIGN_REVIEW_RECONSULT_REQUIRED"

    revised = dict(_design())
    revised["falsifier"] = "the required check fails or the diff is empty"
    second = runtime.submit_design(revised, trigger="INITIAL_ARCHITECTURE", consultation_ref="CONSULT-design-2")
    assert second["design_review"]["revision"] == 2
    assert second["design_review"]["gpt_reconsultation_ref"] == "CONSULT-design-2"


def test_design_review_trigger_is_bounded_and_checkpoint_digest_is_fail_closed(tmp_path: Path) -> None:
    runtime = _approved(tmp_path)
    with pytest.raises(WorkflowRuntimeError) as ordinary:
        runtime.submit_design(_design(), trigger="CONTINUE", consultation_ref="CONSULT-no-review")
    assert ordinary.value.code == "DESIGN_REVIEW_NOT_REQUIRED"
    pending = runtime.submit_design(_design(), trigger="MAJOR_REPLAN", consultation_ref="CONSULT-design-1")
    checkpoint_path = tmp_path / ".research" / "workflow-state.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["design_review"]["design_digest"] = "0" * 64
    checkpoint_path.write_text(json.dumps(checkpoint, sort_keys=True), encoding="utf-8")
    with pytest.raises(WorkflowRuntimeError) as invalid:
        runtime.status()
    assert invalid.value.code == "CHECKPOINT_INVALID"
    assert pending["design_review"]["design_digest"] == design_summary_digest(pending["design_review"]["summary"])


def test_single_stage_contract_without_project_blueprint_is_rejected(tmp_path: Path) -> None:
    runtime = _approved(tmp_path)
    legacy_only = {
        "stage_goal": "a single stage is enough",
        "acceptance": ["passes"],
        "allowed_paths": ["src"],
    }
    with pytest.raises(WorkflowRuntimeError) as invalid:
        runtime.submit_design(legacy_only, trigger="INITIAL_ARCHITECTURE", consultation_ref="CONSULT-legacy")
    assert invalid.value.code == "DESIGN_SUMMARY_INVALID"


def test_human_review_runtime_update_carries_design_summary_without_transport_payload(tmp_path: Path) -> None:
    runtime = _approved(tmp_path)
    result = runtime.submit_design(_design(), trigger="INITIAL_ARCHITECTURE", consultation_ref="CONSULT-design")
    write_human_artifacts(
        tmp_path,
        event="design_review_requested",
        design_review_metadata=result["design_review"],
    )
    content = (tmp_path / "HUMAN_REVIEW.md").read_text(encoding="utf-8")
    assert "阶段设计审阅" in content
    assert "项目最终目标" in content
    assert "阶段数量：2" in content
    assert "stage-candidate-0" in content
    assert "stage-alternative" in content
    assert "路线类别：CANDIDATE_0" in content
    assert "路线类别：ALTERNATIVE" in content
    assert "依赖：" in content
    assert "推荐总体路线" in content
    assert "审阅问题" in content
    assert "需要确认：是" in content
    assert "INITIAL_ARCHITECTURE" in content
    assert "prove one local result" in content
    assert "protected_paths" not in content
    assert "raw_response" not in content
    assert "Stage created：否" in content
    assert "Step 15 状态：AWAITING_DESIGN_REVIEW" in content


def test_canonical_method_evidence_policy_covers_consultation_and_decision_seams() -> None:
    marker = METHOD_EVIDENCE_POLICY_ID
    discovery = discovery_prompt({"real_goal": "goal", "brief": {}, "asset_pack": {}})
    blueprint = blueprint_prompt({})
    stage_consultation = build_stage_consultation_policy("FRESH")
    assert marker in discovery and CANONICAL_METHOD_EVIDENCE_POLICY in discovery
    assert marker in blueprint and CANONICAL_METHOD_EVIDENCE_POLICY in blueprint
    assert marker in stage_consultation and CANONICAL_METHOD_EVIDENCE_POLICY in stage_consultation
    required_rules = (
        "mature peer-reviewed papers",
        "mature open-source projects",
        "engineering case studies",
        "项目推断",
        "证据不足",
        "Candidate 0",
        "meaningful alternative",
        "Actively expand the route space",
    )
    for rule in required_rules:
        assert rule in CANONICAL_METHOD_EVIDENCE_POLICY
        assert rule in discovery
        assert rule in blueprint
        assert rule in stage_consultation
    reference = method_evidence_policy_reference()
    assert all(rule in " ".join(reference["key_rules"]) for rule in ("Candidate 0", "meaningful alternative"))
    assert architecture_check(2, True)["method_evidence_policy"]["policy_id"] == marker
    assert planner_decision(result_status="FAILED")["method_evidence_policy"]["policy_id"] == marker
    assert reference["policy_digest"]
    assert "meaningful alternative" in json.dumps(architecture_check(2, True), ensure_ascii=False)
    assert "meaningful alternative" in json.dumps(planner_decision(result_status="FAILED"), ensure_ascii=False)
