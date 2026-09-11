from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "workflow_mcp.py"


def test_launcher_forces_utf8_jsonrpc_wire_on_windows_locale(tmp_path):
    """The launcher must not depend on the parent process code page."""

    workspace = tmp_path / "disposable-workspace"
    workspace.mkdir()
    requirement = "使用 research-workflow 处理当前项目"
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "workflow_start",
                "arguments": {
                    "workspace": str(workspace),
                    "mode": "CODEX_REQUIREMENTS_INTERVIEW",
                    "rough_requirement": requirement,
                },
            },
        },
    ]
    wire_input = ("\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n").encode("utf-8")
    env = dict(os.environ)
    for name in ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO", "RESEARCH_WORKFLOW_MCP_TRACE_PATH"):
        env.pop(name, None)
    process = subprocess.Popen(
        [sys.executable, str(LAUNCHER)],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout, stderr = process.communicate(wire_input, timeout=15)

    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    assert stderr == b""
    responses = [json.loads(line) for line in stdout.decode("utf-8").splitlines()]
    assert [response["id"] for response in responses] == [1, 2]
    started = responses[1]["result"]
    assert started["isError"] is False
    assert started["structuredContent"]["next_action"] == "ASK_REQUIREMENT"
    assert (workspace / ".research" / "PROJECT_BRIEF.json").read_text(encoding="utf-8")
    brief = json.loads((workspace / ".research" / "PROJECT_BRIEF.json").read_text(encoding="utf-8"))
    assert brief["rough_requirement"] == requirement


def test_launcher_opt_in_transport_trace_is_bounded_and_stdout_stays_json(tmp_path):
    trace_parent = tmp_path / "trace"
    trace_parent.mkdir()
    trace_path = trace_parent / "transport.jsonl"
    requests = [
        {"jsonrpc": "2.0", "id": "init-1", "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "init-1"}},
        {"jsonrpc": "2.0", "method": "$/cancelRequest", "params": {"id": "init-1"}},
        {"jsonrpc": "2.0", "method": "custom/unknown-notification", "params": {}},
        {"jsonrpc": "2.0", "id": "tools-2", "method": "tools/list", "params": {}},
    ]
    wire_input = ("\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n").encode("utf-8")
    env = dict(os.environ)
    env["RESEARCH_WORKFLOW_MCP_TRACE_PATH"] = str(trace_path)
    process = subprocess.Popen(
        [sys.executable, str(LAUNCHER)],
        cwd=str(ROOT),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout, stderr = process.communicate(wire_input, timeout=15)

    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    assert stderr == b""
    responses = [json.loads(line) for line in stdout.decode("utf-8").splitlines()]
    assert [response["id"] for response in responses] == ["init-1", "tools-2"]
    assert all(response["jsonrpc"] == "2.0" for response in responses)

    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    event_names = [event["event"] for event in events]
    assert event_names[0] == "process_start"
    assert event_names[-1] == "process_exit"
    assert {
        "input_received",
        "handler_started",
        "handler_completed",
        "response_write",
        "response_flushed",
        "notification_ignored",
        "eof",
        "process_exit",
    }.issubset(event_names)
    assert events[-1]["status"] == "completed"
    assert {event["method"] for event in events if event["event"] == "notification_ignored"} == {
        "notifications/initialized",
        "notifications/cancelled",
        "$/cancelRequest",
        "custom/unknown-notification",
    }
    assert all("arguments" not in event and "result" not in event and "workspace" not in event for event in events)
    assert all(
        event.get("id_present") is False or isinstance(event.get("id_digest"), str)
        for event in events
        if event["event"] in {"input_received", "handler_started", "handler_completed"}
    )


def test_launcher_rejects_invalid_opt_in_trace_path_without_stdout_pollution(tmp_path):
    missing_parent = tmp_path / "missing" / "transport.jsonl"
    env = dict(os.environ)
    env["RESEARCH_WORKFLOW_MCP_TRACE_PATH"] = str(missing_parent)
    completed = subprocess.run(
        [sys.executable, str(LAUNCHER)],
        cwd=str(ROOT),
        input=b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n',
        capture_output=True,
        env=env,
        timeout=15,
    )
    assert completed.returncode == 2
    assert completed.stdout == b""
    assert b"invalid" in completed.stderr.lower()
