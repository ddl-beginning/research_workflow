from __future__ import annotations

from dataclasses import replace

import pytest

from src.codexpro_adapter import CodexProHandoffExecutor
from src.contracts import ContractValidationError, validate_against_schema
from src.executor import (
    ExecutionRequest,
    ExecutionResult,
    ExecutorDescriptor,
    discover_executors,
    select_executor,
)
from src.stage_controller import StageController


def _contract() -> dict:
    return {
        "schema_version": "stage_contract.v1",
        "project_id": "portable-fixture",
        "repository_root": "configured-at-runtime",
        "stage_id": "stage-executor",
        "stage_name": "executor seam fixture",
        "project_goal": "prove provider replacement",
        "stage_goal": "return bounded execution evidence",
        "user_visible_goal": "show provider-neutral evidence",
        "inputs": ["fixtures/input.txt"],
        "protected_paths": [".git", "production"],
        "allowed_paths": ["tests", ".research"],
        "acceptance_description": "the controller accepts a normalized result",
        "required_checks": ["unit"],
        "review_artifact_requirements": ["summary"],
        "baseline": {"metric": 1},
        "status": "PLANNED",
    }


class FixtureExecutor:
    def __init__(self, provider_id: str = "native-fixture", reliability: float = 0.9, priority: int = 0) -> None:
        self._descriptor = ExecutorDescriptor(
            provider_id=provider_id,
            capabilities=("coding", "execution_evidence"),
            execution_modes=("PRIMARY",),
            reliability=reliability,
            cost=0.25,
            metadata={"transport": "fixture"},
            priority=priority,
        )

    @property
    def descriptor(self) -> ExecutorDescriptor:
        return self._descriptor

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        result = {
            "schema_version": "codex_result.v1",
            "plan_id": request.plan_id or "fixture-plan",
            "stage_id": request.stage_id,
            "task_id": request.task_id or "fixture-task",
            "iteration_index": request.iteration_index,
            "status": "SUCCEEDED",
            "summary": "fixture executor returned bounded evidence",
            "changed_files": ["tests/fixture.py"],
            "tests": [{"name": "fixture", "status": "PASS"}],
            "measurements": {"fixture": {"passed": True}},
            "evidence_refs": ["fixture://execution"],
            "review_artifacts": [],
            "problems_discovered": [],
            "abstraction_layer": "fixture-executor",
            "stage_ready": False,
            "user_visible_failure": False,
            "human_gate_required": False,
            "decision_reason": "fixture evidence awaits controller governance",
            "stop_reason": "fixture_complete",
        }
        if request.baseline_digest:
            result["baseline_digest"] = request.baseline_digest
        return ExecutionResult(
            provider_id=self.descriptor.provider_id,
            result=result,
            evidence={"provider": self.descriptor.provider_id, "kind": "fixture"},
        )


def _request(controller: StageController, *, preferred_provider: str | None = None) -> ExecutionRequest:
    raw = controller.request_executor()["request"]
    return ExecutionRequest.from_stage_request(
        raw,
        plan_id="plan-executor-fixture",
        task_id="task-executor-fixture",
        required_capabilities=("coding", "execution_evidence"),
        preferred_provider=preferred_provider,
        metadata={
            "expected_plan_hash": "plan-hash-fixture",
            "abstraction_layer": "codexpro-handoff",
        },
    )


def _codexpro_provider() -> CodexProHandoffExecutor:
    return CodexProHandoffExecutor(
        {
            "state": "completed",
            "plan_hash": "plan-hash-fixture",
            "iteration": 1,
            "exit_code": 0,
            "timed_out": False,
            "changed_files": ["tests/fixture.py"],
            "tests": [{"name": "codexpro-style fixture", "status": "PASS"}],
        }
    )


def test_discovery_and_selection_are_bounded_and_preference_aware():
    native = FixtureExecutor()
    codexpro = _codexpro_provider()
    discovered = discover_executors([native, codexpro])
    assert [item.provider_id for item in discovered] == ["native-fixture", "codexpro-handoff"]

    controller = StageController(_contract())
    controller.start_stage()
    request = _request(controller)
    selection = select_executor(request, [native, codexpro])
    assert selection.provider_id == "native-fixture"
    assert selection.review_recommended is True
    assert set(selection.bounded_view()) == {
        "selected_provider", "candidates", "reason", "review_recommended"
    }

    preferred_request = replace(request, preferred_provider="codexpro-handoff")
    preferred = select_executor(preferred_request, [native, codexpro])
    assert preferred.provider_id == "codexpro-handoff"
    assert preferred.review_recommended is False


def test_selection_fails_closed_for_unavailable_or_missing_capability():
    controller = StageController(_contract())
    controller.start_stage()
    request = _request(controller)
    unavailable = FixtureExecutor("unavailable", reliability=1.0)
    unavailable._descriptor = ExecutorDescriptor(
        provider_id="unavailable",
        capabilities=("coding", "execution_evidence"),
        available=False,
        reliability=1.0,
        cost=0.0,
    )
    with pytest.raises(ContractValidationError, match="no available executor"):
        select_executor(request, [unavailable])


