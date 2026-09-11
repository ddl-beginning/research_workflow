from __future__ import annotations

from pathlib import Path

import pytest

from src.executor import ExecutionRequest, ExecutionResult, ExecutorDescriptor
from src.stage_controller import StageController
from src.stage_integration import StageIntegrationAdapter
from src.workflow_core_adapter import (
    CoreV1RunnerAdapter,
    CoreV1RunnerError,
    build_core_v1_runner_adapter,
)
from src.workflow_mcp import WorkflowMCPServer


def _contract(root: Path) -> dict[str, object]:
    return {
        "schema_version": "stage_contract.v1",
        "project_id": "workflow-adapter-fixture",
        "repository_root": str(root),
        "stage_id": "stage-adapter",
        "stage_name": "workflow adapter fixture",
        "project_goal": "prove the configured Core V1 seam",
        "stage_goal": "record provider-neutral bounded evidence",
        "user_visible_goal": "show a bounded execution result",
        "inputs": ["tests/fixture.py"],
        "protected_paths": [".git", "production"],
        "allowed_paths": ["tests", ".research"],
        "acceptance_description": "the controller accepts one normalized result",
        "required_checks": ["unit"],
        "review_artifact_requirements": ["summary"],
        "baseline": {"metric": 1},
        "status": "PLANNED",
    }


class _Provider:
    descriptor = ExecutorDescriptor(
        provider_id="fixture-provider",
        capabilities=("coding", "execution_evidence"),
        execution_modes=("PRIMARY",),
        reliability=1.0,
        cost=0.0,
    )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(
            provider_id=self.descriptor.provider_id,
            result={
                "schema_version": "codex_result.v1",
                "plan_id": request.plan_id or "fixture-plan",
                "stage_id": request.stage_id,
                "task_id": request.task_id or "fixture-task",
                "iteration_index": request.iteration_index,
                "status": "SUCCEEDED",
                "summary": "fixture provider evidence",
                "changed_files": ["tests/fixture.py"],
                "tests": [{"name": "fixture", "status": "PASS"}],
                "measurements": {"fixture": {"passed": True}},
                "evidence_refs": ["fixture://result"],
                "review_artifacts": [],
                "problems_discovered": [],
                "abstraction_layer": "fixture-provider",
                "stage_ready": False,
                "user_visible_failure": False,
                "human_gate_required": False,
                "decision_reason": "fixture evidence is submitted to Core V1",
                "stop_reason": "fixture_complete",
                "baseline_digest": request.baseline_digest,
            },
            evidence={"provider": self.descriptor.provider_id},
        )


def _adapter(root: Path) -> tuple[StageController, CoreV1RunnerAdapter]:
    controller = StageController(_contract(root))
    controller.start_stage()
    integration = StageIntegrationAdapter(controller, repository_root=root)
    return controller, CoreV1RunnerAdapter(stage_integration=integration, providers=[_Provider()])


def test_core_v1_adapter_delegates_provider_through_stage_integration(tmp_path):
    controller, adapter = _adapter(tmp_path)
    result = adapter.run(
        {
            "action_map": {"validated": True, "execute": True, "actions": {"run": {}}},
            "required_capabilities": ["coding", "execution_evidence"],
        }
    )
    assert result["runner_outcome"]["outcome"] == "CONTINUE_WORKFLOW"
    assert result["runner_outcome"]["validated"] is True
    assert result["provider_id"] == "fixture-provider"
    assert controller.state["status"] == "ACTIVE"
    assert controller.state["iteration_index"] == 1


def test_core_v1_adapter_requires_validated_action_map_before_core_mutation(tmp_path):
    controller, adapter = _adapter(tmp_path)
    with pytest.raises(CoreV1RunnerError, match="validated and enabled ActionMap"):
        adapter.run({"action_map": {"validated": False, "execute": False}})
    assert controller.state["iteration_index"] == 0
    assert controller.state["executor_requests"] == []


def test_core_v1_factory_is_explicit_and_fail_closed_without_core_configuration():
    with pytest.raises(CoreV1RunnerError) as missing_integration:
        build_core_v1_runner_adapter(stage_integration=None, providers=[])
    assert missing_integration.value.code == "RUNNER_NOT_CONFIGURED"

    with pytest.raises(CoreV1RunnerError) as missing_providers:
        build_core_v1_runner_adapter(stage_integration=None, providers=None)
    assert missing_providers.value.code == "RUNNER_NOT_CONFIGURED"


def test_default_mcp_reports_missing_core_runner_as_bounded_blocked_error(tmp_path):
    server = WorkflowMCPServer()
    brief = {
        "project_goal": "prove default server configuration is explicit",
        "observable_outcome": "missing runner is reported without execution",
        "scope": ["local workspace"],
        "non_goals": ["browser automation"],
        "constraints": ["do not bypass Core"],
        "available_assets": ["existing Core V1"],
        "acceptance": ["bounded error"],
        "human_preferences": ["fail closed"],
    }
    start = server.call_tool("workflow_start", {"workspace": str(tmp_path), "brief": brief})
    assert start["next_action"] == "REQUEST_BRIEF_APPROVAL"
    approved = server.call_tool("workflow_answer", {"workspace": str(tmp_path), "mode": "APPROVE"})
    assert approved["next_action"] == "RUN_WORKFLOW"
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "missing-runner",
            "method": "tools/call",
            "params": {
                "name": "workflow_run",
                "arguments": {
                    "workspace": str(tmp_path),
                    "request": {"action_map": {"validated": True, "execute": True}},
                },
            },
        }
    )
    assert response["result"]["isError"] is True
    content = response["result"]["structuredContent"]
    assert content["error"]["code"] == "RUNNER_NOT_CONFIGURED"
    assert content["next_action"] == "BLOCKED"
