from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from src.contracts import canonical_json
from src.design_review import design_summary_digest, normalize_design_summary
from src.stage_planning import bootstrap_state_digest
from src.runtime_composition import build_workflow_orchestrator
from src.workflow_mcp import WorkflowMCPServer
from src.workflow_orchestrator import (
    WORKFLOW_ORCHESTRATOR_SCHEMA_VERSION,
    WORKFLOW_TRANSITION_SCHEMA_VERSION,
)
from src.workflow_runtime import WorkflowRuntime, WorkflowRuntimeError


def _brief() -> dict[str, object]:
    return {
        "project_goal": "prove the default Bootstrap seam",
        "observable_outcome": "a reviewer can inspect the pending and planned boundaries",
        "scope": ["local workspace"],
        "non_goals": ["real GPT or executor execution"],
        "constraints": ["no secrets in artifacts"],
        "available_assets": ["bounded local evidence"],
        "acceptance": ["the checkpoint transition is validated"],
        "human_preferences": ["one explicit design decision"],
    }


def _summary() -> dict[str, object]:
    return normalize_design_summary(
        {
            "project_final_goal": "prove the default Bootstrap seam",
            "stage_count": 2,
            "stages": [
                {
                    "stage_id": "candidate-0",
                    "name": "Candidate 0",
                    "goal": "retain the measured baseline",
                    "why_exists": "preserve the known route",
                    "inputs": [],
                    "outputs": ["baseline evidence"],
                    "acceptance": ["baseline evidence is present"],
                    "dependencies": [],
                    "route_class": "CANDIDATE_0",
                },
                {
                    "stage_id": "alternative",
                    "name": "Alternative",
                    "goal": "compare one bounded alternative",
                    "why_exists": "avoid anchoring on one route",
                    "inputs": ["baseline evidence"],
                    "outputs": ["comparison evidence"],
                    "acceptance": ["comparison evidence is present"],
                    "dependencies": ["candidate-0"],
                    "route_class": "ALTERNATIVE",
                },
            ],
            "major_risks": ["the alternative may not improve the baseline"],
            "recommended_overall_route": "compare both bounded routes",
            "review_question": "Accept this bounded design?",
            "confirmation_needed": True,
        }
    )


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _bootstrap(root: Path, brief: dict[str, object]) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "bootstrap_state.v1",
        "status": "BOOTSTRAP_COMPLETE",
        "marker": "PROJECT_BOOTSTRAP_RESEARCH_PASS",
        "markers": ["PROJECT_BOOTSTRAP_RESEARCH_PASS", "WORKFLOW_BOOTSTRAP_ORCHESTRATOR_PASS"],
        "next_action": "READY_FOR_STAGE_PLANNING",
        "project_scope_verified": True,
        "project_id": brief["project_id"],
        "brief_digest": _digest(brief),
        "context_digest": "1" * 64,
        "discovery_digest": "2" * 64,
        "blueprint_digest": "3" * 64,
        "stage_created": False,
        "stage_started": False,
    }
    value["state_digest"] = bootstrap_state_digest(value)
    return value


def _attempt(phase: str, digest: str) -> dict[str, object]:
    return {
        "schema_version": "workflow_attempt.v1",
        "phase": phase,
        "status": "complete",
        "attempt": 1,
        "request_budget": 0,
        "request_count": 0,
        "request_count_known": True,
        "input_digest": digest,
        "project_id": "placeholder",
        "retry_allowed": False,
        "indeterminate": False,
    }


