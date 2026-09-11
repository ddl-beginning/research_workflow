from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

import pytest

from src.asset_layer import CANDIDATE_USER_PROJECT_ASSET_ID
from src.contracts import sha256_json
from src.project_blueprint import (
    BLUEPRINT_MANIFEST_RELATIVE_PATH,
    PROJECT_BLUEPRINT_MARKER,
    PROJECT_BLUEPRINT_RELATIVE_PATH,
    PROJECT_BLUEPRINT_STATUS,
    READY_FOR_STAGE_PLANNING,
)
from src.project_context import PROJECT_CONTEXT_RELATIVE_PATH
from src.project_discovery import DISCOVERY_REPORT_MARKER, DISCOVERY_REPORT_RELATIVE_PATH, DISCOVERY_STATUS
from src.project_intake import ProjectRequirementsIntake
from src.stage_planning import BOOTSTRAP_STATE_RELATIVE_PATH, STAGE_PLAN_RELATIVE_PATH
from src.workflow_orchestrator import (
    WorkflowOrchestrator,
    WorkflowOrchestratorDependencies,
    WorkflowOrchestratorError,
)


class _FixtureError(RuntimeError):
    def __init__(self, code: str, message: str = "fixture failure") -> None:
        self.code = code
        super().__init__(message)


def _approved_brief(root: Path) -> dict[str, Any]:
    intake = ProjectRequirementsIntake(root)
    intake.initialize(
        mode="USER_CONFIRMED_BRIEF",
        brief={
            "title": "orchestrator fixture",
            "goal": "prove the bounded Bootstrap handoff",
            "desired_outcome": "a reviewer can inspect the proposed route",
            "success_criteria": ["the design review is explicit"],
            "scope": ["local workspace"],
            "constraints": ["no Stage starts during Bootstrap"],
            "non_goals": ["real GPT transport"],
        },
    )
    return intake.approve()["project_brief"]


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


