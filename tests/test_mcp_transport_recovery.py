from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Mapping

from src.mcp_supervisor import HEALTH_PROBE_ID, MCPTransportSupervisor, WorkerResult
from src.workflow_v2_controller import StageController


def _request(tool: str, arguments: Mapping[str, Any], identifier: str = "request-1") -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": identifier,
            "method": "tools/call",
            "params": {"name": tool, "arguments": dict(arguments)},
        },
        ensure_ascii=False,
    ) + "\n"


def _response(identifier: Any, result: Mapping[str, Any] | None = None) -> WorkerResult:
    return WorkerResult(
        0,
        stdout=json.dumps(
            {"jsonrpc": "2.0", "id": identifier, "result": dict(result or {"ok": True})},
            ensure_ascii=False,
        ) + "\n",
    )


def _structured(output: io.StringIO) -> dict[str, Any]:
    wire = json.loads(output.getvalue())
    return wire["result"]["structuredContent"]


def _stage(stage_id: str = "stage-transport-12345678") -> dict[str, Any]:
    return {
        "schema_version": "stage.v2",
        "stage_id": stage_id,
        "workspace_id": "workspace-12345678",
        "project_id": "project-v2",
        "objective_fingerprint": "objective-12345678",
        "target_identity": "target/project",
        "required_capabilities": ["python"],
        "purpose": "DOMAIN",
        "repair_depth": 0,
        "baseline_digest": "baseline-12345678",
        "status": "PLANNED",
        "budgets": {
            "max_iterations": 2,
            "max_attempts_per_iteration": 2,
            "max_attempts_total": 3,
            "max_revalidation_ops": 2,
            "max_validator_revisions": 2,
            "max_dependency_nodes": 4,
            "max_dependency_depth": 2,
            "max_descendant_attempts": 4,
        },
        "owner_stage_id": None,
        "current_iteration_id": None,
        "current_assessment_id": None,
        "predecessor_stage_id": None,
        "semantic_baseline_diff_digest": None,
    }


def test_t1_normal_request_is_forwarded_once() -> None:
    calls: list[dict[str, Any]] = []

    def invoke(line: str) -> WorkerResult:
        message = json.loads(line)
        calls.append(message)
        return _response(message["id"])

    output = io.StringIO()
    MCPTransportSupervisor(invoke=invoke).serve_stdio(
        io.StringIO(_request("workflow_status", {"workspace": "fixture"})), output
    )

    assert len(calls) == 1
    assert json.loads(output.getvalue())["id"] == "request-1"


def test_t2_worker_close_before_delivery_restarts_health_checks_and_replays() -> None:
    calls: list[dict[str, Any]] = []
    business_calls = 0

    def invoke(line: str) -> WorkerResult:
        nonlocal business_calls
        message = json.loads(line)
        calls.append(message)
        if message.get("id") == HEALTH_PROBE_ID:
            return _response(HEALTH_PROBE_ID)
        business_calls += 1
        return WorkerResult(127, started=False) if business_calls == 1 else _response(message["id"])

    output = io.StringIO()
    MCPTransportSupervisor(invoke=invoke).serve_stdio(
        io.StringIO(_request("workflow_status", {"workspace": "fixture"})), output
    )

    assert [item["id"] for item in calls] == ["request-1", HEALTH_PROBE_ID, "request-1"]
    assert json.loads(output.getvalue())["id"] == "request-1"


