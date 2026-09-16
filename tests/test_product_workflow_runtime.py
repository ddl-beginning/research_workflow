from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

import src.product_workflow_runtime as product_runtime
import src.workflow_mcp as workflow_mcp
from src.openai_codex_executor import RuntimeSnapshot
from src.product_workflow_runtime import ProductWorkflowRuntime, _validate_transport_recovery, initialize_product_runtime
from src.runtime_composition import RuntimeCompositionError, load_runtime_composition_config
from src.stage_integration import StageIntegrationError
from src.workflow_runtime import WorkflowRuntimeError
from src.workflow_v2_controller import StageController


def _complete_brief() -> dict[str, Any]:
    """A complete existing-intake brief that still requires explicit approval."""

    return {
        "project_goal": "make the bounded local workflow observable",
        "observable_outcome": "a reviewer can verify one persisted planned stage",
        "scope": ["the local workspace"],
        "non_goals": ["browser automation in the test"],
        "constraints": ["no secrets in artifacts"],
        "available_assets": ["the existing V2 controller"],
        "acceptance": ["the planned stage reloads from the V2 journal"],
        "human_preferences": ["ask at most one question"],
    }


def _write_machine_config(
    root: Path,
    machine_root: Path,
    *,
    bridge_enabled: bool = True,
    config_path: Path | None = None,
) -> Path:
    """Write the existing runtime-composition schema with bounded machine paths."""

    bridge_root = machine_root / "bridge"
    (bridge_root / "scripts").mkdir(parents=True, exist_ok=True)
    (bridge_root / "scripts" / "consult-pack.mjs").write_text(
        "// offline test bridge; never executed by these tests\n",
        encoding="utf-8",
    )
    profile_dir = machine_root / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "schema_version": "runtime_composition.v1",
        "lifecycle_version": "v2",
        "stage": {
            "state_path": ".research/stage-state.json",
            "contract_path": None,
            "stage_id": None,
            "receipt_root": ".research/integration",
        },
        "executor": {
            "providers": ["openai-codex"],
            "preferred_provider": "openai-codex",
            "codex_executable": sys.executable,
            "preferred_model": "luna",
            "fallback_model": None,
            "auth_mode": "chatgpt",
            "timeout_seconds": 30,
            "artifact_dir": None,
        },
        "bridge": {
            "profile_dir": str(profile_dir),
            "transport": "homepage_fallback",
            "enabled": bridge_enabled,
            "root": str(bridge_root),
            "node_executable": sys.executable,
            "project_url": None,
        },
    }
    config_path = config_path or root / ".research" / "runtime-composition.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return config_path


@pytest.fixture
def native_runtime(monkeypatch: pytest.MonkeyPatch) -> RuntimeSnapshot:
    """Make native-runtime discovery deterministic without starting Codex."""

    snapshot = RuntimeSnapshot(
        executable=sys.executable,
        version="fixture-codex",
        auth_mode="chatgpt",
        authenticated=True,
        models=(
            {
                "id": "gpt-5.6-luna",
                "model": "gpt-5.6-luna",
                "display_name": "fixture Luna",
                "hidden": False,
                "is_default": True,
            },
        ),
        sdk_available=False,
        available=True,
        reason="offline fixture",
    )
    monkeypatch.setattr(
        product_runtime.OpenAICodexExecutor,
        "discover_runtime",
        lambda _provider: snapshot,
    )
    return snapshot


def _approved_runtime(root: Path, config_path: Path) -> ProductWorkflowRuntime:
    config = load_runtime_composition_config(root, config_path=config_path)
    runtime = ProductWorkflowRuntime(root, config=config)
    started = runtime.start(brief=_complete_brief())
    assert started["brief_state"] == "WAITING_USER_APPROVAL"
    approved = runtime.answer(approve=True)
    assert approved["brief_state"] == "APPROVED"
    return runtime


