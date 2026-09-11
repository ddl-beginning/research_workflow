from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest

import src.openai_codex_executor as codex_executor_module
from src.contracts import ContractValidationError, validate_against_schema
from src.executor import ExecutionRequest
from src.openai_codex_executor import (
    CommandResult,
    OpenAICodexExecutor,
    OpenAICodexUnavailable,
    parse_auth_status,
    parse_exec_jsonl,
    parse_model_catalog,
    select_available_model,
)


def _request(tmp_path, *, preference: str | None = None, fallback: str | None = None) -> ExecutionRequest:
    metadata = {
        "required_test_command": "pytest -q",
        "required_changed_path": "tests/changed.py",
    }
    if preference:
        metadata["model_preference"] = preference
    if fallback:
        metadata["fallback_model"] = fallback
    return ExecutionRequest(
        stage_id="stage-native-codex",
        objective="change only the disposable fixture and run its local checks",
        iteration_index=1,
        allowed_paths=("tests",),
        protected_paths=(".git", "production"),
        baseline_digest="baseline-1",
        action_map={
            "validated": True,
            "execute": True,
            "actions": {"run_local_checks": {"owner": "CODEX"}},
        },
        plan_id="plan-native-codex",
        task_id="task-native-codex",
        workspace_root=str(tmp_path),
        metadata=metadata,
    )


def test_model_catalog_parser_and_runtime_alias_selection_are_observed_not_assumed():
    catalog = parse_model_catalog(
        {
            "id": 2,
            "result": {
                "data": [
                    {"id": "provider-alpha", "model": "model-alpha", "displayName": "Alpha"},
                    {"id": "provider-luna", "model": "model-luna", "displayName": "Luna"},
                ]
            },
        }
    )
    assert select_available_model(catalog, "luna") == "provider-luna"
    assert select_available_model(catalog, "alpha") == "provider-alpha"
    with pytest.raises(OpenAICodexUnavailable, match="unavailable"):
        select_available_model(catalog, "sol")
    assert select_available_model(catalog, "sol", fallback="alpha") == "provider-alpha"


def test_cli_debug_models_catalog_uses_slug_and_discards_unbounded_fields():
    catalog = parse_model_catalog(
        {
            "models": [
                {
                    "slug": "gpt-5.6-luna",
                    "display_name": "GPT-5.6-Luna",
                    "visibility": "list",
                    "supported_reasoning_levels": [
                        {"effort": "low", "description": "bounded"},
                        {"effort": "high", "description": "bounded"},
                        {"effort": "max", "description": "bounded"},
                    ],
                    "base_instructions": "must never be retained",
                }
            ]
        }
    )
    assert catalog[0]["id"] == "gpt-5.6-luna"
    assert catalog[0]["model"] == "gpt-5.6-luna"
    assert catalog[0]["display_name"] == "GPT-5.6-Luna"
    assert catalog[0]["supported_reasoning_efforts"] == ["low", "high", "max"]
    assert "base_instructions" not in repr(catalog)
    assert select_available_model(catalog, "luna") == "gpt-5.6-luna"


def test_model_catalog_parser_rejects_malformed_or_unidentified_entries():
    with pytest.raises(ContractValidationError, match="valid JSON"):
        parse_model_catalog("not-json")
    with pytest.raises(ContractValidationError, match="no model id"):
        parse_model_catalog({"models": [{"display_name": "missing identity"}]})


def test_subprocess_forces_utf8_and_replacement_decoding(monkeypatch):
    observed: dict[str, object] = {}

    class _Completed:
        returncode = 0
        stdout = "模型目录\n"
        stderr = "警告\n"

    def fake_run(*args, **kwargs):
        observed.update(kwargs)
        return _Completed()

    monkeypatch.setattr(codex_executor_module.subprocess, "run", fake_run)
    result = codex_executor_module._run_subprocess(["codex", "debug", "models"])
    assert result.returncode == 0
    assert result.stdout == "模型目录\n"
    assert result.stderr == "警告\n"
    assert observed["encoding"] == "utf-8"
    assert observed["errors"] == "replace"


