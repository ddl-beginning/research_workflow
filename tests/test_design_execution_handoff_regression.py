"""Regression coverage for the approved-design to execution handoff.

These tests deliberately use a temporary project and a small orchestrator
fixture.  The fixture models the new handoff inputs (the approved brief,
design digest, research provenance, and human decision) and creates the
Stage through the real :class:`StageController`.  It never creates the old
Bootstrap, Discovery, or Blueprint artifacts.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from src.contracts import canonical_json, sha256_json
from src.design_review import design_summary_digest, normalize_design_summary
from src.stage_controller import StageController, StageState
from src.stage_planning import (
    StagePlanningError,
    build_human_accept_receipt,
    plan_stage_from_accepted_design,
    validate_execution_handoff,
    validate_human_accept_receipt,
)
from src.workflow_runtime import WorkflowRuntime, WorkflowRuntimeError


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _brief() -> dict[str, object]:
    return {
        "project_goal": "carry an approved design into one bounded implementation stage",
        "observable_outcome": "a reviewer can inspect a real planned Stage",
        "scope": ["src", "tests"],
        "non_goals": ["browser automation", "automatic production deployment"],
        "constraints": ["local storage", "StageController owns lifecycle transitions"],
        "available_assets": ["approved requirement baseline", "research receipts", "Design Package"],
        "acceptance": ["the execution handoff is auditable"],
        "human_preferences": ["one explicit design acceptance"],
    }


def _summary() -> dict[str, Any]:
    return normalize_design_summary(
        {
            "project_final_goal": "carry an approved design into one bounded implementation stage",
            "stage_count": 2,
            "stages": [
                {
                    "stage_id": "implementation",
                    "name": "Implementation",
                    "goal": "implement and test the accepted SQLite route",
                    "why_exists": "deliver the selected design route",
                    "inputs": ["approved requirement baseline", "Design Package"],
                    "outputs": ["implementation evidence"],
                    "acceptance": ["the scoped tests pass"],
                    "dependencies": [],
                    "route_class": "CANDIDATE_0",
                },
                {
                    "stage_id": "alternative",
                    "name": "Alternative comparison",
                    "goal": "retain a bounded comparison record",
                    "why_exists": "preserve the accepted alternative evidence",
                    "inputs": ["research receipts"],
                    "outputs": ["comparison evidence"],
                    "acceptance": ["the comparison remains inspectable"],
                    "dependencies": ["implementation"],
                    "route_class": "ALTERNATIVE",
                },
            ],
            "major_risks": ["the implementation may expose a migration issue"],
            "recommended_overall_route": "implement the accepted SQLite route",
            "review_question": "Accept this bounded implementation handoff?",
            "confirmation_needed": True,
        }
    )


def _attempt(project_id: str, phase: str, input_digest: str) -> dict[str, Any]:
    return {
        "schema_version": "workflow_attempt.v1",
        "phase": phase,
        "status": "complete",
        "attempt": 1,
        "request_budget": 0,
        "request_count": 0,
        "request_count_known": True,
        "input_digest": input_digest,
        "project_id": project_id,
        "retry_allowed": False,
        "indeterminate": False,
    }


def test_human_accept_receipt_binds_requirement_design_and_decision_digest() -> None:
    receipt = build_human_accept_receipt(
        project_id="project-fixture",
        brief_digest="a" * 64,
        design_digest="b" * 64,
        actor="human",
    )
    checked = validate_human_accept_receipt(
        receipt,
        project_id="project-fixture",
        brief_digest="a" * 64,
        design_digest="b" * 64,
    )
    assert checked["decision"] == "ACCEPT"
    assert checked["receipt_digest"]

    tampered = copy.deepcopy(receipt)
    tampered["design_digest"] = "c" * 64
    with pytest.raises(StagePlanningError):
        validate_human_accept_receipt(
            tampered,
            project_id="project-fixture",
            brief_digest="a" * 64,
            design_digest="b" * 64,
        )


def _accepted_design_inputs(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create the canonical Design Package and verified research receipt."""

    brief = json.loads((root / ".research" / "PROJECT_BRIEF.json").read_text(encoding="utf-8"))
    project_id = str(brief["project_id"])
    brief_digest = _digest(brief)
    summary = _summary()
    design_digest = design_summary_digest(summary)
    package = {
        "recommended_solution": "SQLite",
        "recommendation_reason": "transactional recovery fits the bounded notebook",
        "alternatives": ["JSON Lines"],
        "evidence": ["bounded local experiment"],
        "risks": ["migration"],
        "unknowns": ["crash timing"],
        "validation_method": ["fault injection"],
        "stage_blueprint": [{"stage_id": "implementation", "goal": "implement and test"}],
        "success_criteria": ["the scoped tests pass"],
        "non_goals": ["automatic execution"],
    }
    package_path = root / ".research" / "DESIGN_PACKAGE.json"
    package_path.parent.mkdir(parents=True, exist_ok=True)
    package_path.write_text(json.dumps(package, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    experiment_path = root / ".research" / "experiments" / "fixture.json"
    experiment_path.parent.mkdir(parents=True, exist_ok=True)
    experiment_path.write_text(
        json.dumps(
            {"experiment_id": "fixture", "status": "PASS", "evidence": "bounded local check"},
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    consultation_id = "CONSULT-fixture-research-1"
    receipt = {
        "consultation_id": consultation_id,
        "created_at": "2026-09-09T00:00:00Z",
        "mode": "continue",
        "request_count": 1,
        "chat_url": "https://chatgpt.com/c/fixture-conversation",
        "conversation_id": "fixture-conversation",
        "conversation_validated": True,
        "project_scope_verified": True,
        "project_scope_requested": True,
        "project_url": "https://chatgpt.com/g/g-p-fixture/project",
        "status": "complete",
    }
    receipt_path = root / ".consultations" / consultation_id / "receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    accepted_review = {
        "schema_version": "workflow_design_review.v1",
        "state": "ACCEPTED",
        "trigger": "INITIAL_ARCHITECTURE",
        "revision": 1,
        "design_digest": design_digest,
        "summary": summary,
        "feedback": None,
        "feedback_digest": None,
        "feedback_required": False,
        "gpt_reconsultation_ref": consultation_id,
        "stage_created": False,
        "stage_started": False,
        "step15_status": "READY",
        "human_decision": {"decision": "ACCEPT", "actor": "human"},
    }
    human_receipt = build_human_accept_receipt(
        project_id=project_id,
        brief_digest=brief_digest,
        design_digest=design_digest,
        actor="human",
    )
    return accepted_review, human_receipt


def test_real_accepted_design_planner_creates_a_planned_stage_without_bootstrap(tmp_path: Path) -> None:
    runtime = WorkflowRuntime(tmp_path)
    runtime.start(brief=_brief())
    runtime.answer(approve=True)
    accepted_review, human_receipt = _accepted_design_inputs(tmp_path)

    first = plan_stage_from_accepted_design(
        tmp_path,
        accepted_review=accepted_review,
        human_accept_receipt=human_receipt,
        stage_options={"stage_id": "implementation"},
    )
    assert first["planning_mode"] == "accepted_design_to_stage_contract"
    assert first["stage_status"] == StageState.PLANNED.value
    assert first["stage_created"] is True
    assert first["stage_started"] is False
    assert (tmp_path / ".research" / "execution-handoff" / "EXECUTION_HANDOFF.json").is_file()
    assert (tmp_path / ".research" / "design-review" / "HUMAN_ACCEPT_RECEIPT.json").is_file()
    assert not (tmp_path / ".research" / "bootstrap_state.json").exists()
    handoff = json.loads(
        (tmp_path / ".research" / "execution-handoff" / "EXECUTION_HANDOFF.json").read_text(encoding="utf-8")
    )
    checked_handoff = validate_execution_handoff(
        handoff,
        project_id=first["project_id"],
        brief_digest=first["requirement_digest"],
        design_package_digest=first["design_package_digest"],
        design_digest=first["design_digest"],
    )
    assert checked_handoff["status"] == "STAGE_PLANNED"
    assert checked_handoff["stage"]["stage_created"] is True
    state = StageController.from_state(tmp_path / ".research" / "stage-state.json")
    assert state.show_stage("implementation")["status"] == StageState.PLANNED.value

    before = (tmp_path / ".research" / "stage-state.json").read_bytes()
    second = plan_stage_from_accepted_design(
        tmp_path,
        accepted_review=accepted_review,
        human_accept_receipt=human_receipt,
        stage_options={"stage_id": "implementation"},
    )
    assert second["idempotent_reuse"] is True
    assert second["contract_digest"] == first["contract_digest"]
    assert (tmp_path / ".research" / "stage-state.json").read_bytes() == before


def test_real_accepted_design_planner_rejects_tampered_package_or_receipt(tmp_path: Path) -> None:
    runtime = WorkflowRuntime(tmp_path)
    runtime.start(brief=_brief())
    runtime.answer(approve=True)
    accepted_review, human_receipt = _accepted_design_inputs(tmp_path)
    plan_stage_from_accepted_design(
        tmp_path,
        accepted_review=accepted_review,
        human_accept_receipt=human_receipt,
        stage_options={"stage_id": "implementation"},
    )
    package_path = tmp_path / ".research" / "DESIGN_PACKAGE.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["recommended_solution"] = "forged route"
    package_path.write_text(json.dumps(package, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(StagePlanningError):
        plan_stage_from_accepted_design(
            tmp_path,
            accepted_review=accepted_review,
            human_accept_receipt=human_receipt,
            stage_options={"stage_id": "implementation"},
        )


class _DesignHandoffOrchestrator:
    """A deterministic producer for the accepted-design handoff seam."""

    def __init__(self, root: Path, *, bypass_controller: bool = False) -> None:
        self.workspace_root = root.resolve()
        self.bypass_controller = bypass_controller
        self.prepare_calls: list[dict[str, Any]] = []
        self.plan_calls: list[dict[str, Any]] = []

    def prepare(self, *, brief: Mapping[str, Any], trigger: str) -> dict[str, Any]:
        self.prepare_calls.append({"brief": copy.deepcopy(dict(brief)), "trigger": trigger})
        project_id = str(brief["project_id"])
        brief_digest = _digest(brief)
        summary = _summary()
        design_digest = design_summary_digest(summary)
        review = {
            "schema_version": "workflow_design_review.v1",
            "state": "AWAITING_DESIGN_REVIEW",
            "trigger": str(trigger).upper(),
            "revision": 1,
            "design_digest": design_digest,
            "summary": summary,
            "feedback": None,
            "feedback_digest": None,
            "feedback_required": False,
            "gpt_reconsultation_ref": "CONSULT-fixture-research-1",
            "research_provenance": {
                "consultation_id": "CONSULT-fixture-research-1",
                "receipt_status": "complete",
            },
            "stage_created": False,
            "stage_started": False,
            "step15_status": "AWAITING_DESIGN_REVIEW",
            "human_decision": None,
        }
        return {
            "schema_version": "workflow_transition.v1",
            "orchestrator_schema_version": "workflow_orchestrator.v1",
            "transition": "AWAITING_DESIGN_REVIEW",
            "project_id": project_id,
            "repository_root": self.workspace_root.as_posix(),
            "brief_digest": brief_digest,
            "validated": True,
            "retry_allowed": False,
            "stage_created": False,
            "stage_started": False,
            "next_action": "REQUEST_DESIGN_REVIEW",
            "attempts": [_attempt(project_id, "DESIGN_REVIEW", _digest({"brief": brief_digest, "design": design_digest}))],
            "artifacts": {
                "research_provenance": {
                    "consultation_id": "CONSULT-fixture-research-1",
                    "status": "complete",
                },
                "requirement_digest": brief_digest,
                "design_digest": design_digest,
            },
            "design_review": review,
        }

    def _contract(self, brief: Mapping[str, Any], stage_id: str) -> dict[str, Any]:
        return {
            "schema_version": "stage_contract.v1",
            "project_id": str(brief["project_id"]),
            "repository_root": self.workspace_root.as_posix(),
            "stage_id": stage_id,
            "stage_name": "Approved design implementation",
            "project_goal": str(brief["brief"]["goal"]),
            "stage_goal": "implement and test the accepted SQLite route",
            "user_visible_goal": "make the approved design executable and reviewable",
            "inputs": [".research/PROJECT_BRIEF.json", ".research/DESIGN_PACKAGE.json"],
            "protected_paths": [".research"],
            "allowed_paths": ["src", "tests"],
            "acceptance_description": "the scoped implementation and tests pass",
            "required_checks": ["pytest"],
            "review_artifact_requirements": ["technical_review"],
            "baseline": {"requirement_digest": _digest(brief)},
            "status": "PLANNED",
            "max_iterations": 1,
        }

    def plan_accepted(
        self,
        accepted_review: Mapping[str, Any],
        *,
        brief: Mapping[str, Any],
        stage_options: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.plan_calls.append(
            {"review": copy.deepcopy(dict(accepted_review)), "stage_options": copy.deepcopy(stage_options)}
        )
        options = dict(stage_options or {})
        stage_id = str(options.get("stage_id", "implementation"))
        contract = self._contract(brief, stage_id)
        contract_path = self.workspace_root / ".research" / "stages" / stage_id / "contract.json"
        state_path = self.workspace_root / ".research" / "stage-state.json"
        if not self.bypass_controller:
            contract_path.parent.mkdir(parents=True, exist_ok=True)
            contract_path.write_text(json.dumps(contract, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            controller = StageController(state_path=state_path)
            controller.prepare_stage(contract)
        contract_digest = sha256_json(contract)
        stage_plan = {
            "schema_version": "stage_planning.v1",
            "status": "PLANNED",
            "stage_status": "PLANNED",
            "stage_id": stage_id,
            "plan_id": "plan-approved-design-1",
            "stage_plan_path": ".research/stage-planning/STAGE_PLAN.json",
            "stage_contract_path": contract_path.relative_to(self.workspace_root).as_posix(),
            "stage_state_path": state_path.relative_to(self.workspace_root).as_posix(),
            "contract_digest": contract_digest,
            "stage_started": False,
            "stage_controller": {"validated": True, "status": "PLANNED"},
        }
        project_id = str(brief["project_id"])
        handoff = {
            "requirement_digest": _digest(brief),
            "design_digest": str(accepted_review["design_digest"]),
            "human_accept": copy.deepcopy(accepted_review.get("human_decision")),
            "research_provenance": {"consultation_id": "CONSULT-fixture-research-1"},
        }
        return {
            "schema_version": "workflow_transition.v1",
            "orchestrator_schema_version": "workflow_orchestrator.v1",
            "transition": "STAGE_PLANNED",
            "project_id": project_id,
            "repository_root": self.workspace_root.as_posix(),
            "brief_digest": _digest(brief),
            "validated": True,
            "retry_allowed": False,
            "stage_created": True,
            "stage_started": False,
            "next_action": "RUN_WORKFLOW",
            "attempts": [_attempt(project_id, "STAGE_PLANNING", _digest(handoff))],
            "artifacts": {"execution_handoff": handoff},
            "stage_plan": stage_plan,
        }


def _approved_runtime(root: Path, orchestrator: _DesignHandoffOrchestrator) -> WorkflowRuntime:
    runtime = WorkflowRuntime(root, orchestrator=orchestrator)
    runtime.start(brief=_brief())
    approved = runtime.answer(approve=True)
    assert approved["phase"] == "READY"
    return runtime


def _pending_runtime(tmp_path: Path) -> tuple[WorkflowRuntime, _DesignHandoffOrchestrator]:
    orchestrator = _DesignHandoffOrchestrator(tmp_path)
    runtime = _approved_runtime(tmp_path, orchestrator)
    pending = runtime.run({"trigger": "INITIAL_ARCHITECTURE"})
    assert pending["phase"] == "AWAITING_DESIGN_REVIEW"
    assert pending["stage_created"] is False
    assert not (tmp_path / ".research" / "stage-state.json").exists()
    return runtime, orchestrator


def test_acceptance_is_the_only_boundary_before_stage_side_effects(tmp_path: Path) -> None:
    runtime, _ = _pending_runtime(tmp_path)

    with pytest.raises(WorkflowRuntimeError) as blocked:
        runtime.run({"trigger": "INITIAL_ARCHITECTURE"})
    assert blocked.value.code == "DESIGN_REVIEW_REQUIRED"
    assert not (tmp_path / ".research" / "stage-state.json").exists()
    assert not (tmp_path / ".research" / "bootstrap_state.json").exists()

    accepted = runtime.accept_design_review(actor="human")
    assert accepted["phase"] == "DESIGN_ACCEPTED"
    assert accepted["design_review"]["human_decision"]["decision"] == "ACCEPT"
    assert accepted["design_review"]["stage_created"] is False
    assert not (tmp_path / ".research" / "stage-state.json").exists()


def test_approved_design_handoff_plans_through_stage_controller_without_bootstrap_history(tmp_path: Path) -> None:
    runtime, orchestrator = _pending_runtime(tmp_path)
    runtime.accept_design_review(actor="human")

    planned = runtime.run({"stage_options": {"stage_id": "implementation"}})
    assert planned["phase"] == "PLANNED"
    assert planned["stage_created"] is True
    assert planned["stage_started"] is False
    assert planned["checkpoint"]["design_review"]["step15_status"] == "PLANNED"
    assert planned["checkpoint"]["last_run"]["planned_evidence_validated"] is True
    assert len(orchestrator.plan_calls) == 1

    state = StageController.from_state(tmp_path / ".research" / "stage-state.json")
    stage = state.show_stage("implementation")
    assert stage["status"] == StageState.PLANNED.value
    assert state.state["active_stage_id"] is None
    assert not (tmp_path / ".research" / "bootstrap_state.json").exists()
    assert not (tmp_path / ".research" / "discovery").exists()
    assert not (tmp_path / ".research" / "blueprint").exists()


def test_human_acceptance_tampering_fails_closed_on_resume(tmp_path: Path) -> None:
    runtime, _ = _pending_runtime(tmp_path)
    runtime.accept_design_review(actor="human")
    checkpoint_path = tmp_path / ".research" / "workflow-state.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    checkpoint["design_review"]["human_decision"]["decision"] = "REJECT"
    checkpoint_path.write_text(json.dumps(checkpoint, sort_keys=True), encoding="utf-8")

    with pytest.raises(WorkflowRuntimeError):
        WorkflowRuntime(tmp_path).resume()


def test_design_or_transition_digest_tampering_fails_closed(tmp_path: Path) -> None:
    runtime, _ = _pending_runtime(tmp_path)
    runtime.accept_design_review(actor="human")
    checkpoint_path = tmp_path / ".research" / "workflow-state.json"
    original = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    try:
        tampered = copy.deepcopy(original)
        tampered["design_review"]["design_digest"] = "0" * 64
        checkpoint_path.write_text(json.dumps(tampered, sort_keys=True), encoding="utf-8")
        with pytest.raises(WorkflowRuntimeError):
            runtime.resume()

        tampered = copy.deepcopy(original)
        tampered["orchestrator_transition"]["project_id"] = "another-project"
        checkpoint_path.write_text(json.dumps(tampered, sort_keys=True), encoding="utf-8")
        with pytest.raises(WorkflowRuntimeError):
            runtime.resume()
    finally:
        checkpoint_path.write_text(json.dumps(original, sort_keys=True), encoding="utf-8")


def test_requirement_drift_after_handoff_fails_closed_before_execution(tmp_path: Path) -> None:
    runtime, orchestrator = _pending_runtime(tmp_path)
    runtime.accept_design_review(actor="human")
    brief_path = tmp_path / ".research" / "PROJECT_BRIEF.json"
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    brief["brief"]["goal"] = "a drifted requirement must not reach execution"
    brief["revision"] += 1
    brief_path.write_text(json.dumps(brief, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(WorkflowRuntimeError):
        runtime.run({"stage_options": {"stage_id": "implementation"}})
    checkpoint = json.loads((tmp_path / ".research" / "workflow-state.json").read_text(encoding="utf-8"))
    assert checkpoint["phase"] == "DESIGN_ACCEPTED"
    assert checkpoint["design_review"]["stage_created"] is False
    assert not (tmp_path / ".research" / "stage-state.json").exists()
    assert orchestrator.plan_calls == []


def test_stage_controller_claim_without_controller_state_is_rejected(tmp_path: Path) -> None:
    orchestrator = _DesignHandoffOrchestrator(tmp_path, bypass_controller=True)
    runtime = _approved_runtime(tmp_path, orchestrator)
    runtime.run({"trigger": "INITIAL_ARCHITECTURE"})
    runtime.accept_design_review(actor="human")

    with pytest.raises(WorkflowRuntimeError):
        runtime.run({"stage_options": {"stage_id": "implementation"}})
    assert runtime.status()["phase"] == "DESIGN_ACCEPTED"
    assert not (tmp_path / ".research" / "stage-state.json").exists()


def test_resume_reuses_planned_handoff_without_duplicate_stage_registration(tmp_path: Path) -> None:
    runtime, orchestrator = _pending_runtime(tmp_path)
    runtime.accept_design_review(actor="human")
    runtime.run({"stage_options": {"stage_id": "implementation"}})

    checkpoint_path = tmp_path / ".research" / "workflow-state.json"
    state_path = tmp_path / ".research" / "stage-state.json"
    checkpoint_before = checkpoint_path.read_bytes()
    state_before = state_path.read_bytes()
    state_payload = json.loads(state_before)
    revision_before = state_payload["revision"]

    resumed = WorkflowRuntime(tmp_path, orchestrator=orchestrator).resume()
    assert resumed["phase"] == "PLANNED"
    assert resumed["resumed"] is True
    assert checkpoint_path.read_bytes() == checkpoint_before
    assert state_path.read_bytes() == state_before
    assert json.loads(state_path.read_text(encoding="utf-8"))["revision"] == revision_before
    assert len(orchestrator.plan_calls) == 1
