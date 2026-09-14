from __future__ import annotations

import json
from pathlib import Path

from src.executor import ExecutionRequest
from src.human_summary import MACHINE_DETAIL_KEYS, build_human_presentation, render_human_presentation
from src.openai_codex_executor import CommandResult, OpenAICodexExecutor


def _ready_state() -> dict:
    return {
        "next_action": "COMMIT_INTEGRATION",
        "phase": "RUNNING",
        "stage": {
            "stage_id": "stage-facade-123456",
            "stage_name": "基础几何恢复",
            "status": "READY",
            "next_action": "COMMIT_INTEGRATION",
            "scenes_completed": ["S0", "S1", "S2", "S3"],
        },
    }


def test_stage_ready_is_human_first_and_evidence_explains_meaning() -> None:
    presentation = build_human_presentation(
        metadata={
            "stage": {
                "stage_id": "stage-facade-123456",
                "stage_goal": "恢复基础几何",
                "accepted_evidence": [
                    "S0–S2 已恢复简单墙面，并在约 5 mm 噪声下保持稳定",
                    "S1 的四个洞没有错误切碎墙面",
                    "S3 已区分两个表面",
                ],
                "limitations": ["inner termination 尚未实现", "finite intersection（有限交线）尚未实现"],
                "changed_files": ["src/facade.py"],
                "tests": [{"name": "facade validation", "status": "PASS"}],
                "review_artifacts": [".research/V2_REVIEW.png"],
                "human_review_mode": "visual",
            },
            "review_decision": {"decision": "STAGE_READY", "consultation_id": "CONSULT-ready-123456"},
        },
        canonical_state=_ready_state(),
    )
    text = render_human_presentation(presentation)
    assert text.startswith("HUMAN_SUMMARY")
    assert text.index("MACHINE_DETAILS") > text.index("当前结论")
    assert "约 5 mm 噪声" in text
    assert "inner termination 尚未实现" in text
    assert presentation["machine_details"]["GPT_DECISION"] == "STAGE_READY"
    assert presentation["machine_details"]["CONSULTATION_ID"] == "CONSULT-ready-123456"
    assert list(presentation["machine_details"]) == list(MACHINE_DETAIL_KEYS)


def test_blocked_summary_names_blocker_and_minimum_human_input() -> None:
    presentation = build_human_presentation(
        metadata={
            "presentation_status": "BLOCKED",
            "blocker_summary": "真实原始点云 identity 缺失",
            "required_input": "V1 原始点云文件及其 metadata",
            "earliest_remaining_failure": "MISSING_EXTERNAL_RAW_DATA",
        },
        canonical_state={"next_action": "WAIT", "stage": {"stage_id": "stage-blocked-123456", "status": "ACTIVE"}},
    )
    summary = presentation["human_summary"]
    assert summary["status"] == "BLOCKED"
    assert "真实原始点云 identity 缺失" in summary["conclusion"]
    assert "V1 原始点云文件及其 metadata" in summary["action_required"]
    assert presentation["machine_details"]["EARLIEST_REMAINING_FAILURE"] == "MISSING_EXTERNAL_RAW_DATA"


def test_human_gate_lists_options_and_tradeoffs() -> None:
    presentation = build_human_presentation(
        metadata={
            "presentation_status": "HUMAN_GATE",
            "options": [
                {"label": "A", "tradeoff": "保持当前表示，改动小"},
                {"label": "B", "tradeoff": "切换表示，验证成本更高"},
            ],
        },
        canonical_state={"next_action": "APPLY_DECISION", "stage": {"stage_id": "stage-choice-123456", "status": "ACTIVE"}},
    )
    action = presentation["human_summary"]["action_required"]
    assert "A：保持当前表示，改动小" in action
    assert "B：切换表示，验证成本更高" in action
    assert "不会替你决定" in presentation["human_summary"]["conclusion"]


def test_continue_and_replan_are_automatic() -> None:
    for decision in ("CONTINUE", "REPLAN"):
        presentation = build_human_presentation(
            metadata={"gpt_decision": decision},
            canonical_state={"next_action": "REQUEST_EXECUTION", "stage": {"stage_id": "stage-auto-123456", "status": "ACTIVE"}},
        )
        assert "不需要介入" in presentation["human_summary"]["conclusion"]
        assert "自动继续" in presentation["human_summary"]["action_required"]
        assert presentation["machine_details"]["GPT_DECISION"] == decision
        assert presentation["machine_details"]["NEXT_ACTION"] == "REQUEST_EXECUTION"


