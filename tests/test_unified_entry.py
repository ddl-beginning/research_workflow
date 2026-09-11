from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.unified_entry import (
    dispatch_once,
    resolve_unified_entry,
)


def _registry() -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for capability, actor in (
        ("requirements.intake", "GPT"),
        ("requirements.confirmation", "Human"),
        ("research", "GPT"),
        ("design.synthesis", "GPT"),
        ("design.human_review", "Human"),
        ("stage.planning", "existing owner"),
        ("stage.execution", "Codex"),
        ("review.technical", "GPT"),
        ("integration", "Codex"),
        ("verification", "Codex"),
    ):
        result[capability] = {
            "actor": actor,
            "contract_ref": f"{capability}@1.0",
            "contract_version": "1.0",
            "machine_schema_version": "workflow_machine.v1",
            "policy": {},
        }
    return result


def _runtime(
    root: Path,
    *,
    project_id: str = "project-1",
    workflow_id: str = "workflow-1",
    goal: str = "a bounded goal",
    workflow: dict[str, object] | None = None,
) -> None:
    research = root / ".research"
    research.mkdir(parents=True, exist_ok=True)
    brief_state = workflow.get("brief_state", "APPROVED") if workflow else "APPROVED"
    (research / "PROJECT_BRIEF.json").write_text(
        json.dumps({"project_id": project_id, "state": brief_state, "goal": goal}),
        encoding="utf-8",
    )
    state: dict[str, object] = {
        "schema_version": "workflow_checkpoint.v1",
        "project_id": project_id,
        "workflow_id": workflow_id,
        "workspace_root": root.as_posix(),
        "brief_state": "APPROVED",
        "phase": "READY",
        "research_complete": False,
    }
    if workflow:
        state.update(workflow)
    (research / "workflow-state.json").write_text(json.dumps(state), encoding="utf-8")


def _handoff(anchor: Path, workspace: Path, **extra: object) -> None:
    payload: dict[str, object] = {
        "project_id": "project-1",
        "workflow_id": "workflow-1",
        "workspace_ref": workspace.as_posix(),
    }
    payload.update(extra)
    (anchor / "PROJECT_HANDOFF.json").write_text(json.dumps(payload), encoding="utf-8")


def test_new_short_entry_routes_without_using_anchor_research(tmp_path: Path) -> None:
    anchor = tmp_path / "project"
    anchor.mkdir()
    # A .research directory at the anchor is an intentionally misleading
    # filesystem hint.  No handoff/identity binding points to it.
    (anchor / ".research").mkdir()
    result = resolve_unified_entry(
        anchor,
        intent="new",
        goal="start a bounded goal",
        canonical_registry=_registry(),
    )

    assert result["entry_outcome"] == "ROUTE"
    assert result["mode"] == "NEW"
    assert result["workspace_ref"] is None
    assert result["next_capability"] == "requirements.intake"
    assert result["next_actor"] == "GPT"


def test_resume_uses_handoff_identity_before_runtime_path(tmp_path: Path) -> None:
    anchor = tmp_path / "project"
    runtime = tmp_path / "runtime"
    anchor.mkdir()
    _runtime(runtime)
    _handoff(anchor, runtime)

    result = resolve_unified_entry(anchor, intent="continue", canonical_registry=_registry())

    assert result["entry_outcome"] == "ROUTE"
    assert result["mode"] == "RESUME"
    assert result["project_id"] == "project-1"
    assert result["workflow_ref"] == "workflow-1"
    assert result["workspace_ref"] == runtime.resolve().as_posix()
    assert result["authority"]["identity_before_path"] is True


def test_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    anchor = tmp_path / "project"
    runtime = tmp_path / "runtime"
    anchor.mkdir()
    _runtime(runtime, project_id="different-project")
    _handoff(anchor, runtime)

    result = resolve_unified_entry(anchor, intent="continue", canonical_registry=_registry())

    assert result["entry_outcome"] == "BLOCKED"
    assert result["blocked_code"] == "PROJECT_IDENTITY_CONFLICT"