def _stage(runtime: ProductWorkflowRuntime, *, stage_id: str = "stage-product-planning") -> dict[str, Any]:
    brief = runtime.intake.state
    assert brief is not None
    return {
        "schema_version": "stage.v2",
        "stage_id": stage_id,
        "workspace_id": runtime.controller.workspace_id,
        "project_id": brief["project_id"],
        "objective_fingerprint": "objective-product-planning",
        "target_identity": "local/workspace",
        "required_capabilities": ["python"],
        "purpose": "DOMAIN",
        "repair_depth": 0,
        "baseline_digest": "baseline-product-planning",
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


def _planning_request(runtime: ProductWorkflowRuntime, *, prompt: str = "plan the bounded stage") -> dict[str, Any]:
    return {
        "operation": "PLAN_STAGE",
        "stage": _stage(runtime),
        "prompt": prompt,
        "context_pack": {
            "pack_sha256": "packet-product-planning",
            "purpose": "stage planning",
        },
    }


def _stub_bridge_envelope() -> dict[str, Any]:
    return {
        "status": "complete",
        "mode": "fresh",
        "response_text": "WORKFLOW_DECISION: CONTINUE",
        "consultation_id": "consultation-product-planning",
        "request_count": 1,
        "receipt": {
            "status": "complete",
            "mode": "fresh",
            "consultation_id": "consultation-product-planning",
            "request_count": 1,
            "conversation_id": "conversation-product-planning",
            "conversation_validated": True,
            "context_pack": {"pack_sha256": "packet-product-planning"},
        },
    }


def _bridge_stub(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def run_bridge(prompt: str, **kwargs: Any) -> Mapping[str, Any]:
        calls.append({"prompt": prompt, **kwargs})
        return _stub_bridge_envelope()

    monkeypatch.setattr(product_runtime, "subprocess_bridge_runner", run_bridge)
    return calls


def _write_target_plan(root: Path, target: str) -> None:
    plan = root / "plan"
    plan.mkdir(parents=True, exist_ok=True)
    (plan / "REQUIREMENTS.md").write_text(
        """# Goal
Build a project-scoped review fixture.

## Workflow Binding
ChatGPT Project URL: """ + target + "\n",
        encoding="utf-8",
    )
    (plan / "STAGE_PLAN.md").write_text(
        """# Stage Plan

## S1 - Review
- Goal: review the bounded fixture
- Machine Acceptance: focused tests pass
""",
        encoding="utf-8",
    )


def _targeted_bridge_envelope(project_url: str, index: int) -> dict[str, Any]:
    conversation_id = f"conversation-project-target-{index}"
    digest = hashlib.sha256(project_url.encode("utf-8")).hexdigest()
    return {
        "status": "complete",
        "mode": "fresh",
        "response_text": "WORKFLOW_DECISION: CONTINUE",
        "consultation_id": f"consultation-project-target-{index}",
        "request_count": 1,
        "receipt": {
            "status": "complete",
            "mode": "fresh",
            "consultation_id": f"consultation-project-target-{index}",
            "request_count": 1,
            "conversation_id": conversation_id,
            "conversation_validated": True,
            "context_pack": {"pack_sha256": "project-target-packet"},
            "project_url": project_url,
            "project_scope_requested": True,
            "project_scope_verified": True,
            "project_scope_evidence": {
                "initial_navigation": {
                    "requested_url": project_url,
                    "landed_url": project_url,
                    "matched": True,
                    "verified": True,
                }
            },
            "chatgpt_target_mode": "PROJECT",
            "chatgpt_target_url_digest": digest,
            "chatgpt_target_origin": "https://chatgpt.com",
            "chatgpt_project_target_verified": "YES",
            "fresh_project_chat_created": "YES",
        },
    }


def test_bound_project_targets_are_isolated_and_each_review_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_root = tmp_path / "project-a"
    second_root = tmp_path / "project-b"
    first_root.mkdir()
    second_root.mkdir()
    first_url = "https://chatgpt.com/g/g-p-project-a/project"
    second_url = "https://chatgpt.com/g/g-p-project-b/project"
    _write_target_plan(first_root, first_url)
    _write_target_plan(second_root, second_url)
    first_config = _write_machine_config(first_root, tmp_path / "machine-a")
    second_config = _write_machine_config(second_root, tmp_path / "machine-b")
    first = ProductWorkflowRuntime(first_root, config=load_runtime_composition_config(first_root, config_path=first_config))
    second = ProductWorkflowRuntime(second_root, config=load_runtime_composition_config(second_root, config_path=second_config))
    first._sync_plan()
    second._sync_plan()
    calls: list[dict[str, Any]] = []

    def run_bridge(prompt: str, **kwargs: Any) -> Mapping[str, Any]:
        calls.append({"prompt": prompt, **kwargs})
        return _targeted_bridge_envelope(kwargs["project_url"], len(calls))

    monkeypatch.setattr(product_runtime, "subprocess_bridge_runner", run_bridge)
    pack = {"pack_sha256": "project-target-packet"}
    first_result = first._consult({"prompt": "review A", "context_pack": pack}, purpose="technical")
    second_result = second._consult({"prompt": "review B", "context_pack": pack}, purpose="technical")

    assert [call["project_url"] for call in calls] == [first_url, second_url]
    assert all(call["profile_dir"] is None for call in calls)
    assert all(call["transport"] is None for call in calls)
    assert [call["mode"] for call in calls] == ["fresh", "fresh"]
    assert [first_result["conversation_id"], second_result["conversation_id"]] == [
        "conversation-project-target-1", "conversation-project-target-2"
    ]
    assert calls[0]["consultation_intent_key"] != calls[1]["consultation_intent_key"]
    first._consult({"prompt": "review A", "context_pack": pack}, purpose="technical")
    assert calls[0]["consultation_intent_key"] == calls[2]["consultation_intent_key"]
    assert calls[2]["recover_consultation_id"] is None

    recovery_path = first_root / ".consultations" / "intent-recovery" / f"{calls[0]['consultation_intent_key']}.json"
    recovery_path.parent.mkdir(parents=True)
    migration = {
        "intent_key": calls[0]["consultation_intent_key"],
        "project_id": first.intake.state["project_id"],
        "project_url": first_url,
        "consultation_id": "CONSULT-20260916-020743-57666118",
    }
    recovery_path.write_text(json.dumps(migration), encoding="utf-8")
    first._consult({"prompt": "review A", "context_pack": pack}, purpose="technical")
    assert calls[-1]["recover_consultation_id"] == migration["consultation_id"]
    migration["project_url"] = second_url
    recovery_path.write_text(json.dumps(migration), encoding="utf-8")
    with pytest.raises(WorkflowRuntimeError, match="legacy recovery"):
        first._consult({"prompt": "review A", "context_pack": pack}, purpose="technical")
    assert len(calls) == 4


def test_pre_prompt_attachment_upload_failure_can_be_recovered_once(tmp_path: Path) -> None:
    receipt_path = tmp_path / ".consultations" / "failed" / "receipt.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(json.dumps({
        "status": "failed_before_prompt",
        "request_count": 0,
        "failure_code": "ATTACHMENT_UPLOAD_FAILED",
        "conversation_id": None,
        "conversation_validated": False,
        "context_pack": {"packet_id": "packet-12345678", "pack_sha256": "pack-12345678"},
        "attachments": [{"upload_status": "failed"}, {"upload_status": "failed"}],
        "attachment_diagnostics": {"request_count": 0, "final_predicate": False},
        "consultation_id": "consultation-12345678",
    }), encoding="utf-8")
    recovery = _validate_transport_recovery(
        tmp_path,
        {
            "planning_revision": 1,
            "stage_id": "stage-s1",
            "receipt_path": ".consultations/failed/receipt.json",
        },
        input_digest="input-12345678",
        stage_id="stage-s1",
        planning_revision=1,
    )
    assert recovery["failure_code"] == "ATTACHMENT_UPLOAD_FAILED"
    assert recovery["request_count"] == 0