class _FixtureServices:
    def __init__(self, root: Path, *, fail_phase: str | None = None, unsafe_plan_path: bool = False) -> None:
        self.root = root
        self.fail_phase = fail_phase
        self.unsafe_plan_path = unsafe_plan_path
        self.consultation_calls: list[str] = []
        self.plan_calls: list[dict[str, Any]] = []
        self.context_digest = ""
        self.discovery: dict[str, Any] = {}
        self.blueprint: dict[str, Any] = {}

    def _consult(self, phase: str, consultant: Any) -> None:
        self.consultation_calls.append(phase)
        consultant({"bounded": True, "phase": phase})
        if self.fail_phase == phase:
            raise _FixtureError(f"{phase}_INDETERMINATE")

    def build_context(self, root: Path, *, brief: Mapping[str, Any]) -> Mapping[str, Any]:
        path = root / PROJECT_CONTEXT_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PROJECT_CONTEXT_COMPACTION_PASS\n", encoding="utf-8")
        self.context_digest = "1" * 64
        return {"context_digest": self.context_digest}

    def verify_context(self, root: Path) -> Mapping[str, Any]:
        if not (root / PROJECT_CONTEXT_RELATIVE_PATH).is_file():
            raise _FixtureError("CONTEXT_NOT_FOUND")
        return {"context_digest": self.context_digest}

    def load_context(self, root: Path) -> Mapping[str, Any]:
        return {"context_digest": self.context_digest, "target": "bounded Bootstrap"}

    def discover(
        self,
        root: Path,
        *,
        consultant: Any,
        verifier: Any,
        brief: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._consult("DISCOVERY", consultant)
        report = {
            "schema_version": "discovery_report.v1",
            "status": DISCOVERY_STATUS,
            "marker": DISCOVERY_REPORT_MARKER,
            "project_id": brief["project_id"],
            "brief_digest": sha256_json(dict(brief)),
            "evidence_digest": "2" * 64,
            "candidate_zero_asset_id": CANDIDATE_USER_PROJECT_ASSET_ID,
            "consultation": {"request_count": 1, "consultation_id": "discovery-consult-1"},
        }
        path = root / DISCOVERY_REPORT_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        self.discovery = report
        return report

    def verify_discovery(self, root: Path) -> Mapping[str, Any]:
        return _read_json(root / DISCOVERY_REPORT_RELATIVE_PATH)

    def load_discovery(self, root: Path) -> Mapping[str, Any]:
        return _read_json(root / DISCOVERY_REPORT_RELATIVE_PATH)

    def build_blueprint(
        self,
        root: Path,
        *,
        consultant: Any,
        brief: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self._consult("BLUEPRINT", consultant)
        blueprint = {
            "schema_version": "project_blueprint.v1",
            "status": PROJECT_BLUEPRINT_STATUS,
            "next_action": READY_FOR_STAGE_PLANNING,
            "marker": PROJECT_BLUEPRINT_MARKER,
            "project_id": brief["project_id"],
            "brief_digest": sha256_json(dict(brief)),
            "context_digest": self.context_digest,
            "discovery_digest": self.discovery["evidence_digest"],
            "blueprint_digest": "3" * 64,
            "feasibility_digest": "4" * 64,
            "stage_created": False,
            "stage_started": False,
            "primary_route": {
                "route_id": "candidate-0",
                "title": "Candidate 0",
                "summary": "retain the locally audited route",
                "steps": ["run the bounded local check"],
            },
            "alternatives": [
                {
                    "route_id": "alternative-1",
                    "title": "Meaningful alternative",
                    "summary": "compare a simpler route",
                    "steps": ["run the same bounded check"],
                }
            ],
            "important_risks": ["the local comparison may falsify the current route"],
            "why_primary": "Candidate 0 has the local audit evidence.",
            "consultation": {"request_count": 1, "claims_digest": "5" * 64},
        }
        blueprint_path = root / PROJECT_BLUEPRINT_RELATIVE_PATH
        blueprint_path.parent.mkdir(parents=True, exist_ok=True)
        blueprint_path.write_text("GPT_CODEX_BLUEPRINT_REVIEW_PASS\n", encoding="utf-8")
        manifest_path = root / BLUEPRINT_MANIFEST_RELATIVE_PATH
        manifest_path.write_text("{}\n", encoding="utf-8")
        self.blueprint = blueprint
        return blueprint

    def verify_blueprint(self, root: Path) -> Mapping[str, Any]:
        return self.blueprint

    def load_blueprint(self, root: Path) -> Mapping[str, Any]:
        return copy.deepcopy(self.blueprint)

    def plan_stage(self, root: Path, *, bootstrap_state_path: Path, **options: Any) -> Mapping[str, Any]:
        self.plan_calls.append({"bootstrap_state_path": bootstrap_state_path, "options": copy.deepcopy(options)})
        contract_path = "../escape/STAGE_CONTRACT.json" if self.unsafe_plan_path else ".research/stage-planning/STAGE_CONTRACT.json"
        state_path = ".research/stage-planning/STAGE_STATE.json"
        plan = {
            "status": "STAGE_PLANNING_COMPLETE",
            "stage_status": "PLANNED",
            "stage_started": False,
            "stage_id": options.get("stage_id", "stage-fixture"),
            "plan_id": "plan-stage-fixture",
            "stage_plan_path": STAGE_PLAN_RELATIVE_PATH.as_posix(),
            "stage_contract_path": contract_path,
            "stage_state_path": state_path,
            "contract_digest": "6" * 64,
        }
        path = root / STAGE_PLAN_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
        return plan

    def verify_stage_plan(self, root: Path) -> Mapping[str, Any]:
        return {"stage_status": "PLANNED", "stage_started": False}

    def dependencies(self) -> WorkflowOrchestratorDependencies:
        return WorkflowOrchestratorDependencies(
            build_context=self.build_context,
            verify_context=self.verify_context,
            load_context=self.load_context,
            discover=self.discover,
            verify_discovery=self.verify_discovery,
            load_discovery=self.load_discovery,
            build_blueprint=self.build_blueprint,
            verify_blueprint=self.verify_blueprint,
            load_blueprint=self.load_blueprint,
            plan_stage=self.plan_stage,
            verify_stage_plan=self.verify_stage_plan,
        )


def _orchestrator(root: Path, services: _FixtureServices) -> tuple[WorkflowOrchestrator, list[str]]:
    factory_calls: list[str] = []

    def factory(phase: str, workspace: Path) -> Any:
        factory_calls.append(phase)
        return lambda payload: {"phase": phase, "bounded": True}

    return (
        WorkflowOrchestrator(root, consultant_factory=factory, dependencies=services.dependencies()),
        factory_calls,
    )


def _persist_bootstrap(root: Path, prepared: Mapping[str, Any]) -> None:
    path = root / BOOTSTRAP_STATE_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prepared["bootstrap_state"], sort_keys=True), encoding="utf-8")