def test_review_routing_reads_stage_evidence_and_reuses_contract(tmp_path: Path) -> None:
    anchor = tmp_path / "project"
    runtime = tmp_path / "runtime"
    anchor.mkdir()
    _runtime(
        runtime,
        workflow={
            "phase": "RUNNING",
            "research_complete": True,
            "design_review_state": "ACCEPTED",
            "stage_state": {
                "status": "ACTIVE",
                "execution_evidence_complete": True,
            },
        },
    )
    _handoff(anchor, runtime)

    result = resolve_unified_entry(anchor, intent="continue", canonical_registry=_registry())

    assert result["entry_outcome"] == "ROUTE"
    assert result["current_position"] == "technical_review"
    assert result["next_capability"] == "review.technical"
    assert result["next_actor"] == "GPT"
    assert result["canonical_contract"]["contract_ref"] == "review.technical@1.0"


def test_two_valid_workspace_candidates_require_one_minimal_human_choice(tmp_path: Path) -> None:
    anchor = tmp_path / "project"
    first = tmp_path / "runtime-a"
    second = tmp_path / "runtime-b"
    anchor.mkdir()
    _runtime(first, workflow_id="workflow-a")
    _runtime(second, workflow_id="workflow-b")
    (anchor / "PROJECT_HANDOFF.json").write_text(
        json.dumps(
            {
                "project_id": "project-1",
                "workspace_candidates": [
                    {"project_id": "project-1", "workflow_id": "workflow-a", "workspace_ref": first.as_posix()},
                    {"project_id": "project-1", "workflow_id": "workflow-b", "workspace_ref": second.as_posix()},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = resolve_unified_entry(anchor, intent="continue", canonical_registry=_registry())

    assert result["entry_outcome"] == "HUMAN_INPUT_REQUIRED"
    assert result["question_id"] == "workspace_candidate"
    assert "请选择 A / B" in result["question"]


def test_repeated_new_goal_recovers_existing_workflow_as_resume(tmp_path: Path) -> None:
    anchor = tmp_path / "project"
    runtime = tmp_path / "runtime"
    anchor.mkdir()
    first = resolve_unified_entry(
        anchor,
        intent="new",
        goal="a bounded goal",
        canonical_registry=_registry(),
    )
    assert first["mode"] == "NEW"

    _runtime(runtime, goal="a bounded goal")
    _handoff(anchor, runtime)
    second = resolve_unified_entry(
        anchor,
        intent="new",
        goal="a bounded goal",
        canonical_registry=_registry(),
    )
    assert second["entry_outcome"] == "ROUTE"
    assert second["mode"] == "RESUME"
    assert second["workflow_ref"] == "workflow-1"


def test_dispatch_once_calls_downstream_at_most_once() -> None:
    calls: list[dict[str, object]] = []

    result = dispatch_once(
        {"entry_outcome": "ROUTE", "next_capability": "research"},
        lambda route: calls.append(dict(route)) or {"status": "CONTINUE"},
    )

    assert len(calls) == 1
    assert result["dispatch_performed"] is True
    assert result["invocation"]["status"] == "CONTINUE"


def test_continue_without_trusted_workflow_is_human_input_required(tmp_path: Path) -> None:
    anchor = tmp_path / "project"
    anchor.mkdir()

    result = resolve_unified_entry(anchor, intent="continue", canonical_registry=_registry())

    assert result["entry_outcome"] == "HUMAN_INPUT_REQUIRED"
    assert result["question_id"] == "missing_workflow"


@pytest.mark.parametrize(
    ("state", "capability", "actor"),
    [
        ({"brief_state": "WAITING_USER_APPROVAL"}, "requirements.confirmation", "Human"),
        ({"brief_state": "APPROVED", "research_complete": True}, "design.synthesis", "GPT"),
    ],
)
def test_derived_route_can_use_existing_capability_metadata(
    tmp_path: Path,
    state: dict[str, object],
    capability: str,
    actor: str,
) -> None:
    anchor = tmp_path / "project"
    runtime = tmp_path / "runtime"
    anchor.mkdir()
    _runtime(runtime, workflow=state)
    _handoff(anchor, runtime)
    result = resolve_unified_entry(anchor, intent="continue", canonical_registry=_registry())
    assert result["entry_outcome"] == "ROUTE"
    assert result["next_capability"] == capability
    assert result["next_actor"] == actor