def test_visual_gate_without_artifact_is_incomplete_and_does_not_claim_acceptance(tmp_path: Path) -> None:
    presentation = build_human_presentation(
        metadata={
            "presentation_status": "HUMAN_GATE",
            "human_review_mode": "visual",
            "measured_points": [[0, 0], [1, 1]],
            # No candidate is supplied, so recovery must remain incomplete.
        },
        canonical_state={"next_action": "APPLY_DECISION", "stage": {"stage_id": "stage-visual-123456", "status": "ACTIVE"}},
        artifact_root=tmp_path,
    )
    assert presentation["presentation_status"] == "HUMAN_REVIEW_PRESENTATION_INCOMPLETE"
    assert presentation["visual_artifact_present"] is False
    assert "还不能进入人工验收" in presentation["human_summary"]["conclusion"]
    assert not (tmp_path / ".research" / "REVIEW_VISUAL.svg").exists()


def test_visual_recovery_binds_measured_and_candidate_points(tmp_path: Path) -> None:
    presentation = build_human_presentation(
        metadata={
            "presentation_status": "HUMAN_GATE",
            "human_review_mode": "visual",
            "measured_points": [[0, 0], [1, 1], [2, 0]],
            "candidate_points": [[0, 0], [1, 0.9], [2, 0]],
        },
        canonical_state={"next_action": "APPLY_DECISION", "stage": {"stage_id": "stage-visual-123456", "status": "ACTIVE"}},
        artifact_root=tmp_path,
    )
    artifact = tmp_path / ".research" / "REVIEW_VISUAL.svg"
    assert presentation["presentation_status"] == "OK"
    assert presentation["presentation_recovery"] == ".research/REVIEW_VISUAL.svg"
    assert artifact.is_file()
    content = artifact.read_text(encoding="utf-8")
    assert "blue=measured" in content
    assert "red=candidate" in content
    assert ".research/REVIEW_VISUAL.svg" in presentation["machine_details"]["REVIEW_ARTIFACT"]


def test_gpt_delegated_review_does_not_waive_visual_gate(tmp_path: Path) -> None:
    presentation = build_human_presentation(
        metadata={
            "presentation_status": "HUMAN_GATE",
            "human_review_mode": "visual",
            "gpt_delegated_review": True,
        },
        canonical_state={"next_action": "APPLY_DECISION", "stage": {"stage_id": "stage-visual-123456", "status": "ACTIVE"}},
        artifact_root=tmp_path,
    )
    assert presentation["presentation_status"] == "HUMAN_REVIEW_PRESENTATION_INCOMPLETE"
    assert presentation["visual_artifact_required"] is True
    assert presentation["visual_artifact_present"] is False
    assert "presentation recovery" in presentation["human_summary"]["what_to_look_for"]


def test_machine_details_are_json_safe_and_stable() -> None:
    presentation = build_human_presentation(canonical_state={"stage": {"stage_id": "stage-12345678", "status": "CLOSED"}})
    encoded = json.dumps(presentation, ensure_ascii=False, sort_keys=True)
    assert "raw_response" not in encoded
    assert "cookie" not in encoded.casefold()
    assert list(presentation["machine_details"]) == list(MACHINE_DETAIL_KEYS)


class _RoutingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.exec_seen = False

    def __call__(self, argv, **kwargs):
        command = list(argv)
        self.calls.append(command)
        if command[1:3] == ["--version"]:
            return CommandResult(0, "codex-cli 0.154.0\n", "")
        if command[1:3] == ["login", "status"]:
            return CommandResult(0, "Logged in using ChatGPT\n", "")
        if command[1:3] == ["debug", "models"]:
            return CommandResult(0, "{}", "")
        if "exec" in command:
            self.exec_seen = True
            return CommandResult(
                0,
                '\n'.join(
                    [
                        '{"type":"thread.started"}',
                        '{"type":"turn.started"}',
                        '{"type":"item.completed","item":{"type":"command_execution","command":"pytest -q","exit_code":0}}',
                        '{"type":"turn.completed"}',
                    ]
                ),
                "",
            )
        if len(command) >= 4 and command[1] == "-C" and command[3] == "status":
            return CommandResult(0, " M tests/changed.py\n" if self.exec_seen else "", "")
        if len(command) >= 4 and command[1] == "-C" and command[3] == "diff":
            return CommandResult(0, "diff --git a/tests/changed.py b/tests/changed.py\n" if self.exec_seen else "", "")
        return CommandResult(0, "", "")