def test_unicode_catalog_is_supported_and_malformed_catalog_fails_closed(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text("placeholder", encoding="utf-8")
    unicode_runner = _DebugModelsRunner(
        CommandResult(0, '{"models":[{"slug":"gpt-5.6-luna","display_name":"GPT-5.6-月"}]}', "")
    )
    provider = OpenAICodexExecutor(
        codex_executable=str(executable),
        workspace_root=str(tmp_path),
        command_runner=unicode_runner,
    )
    assert provider.descriptor.available is True
    assert provider.runtime.models[0]["display_name"] == "GPT-5.6-月"

    malformed_runner = _DebugModelsRunner(CommandResult(0, '{"models":[\ufffd', ""))
    malformed = OpenAICodexExecutor(
        codex_executable=str(executable),
        workspace_root=str(tmp_path),
        command_runner=malformed_runner,
    )
    assert malformed.descriptor.available is False
    assert "valid JSON" in malformed.runtime.reason


def test_auth_parser_distinguishes_saved_chatgpt_from_api_key_or_missing_session():
    assert parse_auth_status("Logged in using ChatGPT\n") == "chatgpt"
    assert parse_auth_status("Logged in using API key\n") == "api_key"
    assert parse_auth_status("Not logged in\n") is None


def test_exec_jsonl_parser_only_returns_bounded_facts_and_test_status():
    parsed = parse_exec_jsonl(
        '\n'.join(
            [
                '{"type":"thread.started","thread_id":"secret-thread-id"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q","exit_code":0}}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"raw response must not be returned"}}',
                '{"type":"turn.completed","usage":{"input_tokens":123}}',
            ]
        )
    )
    assert parsed.terminal_status == "completed"
    assert parsed.thread_seen is True
    assert parsed.turn_seen is True
    assert parsed.tests == ({"name": "Codex-reported local test command", "status": "PASS"},)
    assert "secret-thread-id" not in repr(parsed)
    assert "raw response" not in repr(parsed)


def test_exec_jsonl_parser_tracks_the_required_test_without_retaining_raw_command():
    parsed = parse_exec_jsonl(
        '{"type":"thread.started","thread_id":"secret-thread"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q tests/test_add.py","exit_code":0}}\n'
        '{"type":"turn.completed"}\n',
        required_test="pytest -q tests/test_add.py",
    )
    assert parsed.required_test_seen is True
    assert parsed.required_test_passed is True
    assert parsed.tests == ({"name": "pytest -q tests/test_add.py", "status": "PASS"},)
    assert "secret-thread" not in repr(parsed)


def test_required_test_last_attempt_passes_after_bounded_probe_failures():
    parsed = parse_exec_jsonl(
        "\n".join(
            [
                '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q tests/test_add.py","exit_code":1}}',
                '{"type":"item.completed","item":{"type":"command_execution","command":"PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/test_add.py","exit_code":1}}',
                '{"type":"item.completed","item":{"type":"command_execution","command":"PYTHONPATH=src pytest -q tests/test_add.py","exit_code":0}}',
            ]
        ),
        required_test="pytest -q tests/test_add.py",
    )
    assert parsed.required_test_seen is True
    assert parsed.required_test_passed is True
    assert [item["status"] for item in parsed.tests] == ["FAIL", "FAIL", "PASS"]
    assert sum(item.get("required_test") is True for item in parsed.event_summaries) == 3


def test_required_test_last_attempt_failure_revokes_an_earlier_pass():
    parsed = parse_exec_jsonl(
        "\n".join(
            [
                '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q tests/test_add.py","exit_code":0}}',
                '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q tests/test_add.py","exit_code":1}}',
            ]
        ),
        required_test="pytest -q tests/test_add.py",
    )
    assert parsed.required_test_seen is True
    assert parsed.required_test_passed is False
    assert [item["status"] for item in parsed.tests] == ["PASS", "FAIL"]


def test_required_test_without_a_matching_attempt_is_not_passed():
    parsed = parse_exec_jsonl(
        '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q tests/other.py","exit_code":0}}',
        required_test="pytest -q tests/test_add.py",
    )
    assert parsed.required_test_seen is False
    assert parsed.required_test_passed is False


def test_required_test_matching_normalizes_windows_shell_quoting():
    command = r'''pwsh -Command "python -c \"from pathlib import Path; assert Path('.research/worker.txt').read_text() == 'PASS'\""'''
    parsed = parse_exec_jsonl(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": command, "exit_code": 0},
            }
        ),
        required_test="python -c \"from pathlib import Path; assert Path('.research/worker.txt').read_text() == 'PASS'\"",
    )
    assert parsed.required_test_seen is True
    assert parsed.required_test_passed is True