def test_t3_maybe_delivered_command_replay_is_controller_idempotent(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    controller = StageController(workspace_id="workspace-12345678", state_path=journal)
    controller.initialize()
    stage = _stage()
    command_id = "command-transport-exact-once-12345678"
    request = _request(
        "workflow_run",
        {
            "request": {
                "operation": "COMMAND",
                "command": "REGISTER_STAGE",
                "subject_id": stage["stage_id"],
                "command_id": command_id,
                "payload": {"stage": stage},
            }
        },
    )
    calls: list[dict[str, Any]] = []

    def invoke(line: str) -> WorkerResult:
        message = json.loads(line)
        calls.append(message)
        if message.get("id") == HEALTH_PROBE_ID:
            return _response(HEALTH_PROBE_ID)
        operation = message["params"]["arguments"]["request"]
        if len([item for item in calls if item.get("id") == "request-1"]) == 1:
            controller.dispatch(
                "REGISTER_STAGE",
                subject_id=operation["subject_id"],
                payload=operation["payload"],
                command_id=operation["command_id"],
            )
            return WorkerResult(1)
        reloaded = StageController.from_state(journal)
        receipt = reloaded.dispatch(
            "REGISTER_STAGE",
            subject_id=operation["subject_id"],
            payload=operation["payload"],
            command_id=operation["command_id"],
        )
        return _response(message["id"], receipt)

    output = io.StringIO()
    MCPTransportSupervisor(invoke=invoke).serve_stdio(io.StringIO(request), output)
    persisted = StageController.from_state(journal)

    assert [item["id"] for item in calls] == ["request-1", HEALTH_PROBE_ID, "request-1"]
    assert persisted.revision == 1
    assert len(persisted.state["events"]) == 1
    assert persisted.state["stages"][stage["stage_id"]]["status"] == "PLANNED"
    assert json.loads(output.getvalue())["result"]["command"]["command_id"] == command_id


def test_t4_stale_worker_with_log_only_output_is_recovered() -> None:
    calls: list[dict[str, Any]] = []

    def invoke(line: str) -> WorkerResult:
        message = json.loads(line)
        calls.append(message)
        if message.get("id") == HEALTH_PROBE_ID:
            return _response(HEALTH_PROBE_ID)
        if len(calls) == 1:
            return WorkerResult(0, stdout="stale worker diagnostic\n")
        return _response(message["id"])

    output = io.StringIO()
    MCPTransportSupervisor(invoke=invoke).serve_stdio(
        io.StringIO(json.dumps({"jsonrpc": "2.0", "id": "list-1", "method": "tools/list", "params": {}}) + "\n"),
        output,
    )

    assert [item["id"] for item in calls] == ["list-1", HEALTH_PROBE_ID, "list-1"]
    assert json.loads(output.getvalue())["id"] == "list-1"


def test_t5_health_probe_is_single_and_recovery_attempt_is_bounded() -> None:
    calls: list[dict[str, Any]] = []

    def invoke(line: str) -> WorkerResult:
        message = json.loads(line)
        calls.append(message)
        if message.get("id") == HEALTH_PROBE_ID:
            return _response(HEALTH_PROBE_ID)
        return WorkerResult(1)

    output = io.StringIO()
    MCPTransportSupervisor(invoke=invoke).serve_stdio(
        io.StringIO(_request("workflow_status", {"workspace": "fixture"})), output
    )

    details = _structured(output)
    assert [item["id"] for item in calls] == ["request-1", HEALTH_PROBE_ID, "request-1"]
    assert details["error"]["code"] == "TRANSIENT_TRANSPORT_RECOVERY_EXHAUSTED"
    assert details["details"]["recovery_attempts"] == 1


def test_t6_reload_resume_does_not_mutate_canonical_journal(tmp_path: Path) -> None:
    journal = tmp_path / "journal.json"
    controller = StageController(workspace_id="workspace-transport-12345678", state_path=journal)
    controller.initialize()
    before = journal.read_bytes()
    calls: list[dict[str, Any]] = []

    def invoke(line: str) -> WorkerResult:
        message = json.loads(line)
        calls.append(message)
        reloaded = StageController.from_state(journal)
        if message.get("id") == HEALTH_PROBE_ID:
            return _response(HEALTH_PROBE_ID)
        projection = reloaded.resume_projection()
        if len(calls) == 1:
            return WorkerResult(1)
        return _response(message["id"], projection)

    output = io.StringIO()
    MCPTransportSupervisor(invoke=invoke).serve_stdio(
        io.StringIO(_request("workflow_resume", {"workspace": str(tmp_path)})), output
    )

    assert [item["id"] for item in calls] == ["request-1", HEALTH_PROBE_ID, "request-1"]
    assert journal.read_bytes() == before
    assert json.loads(output.getvalue())["result"]["stage"]["next_action"] == "REGISTER_STAGE"


def test_t7_persistent_worker_and_health_failure_returns_blocked_once() -> None:
    calls: list[dict[str, Any]] = []

    def invoke(line: str) -> WorkerResult:
        calls.append(json.loads(line))
        return WorkerResult(1)

    output = io.StringIO()
    MCPTransportSupervisor(invoke=invoke).serve_stdio(
        io.StringIO(_request("workflow_status", {"workspace": "fixture"})), output
    )

    details = _structured(output)
    assert [item["id"] for item in calls] == ["request-1", HEALTH_PROBE_ID]
    assert details["error"]["code"] == "TRANSIENT_TRANSPORT_RECOVERY_EXHAUSTED"
    assert details["next_action"] == "BLOCKED"


def test_t8_workflow_start_reports_unresolved_effect_without_replay() -> None:
    calls: list[dict[str, Any]] = []

    def invoke(line: str) -> WorkerResult:
        message = json.loads(line)
        calls.append(message)
        if message.get("id") == HEALTH_PROBE_ID:
            return _response(HEALTH_PROBE_ID)
        return WorkerResult(1)

    output = io.StringIO()
    MCPTransportSupervisor(invoke=invoke).serve_stdio(
        io.StringIO(
            _request(
                "workflow_start",
                {
                    "workspace": "fixture",
                    "mode": "CODEX_REQUIREMENTS_INTERVIEW",
                    "rough_requirement": "bounded transport regression",
                },
            )
        ),
        output,
    )

    details = _structured(output)
    assert [item["id"] for item in calls] == ["request-1", HEALTH_PROBE_ID]
    assert details["error"]["code"] == "MCP_TRANSPORT_EFFECT_UNRESOLVED"
    assert details["details"]["health_probe"] == "PASS"
