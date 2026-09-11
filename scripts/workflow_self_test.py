#!/usr/bin/env python3
"""Run one disposable, deterministic GPT-first workflow self-test.

The harness exercises the existing local workflow layers in a fresh directory:

``prepare -> human start -> bounded architecture metadata -> ActionMap ->
fake Codex/Luna action -> recorded evidence -> fake GPT closeout -> routing``.

The architecture consultation is historical evidence.  The harness reads its
bounded receipt metadata and never copies the consultation response.  The
closeout bridge is a deliberately injected in-memory fake; its response is
consumed by :class:`StageIntegrationAdapter` and is never written to disk.
No facade, browser bridge, worker loop, or production source is invoked.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from src.human_artifacts import (  # noqa: E402
    CANONICAL_HUMAN_ARTIFACTS,
    write_human_artifacts,
)
from src.stage_controller import StageController, StageState  # noqa: E402
from src.stage_integration import StageIntegrationAdapter  # noqa: E402
from src.workflow_policy import (  # noqa: E402
    ActionMap,
    ActionMapError,
    ArtifactPolicy,
    ConsultationBudget,
    ConsultationBudgetError,
    NodeOwner,
    TransitionPolicy,
)


SELF_TEST_MARKER = "WORKFLOW_SELF_TEST_PASS"
SELF_TEST_SCHEMA = "workflow_self_test.v1"
ARCHITECTURE_CONSULTATION_ID = "CONSULT-20260906-070055-bef2fdb6"
ARCHITECTURE_CONVERSATION_ID = "6a9d0fc1-38f8-83ee-93e3-7e915ed17b76"
ARCHITECTURE_RECEIPT_RELATIVE = (
    f".consultations/{ARCHITECTURE_CONSULTATION_ID}/receipt.json"
)
_RAW_CLOSEOUT_SENTINEL = "SELF_TEST_FAKE_RAW_CLOSEOUT_RESPONSE_DO_NOT_PERSIST"
_FORBIDDEN_PERSISTED_KEYS = frozenset(
    {
        "prompt",
        "raw_prompt",
        "raw_response",
        "response",
        "response_text",
        "transcript",
        "messages",
        "cookies",
        "storage_state",
    }
)


class SelfTestError(RuntimeError):
    """Raised when the disposable workflow fails a self-test invariant."""


def _utc_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Write a bounded JSON artifact atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex[:8]}.tmp")
    temporary.write_text(_canonical_json(value) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SelfTestError(f"could not read JSON artifact: {path}") from exc
    if not isinstance(value, dict):
        raise SelfTestError(f"JSON artifact must be an object: {path}")
    return value


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= character <= "\u9fff" for character in text)


def _json_has_forbidden_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        if any(str(key).casefold() in _FORBIDDEN_PERSISTED_KEYS for key in value):
            return True
        return any(_json_has_forbidden_key(child) for child in value.values())
    if isinstance(value, list):
        return any(_json_has_forbidden_key(child) for child in value)
    return False


def _all_files(root: Path) -> list[Path]:
    return sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.as_posix(),
    )


def _assert_clean_run_path(repo_root: Path, run_dir: Path) -> Path:
    """Resolve a new run path and refuse accidental reuse or broad writes."""

    repo_root = repo_root.expanduser().resolve()
    tmp_root = (repo_root / ".tmp").resolve()
    run_dir = run_dir.expanduser().resolve()
    try:
        run_dir.relative_to(tmp_root)
    except ValueError as exc:
        raise SelfTestError("run directory must be below supervisor/.tmp") from exc
    if not run_dir.name.startswith("workflow_self_test_"):
        raise SelfTestError("run directory must be named workflow_self_test_<id>")
    if run_dir.exists():
        if not run_dir.is_dir() or any(run_dir.iterdir()):
            raise SelfTestError(f"run directory already exists and is not empty: {run_dir}")
    else:
        run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _load_architecture_receipt(repo_root: Path) -> tuple[dict[str, Any], str]:
    receipt_path = (repo_root / ARCHITECTURE_RECEIPT_RELATIVE).resolve()
    if not receipt_path.is_file():
        raise SelfTestError(f"required architecture receipt is missing: {receipt_path}")
    before_digest = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    receipt = _read_json(receipt_path)
    if receipt.get("consultation_id") != ARCHITECTURE_CONSULTATION_ID:
        raise SelfTestError("architecture receipt consultation id does not match the retained evidence")
    if receipt.get("conversation_id") != ARCHITECTURE_CONVERSATION_ID:
        raise SelfTestError("architecture receipt conversation id does not match the retained evidence")
    if receipt.get("status") != "complete" or receipt.get("request_count") != 1:
        raise SelfTestError("architecture receipt is not a completed one-request consultation")
    if receipt.get("project_scope_verified") is not True:
        raise SelfTestError("architecture receipt lacks verified Project scope")
    context_pack = receipt.get("context_pack")
    if not isinstance(context_pack, Mapping) or context_pack.get("attachment_count") != 5:
        raise SelfTestError("architecture receipt lacks the bounded five-attachment packet")
    bounded = {
        "consultation_id": ARCHITECTURE_CONSULTATION_ID,
        "receipt_id": ARCHITECTURE_CONSULTATION_ID,
        "receipt_path": ARCHITECTURE_RECEIPT_RELATIVE,
        "conversation_id": ARCHITECTURE_CONVERSATION_ID,
        "mode": "FRESH",
        "type": "FRESH_ARCHITECTURE",
        "status": "COMPLETE",
        "project_scope_verified": True,
        "context_pack_id": str(context_pack.get("packet_id")),
        "context_pack_sha256": str(context_pack.get("pack_sha256")),
        "attachment_count": 5,
        "receipt_sha256": before_digest,
    }
    return bounded, before_digest


def _stage_contract(run_dir: Path, run_id: str) -> dict[str, Any]:
    return {
        "schema_version": "stage_contract.v1",
        "plan_id": f"workflow-self-test-plan-{run_id}",
        "project_id": f"workflow-self-test-project-{run_id}",
        "repository_root": str(run_dir),
        "stage_id": "workflow-self-test-stage",
        "stage_name": "一次性 GPT-first workflow 自测",
        "project_goal": "验证有限 Stage workflow 的 GPT/Codex/Luna 职责边界",
        "stage_goal": "生成一个可复核的有界 evidence JSON 并经过 closeout 路由",
        "user_visible_goal": "展示可复核的单步证据和明确人工闸门",
        "inputs": ["SELF_TEST_RESULT.json"],
        "protected_paths": [".git", ".auth", "src", "facade", "bridge"],
        "allowed_paths": [
            "evidence",
            "artifacts",
            "SELF_TEST_RESULT.json",
            *CANONICAL_HUMAN_ARTIFACTS,
        ],
        "acceptance_description": "evidence JSON、检查和人工闸门路由均可复核",
        "required_checks": ["evidence_json", "bounded_action"],
        "review_artifact_requirements": ["metrics_summary"],
        "baseline": {"kind": "workflow_self_test", "metric": 1.0},
        "max_iterations": 1,
        "status": "PLANNED",
    }


def _artifact_stage_snapshot(
    stage: Mapping[str, Any], consultations: list[Mapping[str, Any]]
) -> dict[str, Any]:
    snapshot = copy.deepcopy(dict(stage))
    snapshot["consultations"] = copy.deepcopy(consultations)
    snapshot["decision_history"] = copy.deepcopy(consultations)
    return snapshot


def _fake_gpt_runner(
    run_dir: Path,
    run_id: str,
    decision: str,
    calls: list[dict[str, Any]],
):
    """Return a fake bridge callback that keeps its response in memory only."""

    receipt_path = run_dir / ".consultations" / "fake-closeout" / "receipt.json"
    consultation_id = f"CONSULT-SELFTEST-CLOSEOUT-{run_id}"
    conversation_id = f"conversation-self-test-{run_id}"

    def runner(prompt: str, **kwargs: Any) -> dict[str, Any]:
        # Keep only bounded call metadata.  The prompt and the response below
        # remain in this process and are deliberately absent from this record.
        calls.append(
            {
                "mode": kwargs.get("mode"),
                "continue_from": kwargs.get("continue_from"),
                "context_pack_id": (kwargs.get("context_pack") or {}).get("packet_id"),
            }
        )
        receipt = {
            "consultation_id": consultation_id,
            "conversation_id": conversation_id,
            "created_at": "self-test",
            "mode": "fresh",
            "request_count": 1,
            "status": "complete",
        }
        _write_json(receipt_path, receipt)
        response = (
            f"{_RAW_CLOSEOUT_SENTINEL}\n"
            f"WORKFLOW_DECISION: {decision}\n"
        )
        return {
            "consultation_id": consultation_id,
            "conversation_id": conversation_id,
            "request_count": 1,
            "response_text": response,
            "receipt_path": str(receipt_path),
            "receipt": receipt,
        }

    return runner, receipt_path, consultation_id, conversation_id


def _status_history(controller: StageController) -> list[str]:
    history: list[str] = []
    for event in controller.state.get("events", []):
        for key in ("from_status", "to_status"):
            value = event.get(key)
            if isinstance(value, str) and (not history or history[-1] != value):
                history.append(value)
    return history


def _check_files_and_privacy(
    run_dir: Path,
    *,
    human_review_before_gate: bool,
    architecture_receipt_path: Path,
    architecture_receipt_digest: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    policy = ArtifactPolicy()
    policy.validate(CANONICAL_HUMAN_ARTIFACTS)
    canonical_paths = {
        name: run_dir / name for name in CANONICAL_HUMAN_ARTIFACTS
    }
    singleton_ok = all(
        path.is_file()
        and len([candidate for candidate in run_dir.rglob(path.name) if candidate.is_file()]) == 1
        and path.parent == run_dir
        for path in canonical_paths.values()
    )
    chinese_ok = all(
        _contains_cjk(path.read_text(encoding="utf-8"))
        for path in canonical_paths.values()
    )
    final_files = _all_files(run_dir)
    raw_reply_persisted = any(
        _RAW_CLOSEOUT_SENTINEL in path.read_text(encoding="utf-8", errors="replace")
        for path in final_files
    )
    json_forbidden = False
    for path in final_files:
        if path.suffix.casefold() != ".json":
            continue
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        json_forbidden = json_forbidden or _json_has_forbidden_key(parsed)
    receipt_unchanged = (
        architecture_receipt_path.is_file()
        and hashlib.sha256(architecture_receipt_path.read_bytes()).hexdigest()
        == architecture_receipt_digest
    )
    checks = {
        "three_canonical_files_singletons": "PASS" if singleton_ok else "FAIL",
        "human_artifacts_contain_chinese": "PASS" if chinese_ok else "FAIL",
        "human_review_only_after_gate": "PASS" if not human_review_before_gate else "FAIL",
        "human_review_present_at_gate": "PASS" if (run_dir / "HUMAN_REVIEW.md").is_file() else "FAIL",
        "raw_reply_not_persisted": "PASS" if not raw_reply_persisted else "FAIL",
        "transport_fields_not_persisted": "PASS" if not json_forbidden else "FAIL",
        "architecture_receipt_unchanged": "PASS" if receipt_unchanged else "FAIL",
    }
    metrics = {
        "canonical_human_artifact_count": sum(path.is_file() for path in canonical_paths.values()),
        "canonical_human_artifact_singletons": sum(
            len([candidate for candidate in run_dir.rglob(path.name) if candidate.is_file()])
            for path in canonical_paths.values()
        ),
        "human_review_before_gate": human_review_before_gate,
        "human_review_after_gate": (run_dir / "HUMAN_REVIEW.md").is_file(),
        "evidence_json_count": len(list((run_dir / "evidence").glob("*.json"))),
        "architecture_receipt_reference": ARCHITECTURE_RECEIPT_RELATIVE,
    }
    return checks, metrics


def run_self_test(
    *,
    repo_root: str | os.PathLike[str] | None = None,
    run_dir: str | os.PathLike[str] | None = None,
    closeout_decision: str = "HUMAN_GATE",
) -> dict[str, Any]:
    """Run the disposable workflow and return the machine result mapping."""

    repository = Path(repo_root or SCRIPT_ROOT).expanduser().resolve()
    if closeout_decision not in {"HUMAN_GATE", "STAGE_READY"}:
        raise SelfTestError("closeout_decision must be HUMAN_GATE or STAGE_READY")
    run_id = _utc_run_id()
    if run_dir is None:
        requested_run = repository / ".tmp" / f"workflow_self_test_{run_id}"
    else:
        requested_run = Path(run_dir).expanduser()
        if not requested_run.is_absolute():
            requested_run = repository / requested_run
    run_path = _assert_clean_run_path(repository, requested_run)
    architecture, architecture_receipt_digest = _load_architecture_receipt(repository)
    architecture_receipt_path = repository / ARCHITECTURE_RECEIPT_RELATIVE
    stage_id = "workflow-self-test-stage"
    contract = _stage_contract(run_path, run_id)
    state_path = run_path / "stage-state.json"
    controller = StageController(contract, state_path=state_path)

    # Planning/prepare is persisted as PLANNED, then written to the two
    # non-review human artifacts.  HUMAN_REVIEW.md must not exist yet.
    planning = controller.show_stage(stage_id)
    planning_consultations = [
        {
            **architecture,
            # The retained receipt was a FRESH transport consultation, but the
            # artifact entry is an architecture decision record.  Keeping its
            # lifecycle mode distinct prevents the generic human-artifact
            # renderer from treating the historical review as a closeout gate.
            "mode": "ARCHITECTURE",
            "reason": "验证 GPT-first ownership、预算和 lifecycle/action gating",
            "question_summary": "确定 GPT/Codex/Luna 职责及最小 ActionMap self-test",
            "evidence_summary": architecture["context_pack_sha256"],
            "gpt_conclusion_summary": "接受职责分离、预算和 ActionMap 方向，进入受控 self-test",
            "gpt_recommendation_summary": "建议由 Codex/Luna 解释、校验并执行有界本地动作",
            "workflow_decision": "CONTINUE",
            "codex_disposition": "PARTIAL_ACCEPT",
            "resulting_action": "执行 ActionMap deterministic validation，再运行一次 fake Codex/Luna",
        }
    ]
    write_human_artifacts(
        run_path,
        event="stage_planning",
        stage_metadata=_artifact_stage_snapshot(planning, planning_consultations),
    )
    if (run_path / "HUMAN_REVIEW.md").exists():
        raise SelfTestError("HUMAN_REVIEW.md was created before a gate")
    human_review_before_gate = (run_path / "HUMAN_REVIEW.md").exists()

    # The start is an explicit human operation.  The policy's transition check
    # is recorded as a metric; the controller performs the actual transition.
    transition = TransitionPolicy()
    if not transition.requires_human(StageState.PLANNED.value, StageState.ACTIVE.value):
        raise SelfTestError("PLANNED -> ACTIVE did not require a human gate")
    started = controller.start_stage(
        stage_id,
        actor="self-test-human",
        rationale="一次性自测的显式人工启动",
    )
    started_stage = started["stage"]
    write_human_artifacts(
        run_path,
        event="start_stage",
        stage_metadata=_artifact_stage_snapshot(started_stage, planning_consultations),
    )

    # A bounded summary of the real architecture receipt is the only GPT
    # reasoning metadata injected into this run.  The receipt itself remains
    # outside the disposable run and is verified unchanged at the end.
    write_human_artifacts(
        run_path,
        event="consultation_completed",
        stage_metadata=_artifact_stage_snapshot(started_stage, planning_consultations),
        consultation_metadata=planning_consultations[0],
    )

    # Codex interprets the PARTIAL_ACCEPT recommendation as one allowlisted
    # local action.  Validation and enabling execution are separate states.
    action_map = ActionMap(
        actions={
            "run_local_checks": {
                "owner": NodeOwner.CODEX,
                "rationale": "由 Codex/Luna 执行一次有界本地检查",
            }
        },
        execute=False,
        owner=NodeOwner.CODEX,
    )
    action_execute_before_validation = action_map.can_execute()
    action_map.validate()
    action_execute_after_validation = action_map.can_execute()
    action_map.enable_execution()
    action_execute_after_enable = action_map.can_execute()
    if action_execute_before_validation or action_execute_after_validation or not action_execute_after_enable:
        raise SelfTestError("ActionMap did not follow execute=false -> validated -> execute=true")

    out_of_bounds_rejected = False
    try:
        ActionMap({"delete_repository": NodeOwner.CODEX}).validate()
    except ActionMapError:
        out_of_bounds_rejected = True
    if not out_of_bounds_rejected:
        raise SelfTestError("out-of-bounds ActionMap action was accepted")

    evidence_path = run_path / "evidence" / "iteration-1.json"
    action_calls: list[dict[str, Any]] = []

    def fake_codex_luna_action(
        _response: Mapping[str, Any] | None,
        request: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        iteration_index = int(request["iteration_index"])
        evidence = {
            "schema_version": "workflow_evidence.v1",
            "stage_id": stage_id,
            "run_id": run_id,
            "iteration_index": iteration_index,
            "executor": "fake-codex-luna",
            "checks": {"evidence_json": "PASS", "bounded_action": "PASS"},
            "metrics": {
                "bounded_steps": 1,
                "evidence_bytes": 0,
                "changed_files": 1,
            },
            "changed_files": ["evidence/iteration-1.json"],
            "summary": "fake Codex/Luna 单步执行生成 evidence JSON",
        }
        evidence["metrics"]["evidence_bytes"] = len(_canonical_json(evidence).encode("utf-8"))
        _write_json(evidence_path, evidence)
        action_calls.append(
            {
                "action": "run_local_checks",
                "owner": NodeOwner.CODEX.value,
                "evidence_path": _relative(evidence_path, run_path),
            }
        )
        return {
            "status": "SUCCEEDED",
            "stage_id": stage_id,
            "iteration_index": iteration_index,
            "summary": "fake Codex/Luna 单步执行已记录",
            "checks": evidence["checks"],
            "evidence": evidence,
        }

    runner_result: list[Mapping[str, Any]] = []

    def dispatch_action(name: str, spec: Any) -> Mapping[str, Any]:
        if name != "run_local_checks" or spec.owner is not NodeOwner.CODEX:
            raise SelfTestError("ActionMap dispatched an unexpected owner or action")
        result = adapter.execute_local_action(
            fake_codex_luna_action,
            objective="执行一次 bounded evidence JSON 检查",
            abstraction_layer="workflow-self-test",
            user_visible_improvement=True,
        )
        runner_result.append(result)
        return result

    # Build the adapter after the explicit start and before the one-step local
    # action.  It is injected with a fake closeout runner below; no real bridge
    # entry point is imported or called.
    closeout_calls: list[dict[str, Any]] = []
    fake_bridge, fake_receipt_path, fake_consultation_id, fake_conversation_id = _fake_gpt_runner(
        run_path,
        run_id,
        closeout_decision,
        closeout_calls,
    )
    adapter = StageIntegrationAdapter(
        controller,
        stage_id=stage_id,
        repository_root=run_path,
        bridge_runner=fake_bridge,
        receipt_root=run_path / ".consultations",
    )
    action_map.dispatch(dispatch_action)
    if len(action_calls) != 1 or not evidence_path.is_file() or not runner_result:
        raise SelfTestError("fake Codex/Luna did not produce and record exactly one evidence JSON")
    recorded_result = runner_result[0]["controller"]
    action_stage = controller.show_stage(stage_id)
    write_human_artifacts(
        run_path,
        event="planner_decision",
        stage_metadata=_artifact_stage_snapshot(action_stage, planning_consultations),
        consultation_metadata=planning_consultations[0],
    )
    if (run_path / "HUMAN_REVIEW.md").exists():
        raise SelfTestError("ordinary action/CONTINUE path created HUMAN_REVIEW.md")

    # Consume exactly one closeout budget before the fake GPT call.  A second
    # GPT-triggered consultation is rejected and never reaches the bridge.
    budget = ConsultationBudget()
    budget_request = budget.request("FRESH")
    closeout_handle = adapter.consult_gpt(
        "bounded closeout review; return one closed workflow decision",
        mode="FRESH",
        reason="单步 evidence 已生成，要求一次 closeout 路由判断",
        context_pack={
            "packet_id": f"PACK-SELFTEST-{run_id}",
            "resource_classes": ["stage_contract", "latest_result", "acceptance"],
        },
    )
    closeout_view = adapter.read_response(closeout_handle)
    repeat_budget_rejected = False
    repeat_budget_error = None
    try:
        budget.request("NORMAL", triggered_by_gpt=True)
    except ConsultationBudgetError as exc:
        repeat_budget_rejected = True
        repeat_budget_error = type(exc).__name__
    if not repeat_budget_rejected:
        raise SelfTestError("a second GPT-triggered consultation escaped ConsultationBudget")

    closeout_metadata = {
        "consultation_id": fake_consultation_id,
        "receipt_id": fake_consultation_id,
        "receipt_path": _relative(fake_receipt_path, run_path),
        "conversation_id": fake_conversation_id,
        "mode": "FRESH",
        "type": "FRESH_CLOSEOUT",
        "status": "COMPLETE",
        "evidence_digest": closeout_handle.evidence_digest,
        "reason": "单步 evidence 已生成，要求一次 closeout 路由判断",
        "question_summary": "是否进入人工闸门或 Stage Ready 审阅？",
        "gpt_conclusion_summary": f"fake GPT closeout decision={closeout_decision}",
        "workflow_decision": closeout_decision,
        "codex_disposition": "WAIT_FOR_HUMAN",
        "resulting_action": (
            "等待人工闸门处理"
            if closeout_decision == "HUMAN_GATE"
            else "等待人工审阅"
        ),
    }
    all_consultations = planning_consultations + [closeout_metadata]

    if closeout_view["decision"] == "HUMAN_GATE":
        routed = adapter.request_human_gate("closeout evidence 仍需人工确认")
        if routed["stage"]["status"] != StageState.ACTIVE.value:
            raise SelfTestError("HUMAN_GATE routing unexpectedly changed the Stage state")
        if routed["stage"].get("pending_human_gate") is None:
            raise SelfTestError("HUMAN_GATE routing did not leave a pending gate")
    else:
        routed = adapter.mark_stage_ready(
            required_checks={"evidence_json": "PASS", "bounded_action": "PASS"},
            review_artifacts={"metrics_summary": _relative(evidence_path, run_path)},
        )
        if routed["stage"]["status"] != StageState.STAGE_READY.value:
            raise SelfTestError("STAGE_READY routing did not enter STAGE_READY")
    final_stage = controller.show_stage(stage_id)
    final_artifact_stage = _artifact_stage_snapshot(final_stage, all_consultations)
    # The controller event is already captured in SELF_TEST_RESULT.json.  For
    # the final human surface, let the closeout consultation be the current
    # route record so GPT conclusion/Codex disposition/action remain together
    # in the visible decision log instead of being shadowed by a bare planner
    # event.
    final_artifact_stage["events"] = []
    write_human_artifacts(
        run_path,
        event="consultation_completed",
        stage_metadata=final_artifact_stage,
        consultation_metadata=closeout_metadata,
    )
    if not (run_path / "HUMAN_REVIEW.md").is_file():
        raise SelfTestError("controller gate did not create HUMAN_REVIEW.md")

    checks, metrics = _check_files_and_privacy(
        run_path,
        human_review_before_gate=human_review_before_gate,
        architecture_receipt_path=architecture_receipt_path,
        architecture_receipt_digest=architecture_receipt_digest,
    )
    checks.update(
        {
            "architecture_receipt_reference": "PASS",
            "codex_disposition_partial_accept": "PASS",
            "action_map_execute_false_to_true": "PASS",
            "out_of_bounds_action_rejected": "PASS" if out_of_bounds_rejected else "FAIL",
            "fake_codex_luna_single_step": "PASS" if len(action_calls) == 1 else "FAIL",
            "executor_result_recorded": "PASS" if recorded_result.get("decision") == "CONTINUE" else "FAIL",
            "closeout_controller_routed": "PASS" if closeout_view["decision"] == closeout_decision else "FAIL",
            "repeat_gpt_request_budget_rejected": "PASS" if repeat_budget_rejected else "FAIL",
            "decision_log_gpt_codex_action": (
                "PASS"
                if all(
                    token in (run_path / "RESEARCH_DECISION_LOG.md").read_text(encoding="utf-8")
                    for token in ("GPT", "PARTIAL_ACCEPT", "ActionMap", "Codex", "action")
                )
                else "FAIL"
            ),
        }
    )
    if any(value != "PASS" for value in checks.values()):
        raise SelfTestError(f"workflow self-test checks failed: {checks}")

    status_history = _status_history(controller)
    result: dict[str, Any] = {
        "schema_version": SELF_TEST_SCHEMA,
        "marker": SELF_TEST_MARKER,
        "run_id": run_id,
        "run_path": str(run_path),
        "stage_id": stage_id,
        "final_status": final_stage["status"],
        "pending_human_gate": final_stage.get("pending_human_gate"),
        "status_history": status_history,
        "metrics": {
            **metrics,
            "architecture_consultation_count": 1,
            "fake_closeout_consultation_count": len(closeout_calls),
            "total_fake_bridge_calls": len(closeout_calls),
            "action_dispatch_count": len(action_calls),
            "executor_result_iteration": final_stage.get("iteration_index"),
            "evidence_digest": adapter.latest_evidence.digest if adapter.latest_evidence else None,
            "budget_request_count": budget.total_count,
            "budget_rejected_repeat_count": 1 if repeat_budget_rejected else 0,
        },
        "checks": checks,
        "architecture_consultation": architecture,
        "closeout_consultation": {
            key: value
            for key, value in closeout_metadata.items()
            if key not in {"gpt_conclusion_summary"}
        },
        "codex_disposition": "PARTIAL_ACCEPT",
        "action_map": {
            "execute_before_validation": action_execute_before_validation,
            "execute_after_validation": action_execute_after_validation,
            "execute_after_enable": action_execute_after_enable,
            "executable_after_enable": action_map.executable,
            "out_of_bounds_rejected": out_of_bounds_rejected,
            "validated": action_map.validated,
        },
        "budget": {
            "initial_request": budget_request,
            "repeat_rejected": repeat_budget_rejected,
            "repeat_error": repeat_budget_error,
            **budget.to_dict(),
        },
        "evidence": {
            "path": _relative(evidence_path, run_path),
            "json": _read_json(evidence_path),
            "action_calls": action_calls,
        },
        "files": [],
    }
    result_path = run_path / "SELF_TEST_RESULT.json"
    result["files"] = [
        _relative(path, run_path)
        for path in _all_files(run_path)
        if path != result_path
    ] + ["SELF_TEST_RESULT.json"]
    _write_json(result_path, result)
    # The machine result itself is included in the final privacy scan; this
    # guards against accidentally adding raw transport material to its schema.
    final_serialized = result_path.read_text(encoding="utf-8")
    if _RAW_CLOSEOUT_SENTINEL in final_serialized or "response_text" in final_serialized:
        raise SelfTestError("SELF_TEST_RESULT.json contains a raw closeout response field")
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        type=Path,
        default=SCRIPT_ROOT,
        help="supervisor repository containing the retained architecture receipt",
    )
    parser.add_argument(
        "--run-dir",
        type=Path,
        default=None,
        help="empty supervisor/.tmp/workflow_self_test_<id> directory to use",
    )
    parser.add_argument(
        "--closeout-decision",
        choices=("HUMAN_GATE", "STAGE_READY"),
        default="HUMAN_GATE",
        help="bounded fake GPT closeout decision (default: HUMAN_GATE)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = run_self_test(
            repo_root=args.repo,
            run_dir=args.run_dir,
            closeout_decision=args.closeout_decision,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports one bounded failure
        print(f"SELF_TEST_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"SELF_TEST_RESULT={result['run_path']}/SELF_TEST_RESULT.json")
    print(_canonical_json({
        "marker": result["marker"],
        "run_path": result["run_path"],
        "stage_id": result["stage_id"],
        "final_status": result["final_status"],
        "status_history": result["status_history"],
        "checks": result["checks"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