def _accepted_review(prepared: Mapping[str, Any]) -> dict[str, Any]:
    review = copy.deepcopy(prepared["design_review"])
    review.update({"state": "ACCEPTED", "human_decision": {"decision": "ACCEPT", "actor": "fixture"}})
    return review


def test_prepare_is_two_bounded_fresh_consultations_then_reuses_candidate0_blueprint(tmp_path: Path) -> None:
    brief = _approved_brief(tmp_path)
    services = _FixtureServices(tmp_path)
    orchestrator, factory_calls = _orchestrator(tmp_path, services)

    first = orchestrator.prepare(brief=brief)
    assert first["transition"] == "AWAITING_DESIGN_REVIEW"
    assert first["next_action"] == "REQUEST_DESIGN_REVIEW"
    assert first["stage_created"] is False
    assert first["stage_started"] is False
    assert first["design_review"]["state"] == "AWAITING_DESIGN_REVIEW"
    assert first["design_review"]["design_digest"]
    assert first["bootstrap_state"]["stage_created"] is False
    assert first["bootstrap_state"]["stage_started"] is False
    assert factory_calls == ["DISCOVERY", "BLUEPRINT"]
    attempts = {item["phase"]: item for item in first["attempts"]}
    assert attempts["DISCOVERY"]["request_budget"] == 1
    assert attempts["DISCOVERY"]["request_count"] == 1
    assert attempts["BLUEPRINT"]["request_budget"] == 1
    assert attempts["BLUEPRINT"]["request_count"] == 1
    assert not (tmp_path / ".research" / "workflow-state.json").exists()
    assert not (tmp_path / ".research" / "stage-state.json").exists()

    second = orchestrator.prepare(brief=brief)
    assert second["transition"] == "AWAITING_DESIGN_REVIEW"
    assert factory_calls == ["DISCOVERY", "BLUEPRINT"]
    reused = {item["phase"]: item for item in second["attempts"]}
    assert reused["DISCOVERY"]["status"] == "reused"
    assert reused["DISCOVERY"]["request_budget"] == 0
    assert reused["BLUEPRINT"]["status"] == "reused"
    assert reused["BLUEPRINT"]["request_budget"] == 0


def test_prepare_rejects_stale_candidate0_or_blueprint_identity(tmp_path: Path) -> None:
    brief = _approved_brief(tmp_path)
    services = _FixtureServices(tmp_path)
    orchestrator, _ = _orchestrator(tmp_path, services)
    orchestrator.prepare(brief=brief)

    discovery_path = tmp_path / DISCOVERY_REPORT_RELATIVE_PATH
    discovery = _read_json(discovery_path)
    discovery["project_id"] = "wrong-project"
    discovery_path.write_text(json.dumps(discovery, sort_keys=True), encoding="utf-8")
    with pytest.raises(WorkflowOrchestratorError) as stale_discovery:
        orchestrator.prepare(brief=brief)
    assert stale_discovery.value.code == "DISCOVERY_INPUT_MISMATCH"

    discovery["project_id"] = brief["project_id"]
    discovery_path.write_text(json.dumps(discovery, sort_keys=True), encoding="utf-8")
    blueprint = copy.deepcopy(services.blueprint)
    blueprint["project_id"] = "wrong-project"
    services.blueprint = blueprint
    with pytest.raises(WorkflowOrchestratorError) as stale_blueprint:
        orchestrator.prepare(brief=brief)
    assert stale_blueprint.value.code == "BLUEPRINT_INPUT_MISMATCH"