def test_clean_product_init_uses_mocked_native_runtime_and_persists_empty_v2_journal(
    tmp_path: Path, native_runtime: RuntimeSnapshot
):
    root = tmp_path / "fresh-product"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")

    result = initialize_product_runtime(root, config_path=config_path)

    assert result["ready"] is True
    assert result["lifecycle_version"] == "v2"
    assert result["initialized"] is True
    assert result["stage_created"] is False
    assert result["stage_started"] is False
    assert result["canonical"]["revision"] == 0
    assert result["canonical"]["stage"]["stage_id"] is None
    journal = root / ".workflow-v2" / "journal.json"
    assert journal.is_file()
    assert not (root / ".research" / "stage-state.json").exists()
    assert native_runtime.models[0]["id"] == "gpt-5.6-luna"


def test_missing_machine_config_returns_structured_error_without_journal_writes(tmp_path: Path):
    root = tmp_path / "missing-config"
    root.mkdir()
    missing_config = root / ".research" / "runtime-composition.json"

    with pytest.raises(RuntimeCompositionError) as caught:
        initialize_product_runtime(root, config_path=missing_config)

    assert caught.value.code == "RUNTIME_CONFIG_INVALID"
    diagnostic = caught.value.bounded_view()
    assert diagnostic["code"] == "RUNTIME_CONFIG_INVALID"
    assert diagnostic["missing"][0]["code"] == "FILE_NOT_FOUND"
    assert not (root / ".workflow-v2" / "journal.json").exists()
    assert not (root / ".research" / "journal.json").exists()
    assert not (root / ".research" / "stage-state.json").exists()


