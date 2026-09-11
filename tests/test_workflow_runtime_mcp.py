from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src.contracts import validate_against_schema
from src.workflow_mcp import TOOL_DEFINITIONS, WorkflowMCPServer
from src.workflow_runtime import (
    DEFAULT_CHECKPOINT_RELATIVE,
    WORKFLOW_CHECKPOINT_SCHEMA_VERSION,
    WorkflowRuntime,
    WorkflowRuntimeError,
)


def _complete_brief() -> dict[str, object]:
    return {
        "project_goal": "make the bounded local workflow observable",
        "observable_outcome": "a reviewer can verify one persisted result",
        "scope": ["local workspace"],
        "non_goals": ["browser automation"],
        "constraints": ["no secrets in artifacts"],
        "available_assets": ["existing Core V1 runner"],
        "acceptance": ["checkpoint and brief reload in a new process"],
        "human_preferences": ["ask at most one question"],
    }


def _approve(root: Path) -> WorkflowRuntime:
    root.mkdir(parents=True, exist_ok=True)
    runtime = WorkflowRuntime(root)
    result = runtime.start(brief=_complete_brief())
    assert result["question_count"] == 0
    assert result["brief_state"] == "WAITING_USER_APPROVAL"
    approved = runtime.answer(approve=True)
    assert approved["brief_state"] == "APPROVED"
    return runtime


def test_start_persists_atomic_brief_and_checkpoint_with_one_question(tmp_path):
    runtime = WorkflowRuntime(tmp_path)
    result = runtime.start(project_goal="make a local project outcome observable")
    assert result["question_count"] == 1
    assert result["question"]["id"] == "success_criteria"
    assert result["next_action"] == "ASK_REQUIREMENT"
    assert result["question_id"] == "success_criteria"
    assert result["next_tool"] == "workflow_answer"
    assert (tmp_path / ".research" / "PROJECT_BRIEF.json").is_file()
    assert (tmp_path / DEFAULT_CHECKPOINT_RELATIVE).is_file()
    checkpoint = json.loads((tmp_path / DEFAULT_CHECKPOINT_RELATIVE).read_text(encoding="utf-8"))
    validate_against_schema(result, "workflow_runtime_result.v1")
    validate_against_schema(checkpoint, "workflow_checkpoint.v1")
    assert checkpoint["schema_version"] == WORKFLOW_CHECKPOINT_SCHEMA_VERSION
    assert checkpoint["checkpoint_path"] == DEFAULT_CHECKPOINT_RELATIVE.as_posix()
    assert checkpoint["workspace_root"] == tmp_path.resolve().as_posix()
    assert "D:\\work" not in json.dumps(checkpoint)
    assert checkpoint["brief_digest"]
    assert checkpoint["next_action"] == "ASK_REQUIREMENT"


def test_answer_maps_runtime_vocabulary_and_approve_is_explicit(tmp_path):
    runtime = WorkflowRuntime(tmp_path)
    first = runtime.start()
    assert first["question_count"] == 1
    second = runtime.answer(answer="a visible project outcome", question_id="desired_outcome")
    assert second["question_count"] == 1
    assert second["question"]["id"] == "success_criteria"
    third = runtime.answer(answer={"acceptance": ["a local check passes"]}, question_id="success_criteria")
    assert third["brief_state"] == "WAITING_USER_APPROVAL"
    assert third["question_count"] == 0
    approved = runtime.answer(approve=True)
    assert approved["brief_state"] == "APPROVED"
    assert approved["requirements"]["observable_outcome"] == "a visible project outcome"
    assert approved["requirements"]["acceptance"] == ["a local check passes"]
    assert approved["next_action"] == "RUN_WORKFLOW"
    assert approved["next_tool"] == "workflow_run"


def test_answer_input_is_mutually_exclusive_and_mode_is_bounded(tmp_path):
    runtime = WorkflowRuntime(tmp_path)
    runtime.start()
    with pytest.raises(WorkflowRuntimeError) as combined:
        runtime.answer(answer="one", approve=True)
    assert combined.value.code == "INPUT_INVALID"
    with pytest.raises(WorkflowRuntimeError) as invalid_mode:
        runtime.answer(mode="RUN", answer="one")
    assert invalid_mode.value.code == "MODE_INVALID"
    with pytest.raises(WorkflowRuntimeError) as question_for_update:
        runtime.answer(mode="UPDATE", update={"desired_outcome": "x"}, question_id="desired_outcome")
    assert question_for_update.value.code == "INPUT_INVALID"


