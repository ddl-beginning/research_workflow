from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

import src.product_workflow_runtime as product_runtime
from src.openai_codex_executor import RuntimeSnapshot
from src.product_workflow_runtime import ProductWorkflowRuntime
from src.workflow_mcp import WorkflowMCPServer
from src.workflow_runtime import WorkflowRuntimeError
from src.workflow_v2_controller import StageController
from src.contract_handshake import stage_digest, stage_identity_digest

from tests.test_product_workflow_runtime import (
    _approved_runtime,
    _planning_request,
    _stage,
    _stub_bridge_envelope,
    _write_machine_config,
)


def _native_runtime() -> RuntimeSnapshot:
    return RuntimeSnapshot(
        executable="python",
        version="fixture-codex",
        auth_mode="chatgpt",
        authenticated=True,
        models=({"id": "gpt-5.6-luna"},),
        sdk_available=False,
        available=True,
        reason="offline fixture",
    )


def _install_native_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        product_runtime.OpenAICodexExecutor,
        "discover_runtime",
        lambda _provider: _native_runtime(),
    )


def test_plan_start_execution_transition_keeps_canonical_stage_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_native_runtime(monkeypatch)
    root = tmp_path / "propagation"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    runtime = _approved_runtime(root, config_path)
    monkeypatch.setattr(product_runtime, "subprocess_bridge_runner", lambda *_args, **_kwargs: _stub_bridge_envelope())

    planned = runtime.run(_planning_request(runtime))
    plan_trace = planned["contract_propagation"]
    canonical = runtime.controller.resolve_canonical_stage("stage-product-planning")
    assert plan_trace["canonical_stage_digest"] == stage_digest(canonical)
    assert plan_trace["canonical_stage_identity_digest"] == stage_identity_digest(canonical)
    assert plan_trace["client_request"]["request_stage_exists"] is True
    assert plan_trace["transport_request"]["request_stage_schema_version"] == "stage.v2"
    assert plan_trace["supervisor_received_request"]["request_stage_schema_version"] == "stage.v2"

    started = runtime.run(
        {
            "operation": "COMMAND",
            "command": "START",
            "subject_id": canonical["stage_id"],
            "stage": canonical,
            "payload": {},
            "command_id": "command-propagation-start-12345678",
        }
    )
    start_trace = started["contract_propagation"]
    active = runtime.controller.resolve_canonical_stage(canonical["stage_id"])
    assert started["command_result"]["stage"]["status"] == "ACTIVE"
    assert start_trace["canonical_stage_digest"] == stage_digest(canonical)
    assert start_trace["canonical_stage_identity_digest"] == stage_identity_digest(active)

    requested = runtime.run(
        {
            "operation": "COMMAND",
            "command": "REQUEST_EXECUTION",
            "subject_id": active["stage_id"],
            "stage": active,
            "payload": {
                "request": {"objective": "bounded fixture execution"},
                "provenance": {"provider": "fixture", "engine_digest": "engine-propagation-12345678"},
                "purpose": "DOMAIN",
            },
            "command_id": "command-propagation-request-12345678",
        }
    )
    assert requested["command_result"]["attempt"]["stage_id"] == canonical["stage_id"]
    assert requested["contract_propagation"]["canonical_stage_identity_digest"] == start_trace["canonical_stage_identity_digest"]


def test_plan_missing_stage_is_classified_with_four_layer_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_native_runtime(monkeypatch)
    root = tmp_path / "plan-omitted"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    runtime = _approved_runtime(root, config_path)
    canonical = _stage(runtime)
    runtime.controller.register_stage(canonical, command_id="command-plan-omitted-register-12345678")

    with pytest.raises(WorkflowRuntimeError) as caught:
        runtime.run({"operation": "PLAN_STAGE", "stage_id": canonical["stage_id"]})

    assert caught.value.code == "PLAN_STAGE_STAGE_OBJECT_OMITTED"
    trace = caught.value.details["contract_trace"]
    assert trace["canonical_stage"] is None
    for layer in ("client_request", "transport_request", "supervisor_received_request"):
        assert trace[layer]["request_stage_exists"] is False
        assert trace[layer]["request_stage_schema_version"] is None


def test_legacy_stage_contract_is_rejected_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_native_runtime(monkeypatch)
    root = tmp_path / "legacy-stage"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    runtime = _approved_runtime(root, config_path)
    canonical = _stage(runtime)
    runtime.controller.register_stage(canonical, command_id="command-legacy-stage-register-12345678")
    legacy = copy.deepcopy(canonical)
    legacy["schema_version"] = "stage_contract.v1"

    with pytest.raises(WorkflowRuntimeError) as caught:
        runtime.run(
            {
                "operation": "COMMAND",
                "command": "START",
                "subject_id": canonical["stage_id"],
                "stage": legacy,
                "payload": {},
                "command_id": "command-legacy-stage-start-12345678",
            }
        )

    assert caught.value.code == "CONTRACT_VERSION_MISMATCH"


def test_mcp_self_repair_injects_canonical_stage_and_preserves_business_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_native_runtime(monkeypatch)
    root = tmp_path / "self-repair"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    runtime = _approved_runtime(root, config_path)
    canonical = _stage(runtime)
    runtime.controller.register_stage(canonical, command_id="command-self-repair-register-12345678")
    before = runtime.repair_context()

    calls: list[str] = []

    def maintenance(context: dict[str, Any], error: BaseException) -> dict[str, Any]:
        calls.append(str(getattr(error, "code", "")))
        assert context["project_id"] == before["project_id"]
        return {"engine_scope_only": True, "doctor_handshake": "PASS", "minimal_e2e": "PASS"}

    server = WorkflowMCPServer(default_workspace=str(root), maintenance_capability=maintenance)
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "self-repair-start",
            "method": "tools/call",
            "params": {
                "name": "workflow_run",
                "arguments": {
                    "workspace": str(root),
                    "request": {
                        "operation": "COMMAND",
                        "command": "START",
                        "subject_id": canonical["stage_id"],
                        "payload": {},
                        "command_id": "command-self-repair-start-12345678",
                    },
                },
            },
        }
    )

    assert response is not None
    payload = response["result"]["structuredContent"]
    assert response["result"]["isError"] is False
    assert calls == ["STAGE_SCOPED_ACTION_MUST_USE_CANONICAL_STAGE"]
    assert payload["self_repair"]["status"] == "PASS"
    assert payload["self_repair"]["human_intervention_count"] == 0
    assert payload["self_repair"]["repair_attempts"] == 1
    assert payload["self_repair"]["doctor_handshake"] == "PASS"
    assert payload["stage"]["stage_id"] == before["stage_id"]
    assert payload["stage"]["status"] == "ACTIVE"
    assert payload["project_id"] == before["project_id"]
    trace = payload["contract_propagation"]
    assert trace["transport_request"]["request_stage_exists"] is False
    assert trace["supervisor_received_request"]["request_stage_exists"] is True
    assert trace["supervisor_received_request"]["request_stage_schema_version"] == "stage.v2"