def _routing_request(tmp_path: Path, **metadata: object) -> ExecutionRequest:
    base = {
        "required_test_command": "pytest -q",
        "required_changed_path": "tests/changed.py",
        "parent_operation_id": "operation-parent-123456",
    }
    base.update(metadata)
    return ExecutionRequest(
        stage_id="stage-routing-123456",
        objective="run one bounded routing probe",
        iteration_index=1,
        allowed_paths=("tests",),
        protected_paths=(".git",),
        baseline_digest="baseline-routing-123456",
        action_map={"validated": True, "execute": True},
        plan_id="plan-routing-123456",
        task_id="task-routing-123456",
        workspace_root=str(tmp_path),
        metadata=base,
    )


def test_standard_route_records_requested_and_actual_binding(tmp_path: Path) -> None:
    runner = _RoutingRunner()
    provider = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=runner,
        model_discovery=lambda: [{"id": "acct-luna", "model": "gpt-5.6-luna", "displayName": "Luna"}],
    )
    result = provider.execute(_routing_request(tmp_path, parent_model="gpt-6-astra"))
    binding = result.evidence
    assert result.result["status"] == "SUCCEEDED"
    assert binding["requested_route"] == "STANDARD"
    assert binding["requested_model"] == "gpt-5.6-luna"
    assert binding["requested_reasoning_effort"] == "max"
    assert binding["actual_model"] == "acct-luna"
    assert binding["actual_reasoning_effort"] == "max"
    assert binding["parent_operation_id"] == "operation-parent-123456"
    assert binding["child_operation_id"].startswith("child-")
    assert binding["result_identity"].startswith("result-")
    exec_call = next(item for item in runner.calls if "exec" in item)
    assert exec_call[exec_call.index("--model") + 1] == "acct-luna"
    assert 'model_reasoning_effort="max"' in exec_call


def test_frontier_route_is_parent_model_independent_and_has_no_silent_fallback(tmp_path: Path) -> None:
    runner = _RoutingRunner()
    provider = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=runner,
        fallback_model="gpt-5.6-luna",
        model_discovery=lambda: [{"id": "acct-astra", "model": "gpt-6-astra", "displayName": "Astra"}],
    )
    result = provider.execute(
        _routing_request(
            tmp_path,
            execution_profile="FRONTIER",
            execution_profile_authority="authoritative_executor_request",
            parent_model="gpt-5.6-luna",
        )
    )
    assert result.result["status"] == "SUCCEEDED"
    assert result.evidence["requested_model"] == "gpt-6-astra"
    assert result.evidence["actual_model"] == "acct-astra"
    assert result.evidence["requested_reasoning_effort"] == "low"
    assert result.evidence["actual_reasoning_effort"] == "low"
    assert next(item for item in runner.calls if "exec" in item)[next(item for item in runner.calls if "exec" in item).index("--model") + 1] == "acct-astra"

    unavailable_runner = _RoutingRunner()
    unavailable = OpenAICodexExecutor(
        workspace_root=str(tmp_path),
        command_runner=unavailable_runner,
        fallback_model="gpt-5.6-luna",
        model_discovery=lambda: [{"id": "acct-luna", "model": "gpt-5.6-luna", "displayName": "Luna"}],
    ).execute(
        _routing_request(
            tmp_path,
            execution_profile="FRONTIER",
            execution_profile_authority="authoritative_executor_request",
        )
    )
    assert unavailable.result["status"] == "ERROR"
    assert unavailable.result["execution_failure"]["code"] == "MODEL_UNAVAILABLE"
    assert not any("exec" in item for item in unavailable_runner.calls)


def test_model_binding_is_present_in_metadata_only_run_receipt(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact_dir = tmp_path / "artifacts"
    runner = _RoutingRunner()
    provider = OpenAICodexExecutor(
        workspace_root=str(workspace),
        artifact_dir=str(artifact_dir),
        command_runner=runner,
        model_discovery=lambda: [{"id": "acct-luna", "model": "gpt-5.6-luna", "displayName": "Luna"}],
    )
    result = provider.execute(_routing_request(workspace))
    receipt = json.loads((artifact_dir / "run.json").read_text(encoding="utf-8"))
    for key in (
        "requested_route",
        "requested_model",
        "requested_reasoning_effort",
        "actual_model",
        "actual_reasoning_effort",
        "parent_operation_id",
        "child_operation_id",
        "result_identity",
    ):
        assert receipt[key] == result.evidence[key]
    assert receipt["prompt"]["raw_prompt_stored"] is False