class _RecordingOrchestrator:
    def __init__(self, root: Path) -> None:
        self.workspace_root = root.resolve()
        self.prepare_calls: list[dict[str, object]] = []
        self.plan_calls: list[dict[str, object]] = []

    def prepare(self, *, brief: dict[str, object], trigger: str) -> dict[str, object]:
        self.prepare_calls.append({"brief": copy.deepcopy(brief), "trigger": trigger})
        project_id = str(brief["project_id"])
        brief_digest = _digest(brief)
        bootstrap = _bootstrap(self.workspace_root, brief)
        summary = _summary()
        review = {
            "schema_version": "workflow_design_review.v1",
            "state": "AWAITING_DESIGN_REVIEW",
            "trigger": trigger,
            "revision": 1,
            "design_digest": design_summary_digest(summary),
            "summary": summary,
            "feedback": None,
            "feedback_digest": None,
            "feedback_required": False,
            "gpt_reconsultation_ref": "fixture-bootstrap",
            "stage_created": False,
            "stage_started": False,
            "step15_status": "AWAITING_DESIGN_REVIEW",
            "human_decision": None,
        }
        attempt = _attempt("CONTEXT", "4" * 64)
        attempt["project_id"] = project_id
        return {
            "schema_version": WORKFLOW_TRANSITION_SCHEMA_VERSION,
            "orchestrator_schema_version": WORKFLOW_ORCHESTRATOR_SCHEMA_VERSION,
            "transition": "AWAITING_DESIGN_REVIEW",
            "project_id": project_id,
            "repository_root": self.workspace_root.as_posix(),
            "brief_digest": brief_digest,
            "validated": True,
            "retry_allowed": False,
            "stage_created": False,
            "stage_started": False,
            "next_action": "REQUEST_DESIGN_REVIEW",
            "attempts": [attempt],
            "artifacts": {"bootstrap_state_path": ".research/bootstrap_state.json"},
            "design_review": review,
            "bootstrap_state": bootstrap,
        }

    def plan_accepted(
        self,
        accepted_review: dict[str, object],
        *,
        brief: dict[str, object],
        stage_options: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.plan_calls.append({"review": copy.deepcopy(accepted_review), "stage_options": copy.deepcopy(stage_options)})
        contract_digest = "5" * 64
        plan = {
            "schema_version": "stage_planning.v1",
            "status": "PLANNED",
            "stage_status": "PLANNED",
            "stage_id": "candidate-0",
            "plan_id": "fixture-plan",
            "stage_plan_path": ".research/stage-planning/STAGE_PLAN.json",
            "stage_contract_path": ".research/stages/candidate-0/contract.json",
            "stage_state_path": ".research/stage-state.json",
            "contract_digest": contract_digest,
            "stage_started": False,
            "verification": {"passed": True},
        }
        attempt = _attempt("STAGE_PLANNING", "6" * 64)
        attempt["project_id"] = str(brief["project_id"])
        return {
            "schema_version": WORKFLOW_TRANSITION_SCHEMA_VERSION,
            "orchestrator_schema_version": WORKFLOW_ORCHESTRATOR_SCHEMA_VERSION,
            "transition": "STAGE_PLANNED",
            "project_id": brief["project_id"],
            "repository_root": self.workspace_root.as_posix(),
            "brief_digest": _digest(brief),
            "validated": True,
            "retry_allowed": False,
            "stage_created": True,
            "stage_started": False,
            "next_action": "RUN_WORKFLOW",
            "attempts": [attempt],
            "artifacts": {"stage_plan": {"path": plan["stage_plan_path"]}},
            "stage_plan": plan,
        }


def _approved(root: Path, *, orchestrator: _RecordingOrchestrator | None = None) -> WorkflowRuntime:
    runtime = WorkflowRuntime(root, orchestrator=orchestrator)
    runtime.start(brief=_brief())
    runtime.answer(approve=True)
    return runtime


def test_default_runtime_factory_reaches_prepare_and_writes_pending_boundary(tmp_path: Path) -> None:
    calls: list[Path] = []
    orchestrator = _RecordingOrchestrator(tmp_path)

    def factory(root: Path) -> _RecordingOrchestrator:
        calls.append(Path(root))
        return orchestrator

    runtime = WorkflowRuntime(tmp_path, orchestrator_factory=factory)
    runtime.start(brief=_brief())
    runtime.answer(approve=True)
    pending = runtime.run({"trigger": "INITIAL_ARCHITECTURE"})

    assert calls == [tmp_path.resolve()]
    assert len(orchestrator.prepare_calls) == 1
    assert pending["phase"] == "AWAITING_DESIGN_REVIEW"
    assert pending["next_action"] == "REQUEST_DESIGN_REVIEW"
    assert pending["stage_created"] is False
    assert not (tmp_path / ".research" / "stage-state.json").exists()
    assert (tmp_path / ".research" / "bootstrap_state.json").is_file()
    assert all((tmp_path / name).is_file() for name in ("STAGE_EXECUTION_PLAN.md", "RESEARCH_DECISION_LOG.md", "HUMAN_REVIEW.md"))


def test_design_accept_then_plan_accepted_persists_planned_transition(tmp_path: Path) -> None:
    orchestrator = _RecordingOrchestrator(tmp_path)
    runtime = _approved(tmp_path, orchestrator=orchestrator)
    runtime.run({"trigger": "INITIAL_ARCHITECTURE"})
    runtime.accept_design_review(actor="human")

    planned = runtime.run({"stage_options": {"stage_id": "candidate-0"}})

    assert len(orchestrator.plan_calls) == 1
    assert planned["phase"] == "PLANNED"
    assert planned["checkpoint"]["orchestrator_transition"]["transition"] == "STAGE_PLANNED"
    assert planned["design_review"]["step15_status"] == "PLANNED"
    assert planned["checkpoint"]["last_run"]["planned_evidence_validated"] is True


def test_orchestrator_checkpoint_identity_and_digest_tampering_fail_closed(tmp_path: Path) -> None:
    orchestrator = _RecordingOrchestrator(tmp_path)
    runtime = _approved(tmp_path, orchestrator=orchestrator)
    runtime.run({"trigger": "INITIAL_ARCHITECTURE"})
    checkpoint_path = tmp_path / ".research" / "workflow-state.json"
    original = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    try:
        tampered = copy.deepcopy(original)
        tampered["bootstrap_state"]["state_digest"] = "0" * 64
        checkpoint_path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(WorkflowRuntimeError) as invalid_digest:
            runtime.status()
        assert invalid_digest.value.code == "CHECKPOINT_INVALID"

        tampered = copy.deepcopy(original)
        tampered["orchestrator_transition"]["project_id"] = "other-project"
        checkpoint_path.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(WorkflowRuntimeError) as invalid_identity:
            runtime.status()
        assert invalid_identity.value.code == "PROJECT_IDENTITY_MISMATCH"
    finally:
        checkpoint_path.write_text(json.dumps(original), encoding="utf-8")


def test_mcp_default_path_uses_injected_orchestrator_before_core_composition(tmp_path: Path) -> None:
    orchestrator = _RecordingOrchestrator(tmp_path)
    server = WorkflowMCPServer(orchestrator_factory=lambda _workspace: orchestrator)
    server.call_tool("workflow_start", {"workspace": str(tmp_path), "brief": _brief()})
    server.call_tool("workflow_answer", {"workspace": str(tmp_path), "mode": "APPROVE"})

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "bootstrap",
            "method": "tools/call",
            "params": {
                "name": "workflow_run",
                "arguments": {"workspace": str(tmp_path), "request": {"trigger": "INITIAL_ARCHITECTURE"}},
            },
        }
    )

    assert response["result"]["isError"] is False
    assert response["result"]["structuredContent"]["next_action"] == "REQUEST_DESIGN_REVIEW"
    assert len(orchestrator.prepare_calls) == 1


def test_configuration_factory_allows_deterministic_context_bootstrap_when_missing(tmp_path: Path) -> None:
    consultant_factory = lambda _phase, _root: object()
    orchestrator = build_workflow_orchestrator(tmp_path, consultant_factory=consultant_factory)
    assert orchestrator.workspace_root == tmp_path.resolve()
    assert not (tmp_path / ".research" / "PROJECT_CONTEXT.md").exists()