def test_same_v2_config_initializes_two_relocated_fresh_roots_without_old_state(
    tmp_path: Path, native_runtime: RuntimeSnapshot
):
    machine_root = tmp_path / "machine"
    first_root = tmp_path / "first-root"
    second_root = tmp_path / "second-root"
    first_root.mkdir()
    second_root.mkdir()
    first_config = _write_machine_config(first_root, machine_root)
    config_bytes = first_config.read_bytes()
    second_config = second_root / ".research" / "runtime-composition.json"
    second_config.parent.mkdir(parents=True, exist_ok=True)
    second_config.write_bytes(config_bytes)

    first = initialize_product_runtime(first_root, config_path=first_config)
    second = initialize_product_runtime(second_root, config_path=second_config)

    assert first_config.read_bytes() == second_config.read_bytes()
    assert first["canonical"]["workspace_id"] != second["canonical"]["workspace_id"]
    assert first["canonical"]["workspace_id"]
    assert second["canonical"]["workspace_id"]
    for root in (first_root, second_root):
        controller = StageController.from_state(root / ".workflow-v2" / "journal.json")
        assert controller.state["stages"] == {}
        assert not (root / ".research" / "stage-state.json").exists()
        assert not (root / ".research" / "workflow-checkpoint.json").exists()


def test_canonical_mcp_server_selects_product_adapter_without_legacy_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "mcp-product"
    root.mkdir()
    _write_machine_config(root, tmp_path / "machine")
    bootstrap_calls: list[str] = []

    def legacy_bootstrap(_workspace: str) -> Any:
        bootstrap_calls.append(_workspace)
        raise AssertionError("V2 product composition must not construct legacy bootstrap")

    monkeypatch.setattr(workflow_mcp, "build_workflow_orchestrator", legacy_bootstrap)
    server = workflow_mcp.WorkflowMCPServer(default_workspace=str(root))

    selected = server._runtime({})
    assert isinstance(selected, ProductWorkflowRuntime)
    assert selected.lifecycle_version == "v2"

    started = server.call_tool("workflow_start", {"workspace": str(root), "brief": _complete_brief()})
    assert started["brief_state"] == "WAITING_USER_APPROVAL"
    approved = server.call_tool("workflow_answer", {"workspace": str(root), "approve": True})
    assert approved["brief_state"] == "APPROVED"
    assert bootstrap_calls == []
    assert not (root / ".research" / "workflow-checkpoint.json").exists()
    assert not (root / ".workflow-v2" / "journal.json").exists()