def test_exec_jsonl_parser_records_bounded_failed_command_and_error_summary():
    parsed = parse_exec_jsonl(
        "\n".join(
            [
                '{"type":"thread.started"}',
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q tests/test_add.py --token=sk-test-secret-value","status":"failed","exit_code":-1}}',
                '{"type":"error","error":{"code":"turn_failed","message":"provider token=sk-error-secret-value was rejected"}}',
                '{"type":"turn.failed","error":{"code":"turn_failed","message":"same failure"}}',
            ]
        ),
        required_test="pytest -q tests/test_add.py",
    )
    assert parsed.required_test_seen is True
    assert parsed.required_test_passed is False
    command_event = next(item for item in parsed.event_summaries if item.get("item_type") == "command_execution")
    assert command_event["exit_code"] == -1
    assert command_event["required_test"] is True
    assert command_event["command_status"] == "failed"
    assert isinstance(command_event["command_sha256"], str) and len(command_event["command_sha256"]) == 64
    assert "sk-test-secret-value" not in repr(command_event)
    assert "command_preview" in command_event
    error_event = next(item for item in parsed.event_summaries if item.get("type") == "error")
    assert error_event["error"]["code"] == "turn_failed"
    assert "sk-error-secret-value" not in repr(error_event)
    assert "token=<REDACTED>" in error_event["error"]["message"]


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.turn_done = False

    def __call__(self, argv, **kwargs):
        args = [str(item) for item in argv]
        self.calls.append((args, kwargs))
        if args[1:] == ["--version"]:
            return CommandResult(0, "codex-cli 0.145.0\n", "")
        if args[1:3] == ["login", "status"]:
            return CommandResult(0, "Logged in using ChatGPT\n", "")
        if "git" in args and "status" in args:
            if self.turn_done:
                return CommandResult(0, " M tests/changed.py\n", "")
            return CommandResult(0, "", "")
        if "git" in args and "diff" in args:
            if self.turn_done:
                return CommandResult(0, "diff --git a/tests/changed.py b/tests/changed.py\n+changed\n", "")
            return CommandResult(0, "", "")
        if "exec" in args:
            self.turn_done = True
            return CommandResult(
                0,
                '\n'.join(
                    [
                        '{"type":"thread.started","thread_id":"thread-secret"}',
                        '{"type":"turn.started"}',
                        '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q","exit_code":0}}',
                        '{"type":"turn.completed","usage":{"input_tokens":999}}',
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected command: {args}")


class _UntrackedFakeRunner(_FakeRunner):
    def __call__(self, argv, **kwargs):
        args = [str(item) for item in argv]
        if "git" in args and "status" in args and self.turn_done:
            self.calls.append((args, kwargs))
            return CommandResult(0, "?? tests/changed.py\n", "")
        if "git" in args and "diff" in args and self.turn_done:
            self.calls.append((args, kwargs))
            return CommandResult(0, "", "")
        return super().__call__(argv, **kwargs)


class _NoOpRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        args = [str(item) for item in argv]
        self.calls.append((args, kwargs))
        if args[1:] == ["--version"]:
            return CommandResult(0, "codex-cli 0.145.0\n", "")
        if args[1:3] == ["login", "status"]:
            return CommandResult(0, "Logged in using ChatGPT\n", "")
        if "git" in args and ("status" in args or "diff" in args):
            return CommandResult(0, "", "")
        if "exec" in args:
            return CommandResult(0, '{"type":"thread.started"}\n{"type":"turn.started"}\n{"type":"turn.completed"}\n', "")
        raise AssertionError(f"unexpected command: {args}")


class _CapacityRunner(_FakeRunner):
    """Emit a bounded provider-capacity failure without a real Codex turn."""

    def __call__(self, argv, **kwargs):
        args = [str(item) for item in argv]
        if "exec" in args:
            return CommandResult(
                1,
                "\n".join(
                    [
                        '{"type":"thread.started"}',
                        '{"type":"turn.started"}',
                        '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q","status":"failed","exit_code":-1}}',
                        '{"type":"error","error":{"code":"model_at_capacity","message":"selected model is at capacity"}}',
                        '{"type":"turn.failed","error":{"code":"model_at_capacity","message":"selected model is at capacity"}}',
                    ]
                ),
                "",
            )
        return super().__call__(argv, **kwargs)


class _SandboxFailureRunner(_FakeRunner):
    """Emit the bounded Windows sandbox child-process diagnostic only."""

    def __call__(self, argv, **kwargs):
        args = [str(item) for item in argv]
        if "exec" in args:
            return CommandResult(
                1,
                "\n".join(
                    [
                        '{"type":"thread.started"}',
                        '{"type":"turn.started"}',
                        '{"type":"error","error":{"code":"sandbox_spawn_failed","message":"CreateProcessWithLogonW failed: 2"}}',
                        '{"type":"turn.failed","error":{"code":"sandbox_spawn_failed","message":"CreateProcessWithLogonW failed: 2"}}',
                    ]
                ),
                "",
            )
        return super().__call__(argv, **kwargs)


class _DebugModelsRunner:
    def __init__(self, result: CommandResult) -> None:
        self.result = result
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        args = [str(item) for item in argv]
        self.calls.append((args, kwargs))
        if args[1:] == ["--version"]:
            return CommandResult(0, "codex-cli 0.145.0\n", "")
        if args[1:3] == ["login", "status"]:
            return CommandResult(0, "Logged in using ChatGPT\n", "")
        if args[1:3] == ["debug", "models"]:
            return self.result
        raise AssertionError(f"unexpected command: {args}")


class _AuthUnavailableRunner:
    def __call__(self, argv, **kwargs):
        args = [str(item) for item in argv]
        if args[1:] == ["--version"]:
            return CommandResult(0, "codex-cli 0.145.0\n", "")
        if args[1:3] == ["login", "status"]:
            return CommandResult(1, "Not logged in\n", "")
        raise AssertionError(f"unexpected command: {args}")


def test_default_discovery_uses_bounded_cli_debug_models_not_app_server(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text("placeholder", encoding="utf-8")
    runner = _DebugModelsRunner(
        CommandResult(
            0,
            '{"models":[{"slug":"gpt-5.6-luna","display_name":"GPT-5.6-Luna"},{"slug":"gpt-5.6-sol","display_name":"GPT-5.6-Sol"}]}',
            "",
        )
    )
    provider = OpenAICodexExecutor(
        codex_executable=str(executable),
        workspace_root=str(tmp_path),
        command_runner=runner,
    )
    assert provider.descriptor.available is True
    assert provider.runtime.models[0]["id"] == "gpt-5.6-luna"
    assert any(call[0][1:3] == ["debug", "models"] for call in runner.calls)
    assert not any("app-server" in call[0] for call in runner.calls)


def test_default_discovery_timeout_is_bounded_and_fails_closed(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text("placeholder", encoding="utf-8")
    runner = _DebugModelsRunner(CommandResult(124, "", "command timed out"))
    provider = OpenAICodexExecutor(
        codex_executable=str(executable),
        workspace_root=str(tmp_path),
        command_runner=runner,
    )
    assert provider.descriptor.available is False
    assert provider.runtime.reason == "codex debug models timed out"
    debug_call = next(call for call in runner.calls if call[0][1:3] == ["debug", "models"])
    assert debug_call[1]["timeout"] <= 30.0


def test_exec_command_windows_override_is_bounded_and_non_windows_does_not_inherit_it():
    windows = OpenAICodexExecutor._exec_command(
        "codex", "acct-luna", "C:/workspace", is_windows=True
    )
    assert windows[0] == "codex"
    assert windows[1:3] == ["-c", 'windows.sandbox="unelevated"']
    assert "--sandbox" in windows
    assert windows[windows.index("--sandbox") + 1] == "workspace-write"
    assert "--dangerously-bypass-approvals-and-sandbox" not in windows

    non_windows = OpenAICodexExecutor._exec_command(
        "codex", "acct-luna", "/workspace", is_windows=False
    )
    assert non_windows[1:3] == ["-c", 'model_reasoning_effort="low"']
    assert non_windows[3] == "exec"
    assert "windows.sandbox=\"unelevated\"" not in non_windows
    assert non_windows[non_windows.index("--sandbox") + 1] == "workspace-write"


def test_native_provider_runs_through_existing_execution_contract_without_stage_access(tmp_path):
    runner = _FakeRunner()
    provider = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=runner,
        model_discovery=lambda: [{"id": "acct-luna", "model": "gpt-current", "displayName": "Luna"}],
    )
    request = _request(tmp_path)
    result = provider.execute(request)
    validate_against_schema(result.to_stage_result(), "codex_result")
    assert result.provider_id == "openai-codex"
    assert result.result["status"] == "SUCCEEDED"
    assert result.result["changed_files"] == ["tests/changed.py"]
    assert result.result["tests"] == [{"name": "pytest -q", "status": "PASS"}]
    assert result.result["stage_ready"] is False
    assert result.result["human_gate_required"] is False
    assert result.evidence["auth_mode"] == "chatgpt"
    assert result.evidence["model"] == "acct-luna"
    assert result.evidence["execution_profile"] == "STANDARD"
    assert result.evidence["actual_model"] == "acct-luna"
    assert result.evidence["reasoning_effort"] == "max"
    assert result.evidence["profile_derivation_reason"] == "default_standard"
    assert result.evidence["executor_request_id"] == "task-native-codex"
    assert all("thread-secret" not in repr(item) for item in (result.result, result.evidence))
    assert all("input_tokens" not in repr(item) for item in (result.result, result.evidence))
    exec_call = next(call for call in runner.calls if "exec" in call[0])
    expected_prefix = ["-c", 'windows.sandbox="unelevated"'] if os.name == "nt" else []
    assert exec_call[0][1:] == expected_prefix + [
        "-c",
        'model_reasoning_effort="max"',
        "exec",
        "--json",
        "--ephemeral",
        "--model",
        "acct-luna",
        "--cd",
        str(tmp_path),
        "--sandbox",
        "workspace-write",
        "--skip-git-repo-check",
        "-",
    ]
    assert "--ask-for-approval" not in exec_call[0]
    assert exec_call[1]["cwd"] == str(tmp_path)
    assert "OPENAI_API_KEY" not in exec_call[1]["env"]
    assert "validated Stage task" in exec_call[1]["input_text"]
    assert request.allowed_paths == ("tests",)
    assert request.protected_paths == (".git", "production")
    assert not any(
        any(fragment in key.upper() for fragment in ("API_KEY", "AUTH_TOKEN", "ACCESS_TOKEN", "SECRET", "PASSWORD"))
        for key in exec_call[1]["env"]
    )


def test_native_provider_accepts_a_new_untracked_allowed_file_as_nonempty_evidence(tmp_path):
    provider = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=_UntrackedFakeRunner(),
        model_discovery=lambda: [{"id": "acct-luna", "model": "gpt-current", "displayName": "Luna"}],
    )
    result = provider.execute(_request(tmp_path))
    assert result.result["status"] == "SUCCEEDED"
    assert result.result["measurements"]["openai_codex"]["acceptance"]["nonempty_diff"] is True


def test_frontier_profile_binds_astra_and_high_effort_at_the_cli_seam(tmp_path):
    runner = _FakeRunner()
    provider = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=runner,
        model_discovery=lambda: [{"id": "gpt-6-astra", "model": "gpt-6-astra", "displayName": "Astra"}],
    )
    base = _request(tmp_path)
    request = replace(
        base,
        metadata={
            **base.metadata,
            "execution_profile": "FRONTIER",
            "execution_profile_authority": "authoritative_executor_request",
        },
    )
    result = provider.execute(request)
    assert result.evidence["execution_profile"] == "FRONTIER"
    assert result.evidence["actual_model"] == "gpt-6-astra"
    assert result.evidence["reasoning_effort"] == "low"
    exec_call = next(call for call in runner.calls if "exec" in call[0])
    assert exec_call[0][exec_call[0].index("--model") + 1] == "gpt-6-astra"
    assert 'model_reasoning_effort="low"' in exec_call[0]


def test_native_provider_fails_closed_without_required_coding_evidence(tmp_path):
    provider = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=_NoOpRunner(),
        model_discovery=lambda: [{"id": "acct-luna", "model": "gpt-current", "displayName": "Luna"}],
    )
    result = provider.execute(_request(tmp_path))
    assert result.result["status"] == "FAILED"
    assert result.result["measurements"]["openai_codex"]["acceptance"]["coding_e2e_accepted"] is False
    assert "no_allowed_changed_file" in result.result["problems_discovered"]
    assert "required_test_not_passed" in result.result["problems_discovered"]
    assert "nonempty_diff_required" in result.result["problems_discovered"]
    assert "execution_failure" not in result.result


def test_native_provider_classifies_only_explicit_sandbox_spawn_failure(tmp_path):
    provider = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=_SandboxFailureRunner(),
        model_discovery=lambda: [{"id": "acct-luna", "model": "gpt-current", "displayName": "Luna"}],
    )
    result = provider.execute(_request(tmp_path))
    validate_against_schema(result.to_stage_result(), "codex_result")
    assert result.result["status"] == "ERROR"
    assert result.result["execution_failure"]["kind"] == "PROVIDER_FAILURE"
    assert result.result["execution_failure"]["code"] == "SANDBOX_UNAVAILABLE"
    assert result.result["execution_failure"]["reason"] == "sandbox_spawn_failed CreateProcessWithLogonW failed: 2"


