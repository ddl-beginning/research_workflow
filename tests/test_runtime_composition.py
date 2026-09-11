from __future__ import annotations

import json
from pathlib import Path

from src.executor import ExecutionRequest, ExecutionResult, ExecutorDescriptor
from src.runtime_composition import (
    RuntimeComposition,
    build_runtime_composition,
    doctor_runtime_composition,
)
from src.stage_controller import StageController
from src.workflow_mcp import WorkflowMCPServer


def _contract(root: Path) -> dict[str, object]:
    return {
        "schema_version": "stage_contract.v1",
        "project_id": "composition-fixture",
        "repository_root": str(root),
        "stage_id": "stage-composition",
        "stage_name": "composition fixture",
        "project_goal": "prove portable runtime assembly",
        "stage_goal": "record one provider-neutral result",
        "user_visible_goal": "show a bounded composition",
        "inputs": ["tests/fixture.py"],
        "protected_paths": [".git", "production"],
        "allowed_paths": ["tests", ".research"],
        "acceptance_description": "the configured chain accepts one result",
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
    )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        return ExecutionResult(
            provider_id=self.descriptor.provider_id,
            result={
                "schema_version": "codex_result.v1",
                "plan_id": request.plan_id or "composition-plan",
                "stage_id": request.stage_id,
                "task_id": request.task_id or "composition-task",
                "iteration_index": request.iteration_index,
                "status": "SUCCEEDED",
                "summary": "composition fixture evidence",
                "changed_files": ["tests/fixture.py"],
                "tests": [{"name": "fixture", "status": "PASS"}],
                "measurements": {"fixture": {"passed": True}},
                "evidence_refs": ["fixture://composition"],
                "review_artifacts": [],
                "problems_discovered": [],
                "abstraction_layer": "composition-fixture",
                "stage_ready": False,
                "user_visible_failure": False,
                "human_gate_required": False,
                "decision_reason": "bounded fixture evidence",
                "stop_reason": "fixture_complete",
                "baseline_digest": request.baseline_digest,
            },
            evidence={"provider": self.descriptor.provider_id},
        )


def _write_fixture(root: Path) -> Path:
    contract_path = root / ".research" / "stages" / "stage-composition" / "contract.json"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(json.dumps(_contract(root), indent=2), encoding="utf-8")
    state_path = root / ".research" / "stage-state.json"
    controller = StageController(_contract(root), state_path=state_path)
    controller.start_stage(actor="fixture", rationale="composition test")
    return state_path


def test_doctor_reports_structured_missing_components_without_machine_defaults(tmp_path):
    diagnostic = doctor_runtime_composition(tmp_path, provider_factory=lambda *_args: (_Provider(),))

    assert diagnostic.ready is False
    assert any(item["code"] == "STAGE_STATE_OR_CONTRACT_MISSING" for item in diagnostic.missing)
    assert all("D:\\work\\research_tools" not in str(item) for item in diagnostic.missing)


def test_build_composition_from_relative_config_and_provider_seam(tmp_path):
    state_path = _write_fixture(tmp_path)
    config_path = tmp_path / ".research" / "runtime-composition.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "runtime_composition.v1",
                "stage": {
                    "state_path": state_path.relative_to(tmp_path).as_posix(),
                    "receipt_root": ".research/integration",
                },
                "executor": {
                    "providers": ["fixture-provider"],
                    "preferred_provider": "fixture-provider",
                    "preferred_model": "luna",
                    "fallback_model": None,
                    "auth_mode": "chatgpt",
                    "timeout_seconds": 30,
                    "artifact_dir": None,
                },
                "bridge": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )

    composition = build_runtime_composition(
        tmp_path,
        config_path=config_path,
        provider_factory=lambda _config, _root: (_Provider(),),
    )

    assert isinstance(composition, RuntimeComposition)
    assert composition.runner.repository_root == tmp_path.resolve()
    assert composition.stage_integration.bridge_runner is None
    result = composition.runner.run(
        {
            "action_map": {"validated": True, "execute": True, "actions": {"run": {}}},
            "required_capabilities": ["coding", "execution_evidence"],
            "objective": "record the fixture result",
        }
    )
    assert result["provider_id"] == "fixture-provider"
    assert result["runner_outcome"]["validated"] is True
    assert composition.controller.state["iteration_index"] == 1


def test_mcp_default_path_accepts_a_composed_runner_without_host_core_injection(tmp_path):
    _write_fixture(tmp_path)
    composition = build_runtime_composition(
        tmp_path,
        config=__import__("src.runtime_composition", fromlist=["RuntimeCompositionConfig"]).RuntimeCompositionConfig(
            stage_state_path=".research/stage-state.json",
            providers=("fixture-provider",),
            preferred_provider="fixture-provider",
        ),
        provider_factory=lambda _config, _root: (_Provider(),),
    )
    server = WorkflowMCPServer(composition_factory=lambda _workspace: composition)

    resolved = server._configured_core_runner({"workspace": str(tmp_path)})

    assert resolved is composition.runner