def test_checkpoint_brief_identity_digest_revision_state_and_path_fail_closed(tmp_path):
    runtime = _approve(tmp_path)
    checkpoint_path = tmp_path / DEFAULT_CHECKPOINT_RELATIVE
    original_checkpoint = checkpoint_path.read_text(encoding="utf-8")
    brief_path = tmp_path / ".research" / "PROJECT_BRIEF.json"
    original_brief = brief_path.read_text(encoding="utf-8")
    try:
        brief = json.loads(original_brief)
        brief["revision"] += 1
        brief_path.write_text(json.dumps(brief, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        with pytest.raises(WorkflowRuntimeError) as stale:
            runtime.status()
        assert stale.value.code == "CHECKPOINT_STALE"
        brief_path.write_text(original_brief, encoding="utf-8")
        checkpoint = json.loads(original_checkpoint)
        checkpoint["project_brief_path"] = "other/PROJECT_BRIEF.json"
        checkpoint_path.write_text(json.dumps(checkpoint, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        with pytest.raises(WorkflowRuntimeError) as bad_path:
            runtime.resume()
        assert bad_path.value.code == "CHECKPOINT_INVALID"
    finally:
        brief_path.write_text(original_brief, encoding="utf-8")
        checkpoint_path.write_text(original_checkpoint, encoding="utf-8")


def test_new_runtime_process_resumes_from_disk_only(tmp_path):
    original = _approve(tmp_path)
    before = (tmp_path / DEFAULT_CHECKPOINT_RELATIVE).read_bytes()
    resumed = WorkflowRuntime(tmp_path).resume()
    assert resumed["operation"] == "workflow_resume"
    assert resumed["resumed"] is True
    assert resumed["project_id"] == original.intake.project_id
    assert resumed["brief_state"] == "APPROVED"
    assert (tmp_path / DEFAULT_CHECKPOINT_RELATIVE).read_bytes() == before


def test_independent_os_process_resumes_from_persisted_checkpoint(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    start_script = (
        "import json,sys; "
        "from pathlib import Path; "
        "from src.workflow_runtime import WorkflowRuntime; "
        "brief=json.loads(sys.argv[2]); "
        "result=WorkflowRuntime(Path(sys.argv[1])).start(brief=brief); "
        "print(json.dumps(result, ensure_ascii=False, sort_keys=True))"
    )
    first = subprocess.run(
        [sys.executable, "-c", start_script, str(tmp_path), json.dumps(_complete_brief(), ensure_ascii=False)],
        cwd=str(root),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(first.stdout)["next_action"] == "REQUEST_BRIEF_APPROVAL"
    resume_script = (
        "import json,sys; "
        "from pathlib import Path; "
        "from src.workflow_runtime import WorkflowRuntime; "
        "print(json.dumps(WorkflowRuntime(Path(sys.argv[1])).resume(), sort_keys=True))"
    )
    second = subprocess.run(
        [sys.executable, "-c", resume_script, str(tmp_path)],
        cwd=str(root),
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    resumed = json.loads(second.stdout)
    assert resumed["resumed"] is True
    assert resumed["next_action"] == "REQUEST_BRIEF_APPROVAL"


def test_workspace_and_checkpoint_paths_are_bounded(tmp_path):
    outside = tmp_path.parent / "workflow-state-outside.json"
    with pytest.raises(WorkflowRuntimeError, match="inside workspace_root"):
        WorkflowRuntime(tmp_path, checkpoint_path=outside)
    with pytest.raises(WorkflowRuntimeError, match="existing directory"):
        WorkflowRuntime(tmp_path / "missing")


def test_workflow_run_delegates_injected_existing_runner_and_only_persists_summary(tmp_path):
    calls: list[dict[str, object]] = []

    def runner(request):
        calls.append(dict(request))
        return {
            "status": "SUCCEEDED",
            "provider_id": "fixture-runner",
            "decision": "CONTINUE",
            "runner_outcome": {
                "schema_version": "workflow_runner_outcome.v1",
                "outcome": "COMPLETE",
                "validated": True,
                "evidence_validated": True,
            },
        }

    runtime = _approve(tmp_path)
    runtime.runner = runner
    result = runtime.run({"stage_request": {"stage_id": "stage-a"}, "action_map": {"validated": True}})
    assert calls == [{"stage_request": {"stage_id": "stage-a"}, "action_map": {"validated": True}}]
    assert result["result"]["provider_id"] == "fixture-runner"
    assert result["checkpoint"]["phase"] == "COMPLETE"
    assert result["next_action"] == "COMPLETE"
    assert result["checkpoint"]["last_run"]["result_digest"]
    assert not (tmp_path / ".research" / "stage-state.json").exists()
    assert "result" not in json.loads((tmp_path / DEFAULT_CHECKPOINT_RELATIVE).read_text(encoding="utf-8")).get("last_run", {})


def test_raw_runner_status_cannot_complete_runtime_without_validated_outcome(tmp_path):
    runtime = _approve(tmp_path)
    runtime.runner = lambda request: {"status": "SUCCEEDED", "provider_id": "untrusted"}
    with pytest.raises(WorkflowRuntimeError) as failure:
        runtime.run({"action": "run_local_checks"})
    assert failure.value.code == "RUNNER_OUTCOME_INVALID"
    assert runtime.status()["phase"] == "READY"
    assert runtime.status()["next_action"] == "RUN_WORKFLOW"


def test_run_requires_approved_brief_and_configured_runner(tmp_path):
    runtime = WorkflowRuntime(tmp_path)
    runtime.start(project_goal="a goal")
    with pytest.raises(WorkflowRuntimeError, match="approved"):
        runtime.run({"action": "run_local_checks"})
    approved = _approve(tmp_path / "approved")
    with pytest.raises(WorkflowRuntimeError, match="injected existing Core V1 runner"):
        approved.run({"action": "run_local_checks"})


def test_mcp_initialize_tools_and_workflow_call_protocol(tmp_path):
    server = WorkflowMCPServer()
    initialized = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert initialized["result"]["capabilities"]["tools"] == {}
    instructions = initialized["result"]["instructions"]
    assert len(instructions) <= 512
    assert instructions.startswith("Use workflow_resume first.")
    assert "WORKFLOW_NOT_FOUND" in instructions
    assert "next_action" in instructions and "one requirement question" in instructions
    assert "DESIGN_ACCEPT" in instructions and "not PLANNED" in instructions
    assert "validated Stage evidence" in instructions
    assert "do not bypass core" in instructions.lower()
    listed = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    names = [item["name"] for item in listed["result"]["tools"]]
    assert names == ["workflow_start", "workflow_answer", "workflow_run", "workflow_status", "workflow_resume"]
    started = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "workflow_start",
                "arguments": {"workspace": str(tmp_path), "project_goal": "show a local result"},
            },
        }
    )
    assert started["result"]["isError"] is False
    assert started["result"]["structuredContent"]["question_count"] == 1
    answered = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "workflow_answer",
                "arguments": {
                    "workspace": str(tmp_path),
                    "mode": "ANSWER",
                    "question_id": "success_criteria",
                    "answer": "a local check passes",
                },
            },
        }
    )
    assert answered["result"]["isError"] is False
    assert answered["result"]["structuredContent"]["next_action"] == "REQUEST_BRIEF_APPROVAL"
    assert set(item["name"] for item in TOOL_DEFINITIONS) == set(names)


def test_mcp_stdio_protocol_does_not_write_logs_or_reply_to_notifications(tmp_path):
    server = WorkflowMCPServer()
    incoming = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}),
            json.dumps({"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": 1}}),
            json.dumps({"jsonrpc": "2.0", "method": "custom/unknown-notification", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": "tools-2", "method": "tools/list", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": "unknown-3", "method": "custom/unknown-request", "params": {}}),
        ]
    ) + "\n"
    output = io.StringIO()
    server.serve_stdio(io.StringIO(incoming), output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [item["id"] for item in responses] == [1, "tools-2", "unknown-3"]
    assert all(item["jsonrpc"] == "2.0" for item in responses)
    assert responses[-1]["error"]["code"] == -32601


def test_mcp_notification_with_no_id_is_silent_but_explicit_null_id_is_a_request():
    server = WorkflowMCPServer()
    assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}) is None
    assert server.handle_message({"jsonrpc": "2.0", "method": "custom/unknown-notification"}) is None
    response = server.handle_message({"jsonrpc": "2.0", "id": None, "method": "custom/unknown-request"})
    assert response["id"] is None
    assert response["error"]["code"] == -32601


def test_mcp_unknown_tool_is_bounded_tool_error(tmp_path):
    server = WorkflowMCPServer(default_workspace=str(tmp_path))
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "bad-tool",
            "method": "tools/call",
            "params": {"name": "workflow_nope", "arguments": {}},
        }
    )
    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"]["error"]["code"] == "TOOL_NOT_FOUND"


@pytest.mark.parametrize("tool,arguments", [
    ("workflow_answer", {"workspace": "{workspace}"}),
    ("workflow_status", {"workspace": "{workspace}"}),
    ("workflow_resume", {"workspace": "{workspace}"}),
    ("workflow_run", {"workspace": "{workspace}", "request": {"action": "run"}}),
])
def test_mcp_workflow_tools_report_stable_not_found_error_before_start(tmp_path, tool, arguments):
    server = WorkflowMCPServer()
    rendered = {key: (str(tmp_path) if value == "{workspace}" else value) for key, value in arguments.items()}
    response = server.handle_message(
        {"jsonrpc": "2.0", "id": tool, "method": "tools/call", "params": {"name": tool, "arguments": rendered}}
    )
    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"]["error"]["code"] == "WORKFLOW_NOT_FOUND"