def test_plan_accepted_calls_step15_and_never_starts_stage(tmp_path: Path) -> None:
    brief = _approved_brief(tmp_path)
    services = _FixtureServices(tmp_path)
    orchestrator, _ = _orchestrator(tmp_path, services)
    prepared = orchestrator.prepare(brief=brief)
    _persist_bootstrap(tmp_path, prepared)

    planned = orchestrator.plan_accepted(
        _accepted_review(prepared),
        brief=brief,
        stage_options={
            "stage_id": "stage-fixture",
            "stage_name": "fixture Stage",
            "allowed_paths": ["src"],
            "required_checks": ["pytest -q"],
        },
    )
    assert planned["transition"] == "STAGE_PLANNED"
    assert planned["next_action"] == "RUN_WORKFLOW"
    assert planned["stage_created"] is True
    assert planned["stage_started"] is False
    assert planned["stage_plan"]["stage_status"] == "PLANNED"
    assert len(services.plan_calls) == 1
    assert services.plan_calls[0]["options"]["stage_id"] == "stage-fixture"
    assert not (tmp_path / ".research" / "workflow-state.json").exists()
    assert not (tmp_path / ".research" / "stage-state.json").exists()


def test_plan_accepted_requires_digest_and_reconsultation_after_feedback(tmp_path: Path) -> None:
    brief = _approved_brief(tmp_path)
    services = _FixtureServices(tmp_path)
    orchestrator, _ = _orchestrator(tmp_path, services)
    prepared = orchestrator.prepare(brief=brief)
    _persist_bootstrap(tmp_path, prepared)

    invalid_digest = _accepted_review(prepared)
    invalid_digest["design_digest"] = "0" * 64
    with pytest.raises(WorkflowOrchestratorError) as digest_error:
        orchestrator.plan_accepted(invalid_digest, brief=brief)
    assert digest_error.value.code == "DESIGN_REVIEW_INVALID"

    feedback = _accepted_review(prepared)
    feedback["feedback_required"] = True
    feedback["gpt_reconsultation_ref"] = None
    with pytest.raises(WorkflowOrchestratorError) as reconsult_error:
        orchestrator.plan_accepted(feedback, brief=brief)
    assert reconsult_error.value.code == "DESIGN_REVIEW_RECONSULT_REQUIRED"

    feedback["gpt_reconsultation_ref"] = "CONSULT-revised-1"
    planned = orchestrator.plan_accepted(feedback, brief=brief)
    assert planned["transition"] == "STAGE_PLANNED"
    assert planned["stage_started"] is False


def test_indeterminate_discovery_is_bounded_no_retry_and_unsafe_plan_path_is_rejected(tmp_path: Path) -> None:
    brief = _approved_brief(tmp_path)
    services = _FixtureServices(tmp_path, fail_phase="DISCOVERY")
    orchestrator, factory_calls = _orchestrator(tmp_path, services)
    with pytest.raises(WorkflowOrchestratorError) as indeterminate:
        orchestrator.prepare(brief=brief)
    assert indeterminate.value.code == "DISCOVERY_INDETERMINATE"
    assert indeterminate.value.details["retry_allowed"] is False
    assert indeterminate.value.details["indeterminate"] is True
    assert indeterminate.value.details["attempts"][-1]["request_count"] is None
    assert factory_calls == ["DISCOVERY"]

    safe_services = _FixtureServices(tmp_path / "safe", unsafe_plan_path=True)
    safe_services.root.mkdir(parents=True, exist_ok=True)
    safe_brief = _approved_brief(safe_services.root)
    safe_orchestrator, _ = _orchestrator(safe_services.root, safe_services)
    prepared = safe_orchestrator.prepare(brief=safe_brief)
    _persist_bootstrap(safe_services.root, prepared)
    with pytest.raises(WorkflowOrchestratorError) as unsafe:
        safe_orchestrator.plan_accepted(_accepted_review(prepared), brief=safe_brief)
    assert unsafe.value.code == "STAGE_PLAN_PATH_INVALID"