def test_native_provider_persists_only_bounded_run_artifacts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact_dir = tmp_path / "artifacts"
    runner = _FakeRunner()
    provider = OpenAICodexExecutor(
        workspace_root=str(workspace),
        artifact_dir=str(artifact_dir),
        command_runner=runner,
        model_discovery=lambda: [{"id": "acct-luna", "model": "gpt-current", "displayName": "Luna"}],
    )
    result = provider.execute(_request(workspace))
    artifacts = result.evidence["artifacts"]
    assert artifacts["saved"] is True
    assert (artifact_dir / "request.json").is_file()
    assert (artifact_dir / "events.jsonl").is_file()
    assert (artifact_dir / "workspace.diff").is_file()
    assert (artifact_dir / "run.json").is_file()
    combined = "\n".join(path.read_text(encoding="utf-8") for path in artifact_dir.iterdir())
    events = (artifact_dir / "events.jsonl").read_text(encoding="utf-8")
    assert "command_sha256" in events
    assert "exit_code" in events
    assert "thread-secret" not in combined
    assert "input_tokens" not in combined
    assert '"raw_prompt_stored": false' in combined
    assert "workspace.diff" in combined


def test_native_provider_requires_explicit_fallback_when_luna_is_not_available(tmp_path):
    provider = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=_FakeRunner(),
        model_discovery=lambda: [{"id": "acct-sol", "model": "gpt-current", "displayName": "Sol"}],
    )
    unavailable = provider.execute(_request(tmp_path))
    validate_against_schema(unavailable.to_stage_result(), "codex_result")
    assert unavailable.result["status"] == "ERROR"
    assert unavailable.result["execution_failure"]["kind"] == "PROVIDER_FAILURE"
    assert unavailable.result["execution_failure"]["code"] == "MODEL_UNAVAILABLE"
    # A configured fallback is a model selection choice, not a second executor.
    runner = _FakeRunner()
    provider = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=runner,
        model_discovery=lambda: [{"id": "acct-sol", "model": "gpt-current", "displayName": "Sol"}],
        fallback_model="sol",
    )
    assert provider.execute(_request(tmp_path)).evidence["model"] == "acct-sol"