def test_planning_uses_validated_bridge_envelope_and_registers_stage_through_real_v2_controller(
    tmp_path: Path, native_runtime: RuntimeSnapshot, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "planning-product"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    runtime = _approved_runtime(root, config_path)
    calls = _bridge_stub(monkeypatch)
    request = _planning_request(runtime)

    first = runtime.run(request)

    assert len(calls) == 1
    assert isinstance(runtime.controller, StageController)
    assert first["WORKFLOW_DECISION"] == "CONTINUE"
    assert first["stage_created"] is True
    assert first["stage_started"] is False
    assert first["runner_ready"] is True
    assert runtime.controller.state["stages"][request["stage"]["stage_id"]]["status"] == "PLANNED"
    assert (root / ".workflow-v2" / "journal.json").is_file()

    revision = runtime.controller.revision
    evidence = next((root / ".research" / "planning").glob("*/receipt.json"))
    evidence_bytes = evidence.read_bytes()
    second = runtime.run(request)

    assert len(calls) == 1
    assert runtime.controller.revision == revision
    assert second["planning"] == first["planning"]
    assert evidence.read_bytes() == evidence_bytes


def test_planning_changed_input_fails_without_reconsulting_or_rewriting_receipt(
    tmp_path: Path, native_runtime: RuntimeSnapshot, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "changed-planning"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    runtime = _approved_runtime(root, config_path)
    calls = _bridge_stub(monkeypatch)
    request = _planning_request(runtime)
    runtime.run(request)
    evidence = next((root / ".research" / "planning").glob("*/receipt.json"))
    original = evidence.read_bytes()

    changed = dict(request)
    changed["prompt"] = "plan the changed stage input"
    with pytest.raises(WorkflowRuntimeError) as caught:
        runtime.run(changed)

    assert caught.value.code == "PLANNING_INPUT_CHANGED"
    assert len(calls) == 1
    assert evidence.read_bytes() == original
    assert runtime.controller.state["stages"][request["stage"]["stage_id"]]["status"] == "PLANNED"


@pytest.mark.parametrize(
    ("field", "replacement"),
    (("decision", "BLOCKED"), ("conversation_id", "forged-conversation")),
)
def test_tampered_cached_planning_receipt_fails_closed_without_recomputing(
    tmp_path: Path,
    native_runtime: RuntimeSnapshot,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: str,
):
    root = tmp_path / f"tampered-{field}"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    runtime = _approved_runtime(root, config_path)
    calls = _bridge_stub(monkeypatch)
    request = _planning_request(runtime)
    runtime.run(request)
    receipt = next((root / ".research" / "planning").glob("*/receipt.json"))
    cached = json.loads(receipt.read_text(encoding="utf-8"))
    cached[field] = replacement
    receipt.write_text(json.dumps(cached, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    with pytest.raises(WorkflowRuntimeError) as caught:
        runtime.run(request)

    assert caught.value.code == "PLANNING_INPUT_CHANGED"
    assert len(calls) == 1
    assert runtime.controller.state["stages"][request["stage"]["stage_id"]]["status"] == "PLANNED"


def test_invalid_approve_string_is_rejected_without_approving_or_creating_stage(
    tmp_path: Path,
):
    root = tmp_path / "invalid-approve"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    config = load_runtime_composition_config(root, config_path=config_path)
    runtime = ProductWorkflowRuntime(root, config=config)
    started = runtime.start(brief=_complete_brief())
    assert started["brief_state"] == "WAITING_USER_APPROVAL"

    with pytest.raises(WorkflowRuntimeError) as caught:
        runtime.answer(approve="true")  # type: ignore[arg-type]

    assert caught.value.code == "INPUT_INVALID"
    assert runtime.intake.state is not None
    assert runtime.intake.state["state"] == "WAITING_USER_APPROVAL"
    assert runtime.controller.state["stages"] == {}
    assert not (root / ".workflow-v2" / "journal.json").exists()


def test_planning_missing_receipt_after_committed_intent_fails_closed(
    tmp_path: Path, native_runtime: RuntimeSnapshot, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "missing-receipt"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    runtime = _approved_runtime(root, config_path)
    calls = _bridge_stub(monkeypatch)
    request = _planning_request(runtime)
    runtime.run(request)
    receipt = next((root / ".research" / "planning").glob("*/receipt.json"))
    intent = receipt.with_name("request.json")
    assert intent.is_file()
    receipt.unlink()
    revision = runtime.controller.revision

    with pytest.raises(WorkflowRuntimeError) as caught:
        runtime.run(request)

    assert caught.value.code == "CONSULTATION_EFFECT_UNRESOLVED"
    assert len(calls) == 1
    assert intent.is_file()
    assert not receipt.exists()
    assert runtime.controller.revision == revision
    assert request["stage"]["stage_id"] not in runtime.controller.state["stages"] or runtime.controller.state["stages"][request["stage"]["stage_id"]]["status"] == "PLANNED"


def test_planning_caps_bridge_timeout_and_maps_bounded_bridge_failure(
    tmp_path: Path, native_runtime: RuntimeSnapshot, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "timeout-product"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    raw_config["executor"]["timeout_seconds"] = 900
    config_path.write_text(json.dumps(raw_config, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    runtime = _approved_runtime(root, config_path)
    calls: list[dict[str, Any]] = []

    def fail_bridge(_prompt: str, **kwargs: Any) -> Mapping[str, Any]:
        calls.append(kwargs)
        raise StageIntegrationError(
            "BRIDGE_SUBPROCESS_TIMEOUT",
            "fixture bridge timed out",
            details={"failure_phase": "BRIDGE_SUBPROCESS", "request_count": 0, "attempt_count": 1},
        )

    monkeypatch.setattr(product_runtime, "subprocess_bridge_runner", fail_bridge)
    request = _planning_request(runtime)

    with pytest.raises(WorkflowRuntimeError) as caught:
        runtime.run(request)

    assert calls and calls[0]["timeout_ms"] == 300000
    assert caught.value.code == "BRIDGE_SUBPROCESS_TIMEOUT"
    assert caught.value.details == {
        "failure_phase": "BRIDGE_SUBPROCESS",
        "request_count": 0,
        "attempt_count": 1,
    }
    intent = next((root / ".research" / "planning").glob("*/request.json"))
    assert json.loads(intent.read_text(encoding="utf-8"))["timeout_ms"] == 300000
    assert not list((root / ".research" / "planning").glob("*/receipt.json"))

    with pytest.raises(WorkflowRuntimeError) as unresolved:
        runtime.run(request)
    assert unresolved.value.code == "CONSULTATION_EFFECT_UNRESOLVED"
    assert len(calls) == 1


def test_mcp_invalid_controller_command_is_structured_and_has_no_lifecycle_write(
    tmp_path: Path,
):
    root = tmp_path / "invalid-controller-command"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    runtime = _approved_runtime(root, config_path)
    server = workflow_mcp.WorkflowMCPServer(default_workspace=str(root))

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "invalid-command",
            "method": "tools/call",
            "params": {
                "name": "workflow_run",
                "arguments": {
                    "workspace": str(root),
                    "request": {
                        "operation": "COMMAND",
                        "command": "START",
                        "subject_id": "missing-stage",
                        "payload": {},
                    },
                },
            },
        }
    )

    assert response is not None
    payload = response["result"]["structuredContent"]
    assert response["result"]["isError"] is True
    assert payload["error"]["code"] == "V2_CONTRACT_REJECTED"
    assert not (root / ".workflow-v2" / "journal.json").exists()
    assert not (root / ".research" / "stage-state.json").exists()


def test_mcp_malformed_bridge_envelope_is_structured_and_leaves_intent_unresolved(
    tmp_path: Path, native_runtime: RuntimeSnapshot, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "malformed-bridge"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine")
    runtime = _approved_runtime(root, config_path)

    def malformed_bridge(_prompt: str, **_kwargs: Any) -> Mapping[str, Any]:
        return {
            "response_text": "WORKFLOW_DECISION: CONTINUE",
            "consultation_id": "consultation-malformed",
            "request_count": 1,
            "receipt": {},
        }

    monkeypatch.setattr(product_runtime, "subprocess_bridge_runner", malformed_bridge)
    request = _planning_request(runtime)
    server = workflow_mcp.WorkflowMCPServer(default_workspace=str(root))
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "malformed-bridge",
            "method": "tools/call",
            "params": {
                "name": "workflow_run",
                "arguments": {"workspace": str(root), "request": request},
            },
        }
    )

    assert response is not None
    payload = response["result"]["structuredContent"]
    assert response["result"]["isError"] is True
    assert payload["error"]["code"] == "BRIDGE_RECEIPT_MISSING"
    assert payload["error"]["code"] != "MCP_INTERNAL_ERROR"
    assert not (root / ".workflow-v2" / "journal.json").exists()
    assert list((root / ".research" / "planning").glob("*/request.json"))
    assert not list((root / ".research" / "planning").glob("*/receipt.json"))


def test_product_init_works_in_isolated_root_when_legacy_builders_are_denied(
    tmp_path: Path, native_runtime: RuntimeSnapshot, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "isolated-product"
    root.mkdir()
    config_path = _write_machine_config(
        root,
        tmp_path / "machine",
        config_path=tmp_path / "machine" / "runtime-composition.json",
    )
    assert not (root / ".research").exists()

    def denied_legacy_builder(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("legacy runtime builder must not be read or constructed")

    monkeypatch.setattr(workflow_mcp, "build_workflow_orchestrator", denied_legacy_builder)
    monkeypatch.setattr(workflow_mcp, "build_runtime_composition", denied_legacy_builder)
    monkeypatch.setenv("RESEARCH_WORKFLOW_RUNTIME_CONFIG", str(config_path))

    server = workflow_mcp.WorkflowMCPServer(default_workspace=str(root))
    selected = server._runtime({})
    result = initialize_product_runtime(root, config_path=config_path)

    assert isinstance(selected, ProductWorkflowRuntime)
    assert result["ready"] is True
    assert (root / ".workflow-v2" / "journal.json").is_file()
    assert not (root / ".research").exists()


def test_mcp_reports_missing_machine_dependency_structured_and_does_not_create_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "blocked-product"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine", bridge_enabled=False)
    runtime = _approved_runtime(root, config_path)
    request = _planning_request(runtime)
    server = workflow_mcp.WorkflowMCPServer(default_workspace=str(root))

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "blocked-plan",
            "method": "tools/call",
            "params": {
                "name": "workflow_run",
                "arguments": {"workspace": str(root), "request": request},
            },
        }
    )

    assert response is not None
    payload = response["result"]["structuredContent"]
    assert response["result"]["isError"] is True
    assert payload["error"]["code"] == "RUNNER_NOT_CONFIGURED"
    assert payload["details"]["missing"]
    assert any(item["code"] == "BRIDGE_CONFIGURATION_REQUIRED" for item in payload["details"]["missing"])
    assert not (root / ".workflow-v2" / "journal.json").exists()
    assert not (root / ".research" / "stage-state.json").exists()
    assert not (root / ".research" / "workflow-checkpoint.json").exists()


def test_intake_and_direct_lifecycle_commands_never_create_stage_without_planning_consultant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "lifecycle-gate"
    root.mkdir()
    config_path = _write_machine_config(root, tmp_path / "machine", bridge_enabled=False)
    runtime = _approved_runtime(root, config_path)

    assert runtime.controller.state["stages"] == {}
    assert not (root / ".workflow-v2" / "journal.json").exists()

    with pytest.raises(WorkflowRuntimeError) as caught:
        runtime.run(
            {
                "operation": "COMMAND",
                "command": "REGISTER_STAGE",
                "subject_id": _stage(runtime)["stage_id"],
                "payload": {"stage": _stage(runtime)},
            }
        )

    assert caught.value.code == "RUN_REQUEST_INVALID"
    assert runtime.controller.state["stages"] == {}
    assert not (root / ".workflow-v2" / "journal.json").exists()
