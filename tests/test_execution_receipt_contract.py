import json

import pytest

import src.openai_codex_executor as codex_executor_module
from src.contracts import ContractValidationError
from src.execution_receipt_contract import (
    EXECUTION_RECEIPT_OWNER,
    EXECUTION_RECEIPT_SCHEMA_VERSION,
    build_execution_receipt_contract,
    build_runner_execution_receipt,
    persist_execution_receipt,
    resolve_execution_receipt_path,
    validate_execution_receipt,
)
from src.executor import ExecutionRequest
from src.openai_codex_executor import CommandResult, OpenAICodexExecutor
from src.workflow_v2_controller import ingest_stage_execution_receipt, resolve_stage_execution_receipt_path


def _receipt_request(tmp_path, *, path=".tmp/provider-receipt.json") -> ExecutionRequest:
    return ExecutionRequest(
        stage_id="stage-receipt-contract",
        objective="run the bounded provider execution probe",
        iteration_index=1,
        allowed_paths=(".tmp",),
        protected_paths=(".git", "src", "tests"),
        baseline_digest="baseline-receipt",
        action_map={"validated": True, "execute": True, "actions": {"probe": {"owner": "CODEX"}}},
        plan_id="plan-receipt-contract",
        task_id="task-receipt-contract",
        workspace_root=str(tmp_path),
        metadata={
            "request_id": "request-receipt-contract",
            "operation_id": "operation-receipt-contract",
            "required_test_command": "pytest -q",
            "required_changed_path": path,
        },
    )