def test_configured_priority_is_the_deterministic_ordering_source_before_reliability_and_cost():
    controller = StageController(_contract())
    controller.start_stage()
    request = _request(controller)
    lower = FixtureExecutor("priority-lower", reliability=1.0, priority=1)
    higher = FixtureExecutor("priority-higher", reliability=0.1, priority=7)
    selection = select_executor(request, [lower, higher])
    assert selection.provider_id == "priority-higher"
    by_id = {item["provider_id"]: item for item in selection.candidates}
    assert by_id["priority-higher"]["priority"] == 7
    assert "configured priority" in selection.reason


def test_execution_request_requires_a_validated_enabled_action_map_when_supplied():
    controller = StageController(_contract())
    controller.start_stage()
    raw = controller.request_executor()["request"]
    with pytest.raises(ContractValidationError, match="validated"):
        ExecutionRequest.from_stage_request(raw, action_map={"validated": False, "execute": False})
    checked = ExecutionRequest.from_stage_request(
        raw,
        action_map={"validated": True, "execute": True, "actions": {"run_local_checks": {}}},
    )
    assert checked.action_map["validated"] is True


def test_codexpro_provider_normalizes_execution_request_and_keeps_controller_in_charge():
    controller = StageController(_contract())
    controller.start_stage()
    request = _request(controller, preferred_provider="codexpro-handoff")
    provider = _codexpro_provider()
    result = provider.execute(request)
    validate_against_schema(result.to_stage_result(), "codex_result")
    assert result.provider_id == "codexpro-handoff"
    assert result.result["status"] == "SUCCEEDED"
    assert result.result["stage_ready"] is False
    assert result.result["human_gate_required"] is False
    assert controller.state["iteration_index"] == 0

    recorded = controller.record_executor_result(
        result.to_stage_result(),
        request_id=request.metadata["request_id"],
    )
    assert recorded["decision"] == "CONTINUE"
    assert controller.state["iteration_index"] == 1
    assert controller.state["status"] == "ACTIVE"


def test_provider_availability_failure_keeps_stage_active_without_replan():
    controller = StageController(_contract())
    controller.start_stage()
    request = controller.request_executor()["request"]
    result = {
        "schema_version": "codex_result.v1",
        "plan_id": "plan-provider-failure",
        "stage_id": request["stage_id"],
        "task_id": request["request_id"],
        "iteration_index": request["iteration_index"],
        "status": "ERROR",
        "summary": "provider unavailable",
        "changed_files": [],
        "tests": [],
        "measurements": {"provider": {"available": False}},
        "evidence_refs": ["provider://status"],
        "review_artifacts": [],
        "problems_discovered": ["MODEL_AT_CAPACITY"],
        "abstraction_layer": "provider-neutral",
        "stage_ready": False,
        "user_visible_failure": True,
        "human_gate_required": False,
        "execution_failure": {
            "kind": "PROVIDER_FAILURE",
            "code": "MODEL_AT_CAPACITY",
            "retryable": True,
            "reason": "bounded provider status",
        },
    }
    recorded = controller.record_executor_result(result, request_id=request["request_id"])
    assert recorded["decision"] == "EXECUTION_PROVIDER_FAILURE"
    assert recorded["execution_failure"]["code"] == "MODEL_AT_CAPACITY"
    assert recorded["stage"]["status"] == "ACTIVE"
    assert controller.state["iteration_index"] == 0
    assert controller.state["active_stage_id"] == request["stage_id"]


def test_evidence_failure_without_provider_marker_keeps_existing_replan_semantics():
    controller = StageController(_contract())
    controller.start_stage()
    request = controller.request_executor()["request"]
    recorded = controller.record_executor_result(
        {
            "status": "FAILED",
            "iteration_index": request["iteration_index"],
            "summary": "required test failed",
        },
        request_id=request["request_id"],
    )
    assert recorded["decision"] == "REPLAN"
    assert recorded["stage"]["status"] == "ACTIVE"
    assert controller.state["iteration_index"] == 1


def test_provider_replacement_preserves_stage_lifecycle_and_reaches_bounded_gpt_review():
    native = FixtureExecutor()
    codexpro = _codexpro_provider()
    outcomes = []
    for provider in (native, codexpro):
        controller = StageController(_contract())
        controller.start_stage()
        request = _request(controller, preferred_provider=provider.descriptor.provider_id)
        selected = select_executor(request, [native, codexpro])
        assert selected.provider_id == provider.descriptor.provider_id
        result = provider.execute(request)
        recorded = controller.record_executor_result(
            result.to_stage_result(),
            request_id=request.metadata["request_id"],
        )
        consultation = controller.request_consultation(
            mode="NORMAL",
            evidence_digest=f"{provider.descriptor.provider_id}-evidence",
            conversation_id="fixture-conversation",
        )
        outcomes.append((recorded["decision"], recorded["stage"]["status"], consultation["request"]["mode"]))

    # The provider changes, but the controller lifecycle and GPT-review seam do
    # not.  No provider is given a controller reference or a state mutation API.
    assert outcomes == [("CONTINUE", "ACTIVE", "NORMAL"), ("CONTINUE", "ACTIVE", "NORMAL")]