def test_native_provider_normalizes_model_capacity_as_provider_failure(tmp_path):
    provider = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=_CapacityRunner(),
        model_discovery=lambda: [{"id": "acct-luna", "model": "gpt-current", "displayName": "Luna"}],
    )
    result = provider.execute(_request(tmp_path))
    validate_against_schema(result.to_stage_result(), "codex_result")
    assert result.result["status"] == "ERROR"
    assert result.result["execution_failure"] == {
        "kind": "PROVIDER_FAILURE",
        "code": "MODEL_AT_CAPACITY",
        "retryable": True,
        "reason": "model_at_capacity selected model is at capacity",
    }
    assert result.result["measurements"]["openai_codex"]["failure_code"] == "MODEL_AT_CAPACITY"


def test_native_provider_normalizes_missing_cli_as_provider_failure(tmp_path):
    provider = OpenAICodexExecutor(
        codex_executable=str(tmp_path / "does-not-exist"),
        workspace_root=str(tmp_path),
        model_discovery=lambda: [],
    )
    result = provider.execute(_request(tmp_path))
    validate_against_schema(result.to_stage_result(), "codex_result")
    assert result.result["status"] == "ERROR"
    assert result.result["execution_failure"]["kind"] == "PROVIDER_FAILURE"
    assert result.result["execution_failure"]["code"] == "PROVIDER_UNAVAILABLE"


def test_native_provider_normalizes_missing_chatgpt_session_as_auth_failure(tmp_path):
    executable = tmp_path / "codex"
    executable.write_text("placeholder", encoding="utf-8")
    provider = OpenAICodexExecutor(
        codex_executable=str(executable),
        workspace_root=str(tmp_path),
        command_runner=_AuthUnavailableRunner(),
    )
    result = provider.execute(_request(tmp_path))
    validate_against_schema(result.to_stage_result(), "codex_result")
    assert result.result["status"] == "ERROR"
    assert result.result["execution_failure"]["code"] == "AUTH_UNAVAILABLE"


def test_api_key_auth_is_not_implicitly_supported():
    with pytest.raises(OpenAICodexUnavailable, match="saved ChatGPT auth"):
        OpenAICodexExecutor(auth_mode="api_key")


def test_missing_cli_is_unavailable_without_fabricating_a_provider(tmp_path):
    provider = OpenAICodexExecutor(
        codex_executable=str(tmp_path / "does-not-exist"),
        workspace_root=str(tmp_path),
        model_discovery=lambda: [],
    )
    assert provider.descriptor.available is False
    assert "not found" in provider.descriptor.metadata["runtime_reason"]