class _ReceiptRunner:
    def __init__(self):
        self.calls = []

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
            return CommandResult(
                0,
                "\n".join(
                    [
                        '{"type":"thread.started","thread_id":"thread-receipt"}',
                        '{"type":"turn.started"}',
                        '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q","exit_code":0}}',
                        '{"type":"turn.completed"}',
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected command: {args}")


def test_provider_execution_receipt_contract_reaches_provider(tmp_path):
    runner = _ReceiptRunner()
    provider = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=runner,
        model_discovery=lambda: [{"id": "acct-luna", "model": "gpt-current", "displayName": "Luna"}],
    )
    request = _receipt_request(tmp_path)
    result = provider.execute(request)
    receipt_path = tmp_path / ".tmp" / "provider-receipt.json"

    assert result.result["status"] == "SUCCEEDED"
    assert receipt_path.is_file()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["owner"] == EXECUTION_RECEIPT_OWNER
    assert receipt["schema_version"] == EXECUTION_RECEIPT_SCHEMA_VERSION
    assert receipt["status"] == "PASS"
    assert receipt["provider"]["id"] == "openai-codex"
    assert receipt["provider"]["model"] == "acct-luna"
    assert receipt["provider"]["request_id"] == "request-receipt-contract"
    assert receipt["provider"]["workdir"] == str(tmp_path.resolve())
    assert receipt["provider"]["allowed_paths"] == [".tmp"]
    assert len(receipt["provider"]["prompt_digest"]) == 64
    assert result.result["measurements"]["openai_codex"]["receipt_persisted"] is True
    exec_call = next(call for call in runner.calls if "exec" in call[0])
    prompt = exec_call[1]["input_text"]
    assert str(receipt_path.resolve()) in prompt
    assert EXECUTION_RECEIPT_SCHEMA_VERSION in prompt
    assert '"owner": "RUNNER"' in prompt
    assert "runner owns the execution receipt" in prompt


def test_minimal_provider_probe_emits_required_markers(tmp_path):
    provider = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=_ReceiptRunner(),
        model_discovery=lambda: [{"id": "acct-luna", "model": "gpt-current", "displayName": "Luna"}],
    )
    result = provider.execute(_receipt_request(tmp_path))
    receipt_path = tmp_path / ".tmp" / "provider-receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.is_file() else {}
    markers = {
        "PROVIDER_PROBE_EXECUTION": "PASS" if result.result["status"] == "SUCCEEDED" else "FAIL",
        "RECEIPT_CREATED": "YES" if receipt_path.is_file() else "NO",
        "SUPERVISOR_INGEST": "PASS"
        if receipt_path.is_file()
        and ingest_stage_execution_receipt(
            tmp_path,
            ".tmp/provider-receipt.json",
            (".tmp",),
            (".git", "src", "tests"),
        )
        else "FAIL",
    }
    print(" ".join(f"{key}={value}" for key, value in markers.items()))
    assert markers == {
        "PROVIDER_PROBE_EXECUTION": "PASS",
        "RECEIPT_CREATED": "YES",
        "SUPERVISOR_INGEST": "PASS",
    }


def test_receipt_path_resolves_identically_across_runner_and_supervisor(tmp_path):
    runner_path = resolve_execution_receipt_path(
        tmp_path,
        ".tmp/provider-receipt.json",
        (".tmp",),
        (".git", "src", "tests"),
    )
    supervisor_path = resolve_stage_execution_receipt_path(
        tmp_path,
        ".tmp/provider-receipt.json",
        (".tmp",),
        (".git", "src", "tests"),
    )
    assert runner_path == supervisor_path


def test_receipt_path_is_writable_under_stage_scope(tmp_path):
    contract = build_execution_receipt_contract(
        workspace_root=tmp_path,
        relative_path=".tmp/provider-receipt.json",
        allowed_paths=(".tmp",),
        protected_paths=(".git", "src", "tests"),
    )
    path = resolve_execution_receipt_path(
        contract["workspace_root"],
        contract["relative_path"],
        contract["allowed_paths"],
        contract["protected_paths"],
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("writable\n", encoding="utf-8")
    assert path.read_text(encoding="utf-8") == "writable\n"


def test_missing_receipt_blocks_execution(tmp_path, monkeypatch):
    def fail_persist(*args, **kwargs):
        raise OSError("receipt persistence unavailable")

    monkeypatch.setattr(codex_executor_module, "persist_execution_receipt", fail_persist)
    provider = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=_ReceiptRunner(),
        model_discovery=lambda: [{"id": "acct-luna", "model": "gpt-current", "displayName": "Luna"}],
    )
    result = provider.execute(_receipt_request(tmp_path))
    assert result.result["status"] == "FAILED"
    assert "runner_receipt_persistence_failed" in result.result["problems_discovered"]
    assert result.result["measurements"]["openai_codex"]["receipt_persisted"] is False
    assert result.result["measurements"]["openai_codex"]["acceptance"]["execution_receipt"] is False


def test_supervisor_ingest_missing_receipt_blocks_execution(tmp_path):
    with pytest.raises(ContractValidationError, match="execution receipt is missing"):
        ingest_stage_execution_receipt(
            tmp_path,
            ".tmp/provider-receipt.json",
            (".tmp",),
            (".git", "src", "tests"),
        )


def test_valid_receipt_allows_execution_progress(tmp_path):
    request = _receipt_request(tmp_path)
    contract = build_execution_receipt_contract(
        workspace_root=tmp_path,
        relative_path=".tmp/provider-receipt.json",
        allowed_paths=(".tmp",),
        protected_paths=(".git", "src", "tests"),
    )
    receipt = build_runner_execution_receipt(
        contract=contract,
        request=request,
        result={"status": "SUCCEEDED", "changed_files": [], "tests": [{"status": "PASS"}]},
        provider_id="openai-codex",
    )
    persist_execution_receipt(contract, receipt)
    assert validate_execution_receipt(receipt, contract=contract)["status"] == "PASS"


def test_runner_persists_receipt_from_valid_provider_result(tmp_path):
    request = _receipt_request(tmp_path)
    contract = build_execution_receipt_contract(
        workspace_root=tmp_path,
        relative_path=".tmp/provider-receipt.json",
        allowed_paths=(".tmp",),
        protected_paths=(".git", "src", "tests"),
    )
    receipt = build_runner_execution_receipt(
        contract=contract,
        request=request,
        result={"status": "SUCCEEDED", "changed_files": [], "tests": []},
        provider_id="openai-codex",
        operation="provider_receipt_probe",
    )
    actual = persist_execution_receipt(contract, receipt)
    assert actual == str((tmp_path / ".tmp" / "provider-receipt.json").resolve())
    loaded = json.loads((tmp_path / ".tmp" / "provider-receipt.json").read_text(encoding="utf-8"))
    assert loaded["operation"] == "provider_receipt_probe"
    assert validate_execution_receipt(loaded, contract=contract)["status"] == "PASS"


def test_receipt_path_scope_conflict_is_rejected(tmp_path):
    with pytest.raises(ContractValidationError, match="outside allowed_paths"):
        build_execution_receipt_contract(
            workspace_root=tmp_path,
            relative_path="tests/provider-receipt.json",
            allowed_paths=(".tmp",),
            protected_paths=(".git", "src", "tests"),
        )
