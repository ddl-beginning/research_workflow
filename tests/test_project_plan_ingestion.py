from __future__ import annotations

from pathlib import Path

from scripts.workflow import build_parser, run_command
from src.project_intake import ProjectRequirementsIntake
from src.project_plan_ingestion import (
    CURRENT_STATE_RELATIVE_PATH,
    WORKFLOW_PLAN_RELATIVE_PATH,
    ProjectPlanIngestionError,
    detect_plan_sources,
    load_project_plan,
    sync_project_plan,
)
from src.workflow_v2_controller import StageController
from src.workflow_v2_contracts import assess_observation, observation_identity


def _write_plan(root: Path, *, stage_two: bool = True) -> None:
    plan = root / "plan"
    plan.mkdir(parents=True, exist_ok=True)
    (plan / "REQUIREMENTS.md").write_text(
        """# Final Project Goal
Build a bounded, testable project result.

## Scope
- local source and tests

## Non-goals
- no external deployment

## Final Output
- a verified local result

## Quality / Acceptance
- focused tests pass
- the final artifact is reproducible

## Business Constraints
- no secrets in generated files
""",
        encoding="utf-8",
    )
    second = (
        """
## S2 - Verify
- Goal: verify the result
- Tasks:
  - run the verification tests
- Expected Outputs:
  - verification report
- Machine Acceptance:
  - tests pass
- Human Gate Required: NO
"""
        if stage_two
        else ""
    )
    (plan / "STAGE_PLAN.md").write_text(
        """# Stage Plan

## S1 - Build
- Goal: build the bounded result
- Inputs: the local project
- Tasks:
  - implement the result
- Expected Outputs:
  - a local artifact
- Machine Acceptance:
  - focused tests pass
- Human Acceptance: no visual review
- Human Gate Required: NO
"""
        + second,
        encoding="utf-8",
    )


def _controller(root: Path) -> StageController:
    return StageController(
        workspace_id="workspace-" + root.name + "-12345678",
        state_path=root / ".workflow-v2" / "journal.json",
    )


def _complete_stage(controller: StageController, stage_id: str) -> None:
    request = controller.request_execution(
        stage_id,
        command_id="command-plan-test-request-" + stage_id,
        request={"work": "bounded"},
        provenance={"provider": "fixture-provider", "engine_digest": "engine-12345678"},
    )
    attempt = request["attempt"]
    observation = {
        "schema_version": "provider_observation.v2",
        "observation_id": "observation-plan-test-" + stage_id,
        "stage_id": stage_id,
        "iteration_id": attempt["iteration_id"],
        "attempt_id": attempt["attempt_id"],
        "provider_result_digest": "provider-plan-test-" + stage_id,
        "evidence_manifest_digest": "manifest-plan-test-" + stage_id,
        "provider_terminal_status": "SUCCEEDED",
        "raw_provider_claim": {"status": "SUCCEEDED"},
        "provenance": {"provider": "fixture-provider", "engine_digest": "engine-12345678"},
        "failure": None,
        "outputs": {"files": ["artifacts/result.json"]},
        "immutable": True,
    }
    observation["observation_id"] = observation_identity(observation)
    controller.record_observation(
        stage_id,
        observation,
        effect_state="SETTLED",
        command_id="command-plan-test-observe-" + stage_id,
    )
    manifest = {
        "schema_version": "evidence_manifest.v2",
        "manifest_id": observation["evidence_manifest_digest"],
        "stage_id": stage_id,
        "attempt_id": attempt["attempt_id"],
        "required_artifact_paths": ["artifacts/result.json"],
        "changed_paths": ["artifacts/result.json"],
        "allowed_paths": ["artifacts"],
        "protected_paths": [".git"],
        "path_inventory": [{"path": "artifacts/result.json", "sha256": "artifact-plan-12345678"}],
        "complete": True,
    }
    assessment = assess_observation(
        observation,
        manifest,
        baseline_digest=controller.resolve_canonical_stage(stage_id)["baseline_digest"],
        validator_code_digest="validator-plan-12345678",
        validation_contract_revision="admission.v2",
    )
    controller.assess_result(stage_id, assessment, command_id="command-plan-test-assess-" + stage_id)
    decision = {
        "schema_version": "decision.v2",
        "decision_id": "decision-plan-test-" + stage_id,
        "actor_kind": "GPT",
        "boundary": "TECHNICAL_REVIEW",
        "subject_id": assessment["assessment_id"],
        "subject_digest": assessment["assessment_id"],
        "subject_version": 1,
        "allowed_choices": ["STAGE_READY"],
        "requested_action": "APPLY_GPT_DECISION",
        "provenance": {
            "request_count": 1,
            "conversation_id": "conversation-plan-12345678",
            "response_digest": "response-plan-12345678",
            "packet_digest": "packet-plan-12345678",
        },
        "supersedes": None,
    }
    controller.apply_gpt_decision(
        stage_id,
        decision,
        choice="STAGE_READY",
        command_id="command-plan-test-ready-" + stage_id,
    )
    integration = controller.commit_integration(
        stage_id,
        command_id="command-plan-test-integrate-" + stage_id,
        target_manifest_digest="target-plan-12345678",
    )
    controller.apply_receipt(
        stage_id,
        command_id="command-plan-test-receipt-" + stage_id,
        operation_id=integration["operation"]["operation_id"],
        effect_state="SETTLED",
        settlement_proof="settled-plan-12345678",
        receipt={"verified": True},
    )
    controller.closeout(stage_id, command_id="command-plan-test-close-" + stage_id)


def test_detects_requirements_and_stage_plan(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    result = detect_plan_sources(tmp_path)
    assert result["status"] == "READY"
    assert result["missing"] == []


def test_missing_one_plan_source_reports_exact_missing_file(tmp_path: Path) -> None:
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "REQUIREMENTS.md").write_text("# Goal\nA goal\n", encoding="utf-8")
    result = detect_plan_sources(tmp_path)
    assert result["status"] == "INCOMPLETE"
    assert result["missing"] == ["plan/STAGE_PLAN.md"]
    try:
        load_project_plan(tmp_path)
    except ProjectPlanIngestionError as exc:
        assert exc.code == "PLAN_INPUT_MISSING"
        assert "plan/STAGE_PLAN.md" in str(exc)
    else:
        raise AssertionError("missing plan source did not fail closed")


def test_cli_fixed_discovery_reports_exact_missing_file(tmp_path: Path) -> None:
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "STAGE_PLAN.md").write_text("# Stage Plan\n", encoding="utf-8")
    args = build_parser().parse_args(["--project", str(tmp_path), "--json"])
    code, result = run_command(args)
    assert code == 1
    assert result["code"] == "PLAN_INPUT_MISSING"
    assert result["missing"] == ["plan/REQUIREMENTS.md"]


def test_cli_init_reports_plan_gap_before_machine_setup(tmp_path: Path) -> None:
    (tmp_path / "plan").mkdir()
    (tmp_path / "plan" / "REQUIREMENTS.md").write_text("# Goal\nA goal\n", encoding="utf-8")
    args = build_parser().parse_args(["init", "--project", str(tmp_path), "--json"])
    code, result = run_command(args)
    assert code == 1
    assert result["code"] == "PLAN_INPUT_MISSING"
    assert result["missing"] == ["plan/STAGE_PLAN.md"]


def test_natural_language_stage_headings_receive_stable_slugs(tmp_path: Path) -> None:
    _write_plan(tmp_path, stage_two=False)
    (tmp_path / "plan" / "STAGE_PLAN.md").write_text(
        """# Stage Plan

## Prepare data
- Goal: prepare the input

## Verify output
- Goal: verify the output
""",
        encoding="utf-8",
    )
    first = [item["stage_id"] for item in load_project_plan(tmp_path)["stages"]]
    second = [item["stage_id"] for item in load_project_plan(tmp_path)["stages"]]
    assert first == ["stage-prepare-data", "stage-verify-output"]
    assert second == first


def test_plan_sources_generate_workflow_plan(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    result = sync_project_plan(tmp_path)
    assert result["workflow_plan_generated"] is True
    assert (tmp_path / WORKFLOW_PLAN_RELATIVE_PATH).is_file()
    generated = (tmp_path / WORKFLOW_PLAN_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "Project Goal" in generated
    assert "Stage Goal" in generated
    assert "SOURCE_STAGE_SHA256:" in generated


def test_plan_sources_generate_current_state(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    result = sync_project_plan(tmp_path, controller=controller)
    assert result["stage_started"] is True
    state = (tmp_path / CURRENT_STATE_RELATIVE_PATH).read_text(encoding="utf-8")
    assert state.startswith("# Current State\n\nDERIVED HUMAN-READABLE SNAPSHOT")
    assert "Canonical runtime authority remains:" in state
    assert "Current Stage: stage-s1" in state


def test_empty_project_e2e(tmp_path: Path) -> None:
    """Disposable E2E A: two user files are enough to enter S1."""

    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    result = sync_project_plan(tmp_path, controller=controller)
    assert result["status"] == "INGESTED"
    assert result["registered_stage_ids"] == ["stage-s1", "stage-s2"]
    assert result["stage_started"] is True
    assert controller.resume_projection()["stage"]["stage_id"] == "stage-s1"
    assert (tmp_path / "plan" / "WORKFLOW_PLAN.md").is_file()
    assert (tmp_path / "plan" / "CURRENT_STATE.md").is_file()
    assert result["human_intervention_count"] == 0


def test_current_state_is_derived_not_authoritative(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    sync_project_plan(tmp_path, controller=controller)
    (tmp_path / CURRENT_STATE_RELATIVE_PATH).write_text("Current Stage: forged-stage\n", encoding="utf-8")
    revision = controller.revision
    sync_project_plan(tmp_path, controller=controller)
    state = (tmp_path / CURRENT_STATE_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "Current Stage: stage-s1" in state
    assert "forged-stage" not in state
    assert controller.revision == revision


def test_missing_current_state_is_regenerated(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    sync_project_plan(tmp_path, controller=controller)
    (tmp_path / CURRENT_STATE_RELATIVE_PATH).unlink()
    result = sync_project_plan(tmp_path, controller=controller)
    assert result["current_state_written"] is True
    assert (tmp_path / CURRENT_STATE_RELATIVE_PATH).is_file()


def test_missing_workflow_plan_is_regenerated(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    sync_project_plan(tmp_path)
    (tmp_path / WORKFLOW_PLAN_RELATIVE_PATH).unlink()
    result = sync_project_plan(tmp_path)
    assert result["workflow_plan_written"] is True
    assert (tmp_path / WORKFLOW_PLAN_RELATIVE_PATH).is_file()


def test_stage_completion_updates_current_state(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    sync_project_plan(tmp_path, controller=controller)
    _complete_stage(controller, "stage-s1")
    result = sync_project_plan(tmp_path, controller=controller)
    assert result["stage_started"] is True
    state = (tmp_path / CURRENT_STATE_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "Completed Stages: stage-s1" in state
    assert "Current Stage: stage-s2" in state


def test_stage_completion_auto_enters_next_stage(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    sync_project_plan(tmp_path, controller=controller)
    _complete_stage(controller, "stage-s1")
    sync_project_plan(tmp_path, controller=controller)
    assert controller.resolve_canonical_stage("stage-s1")["status"] == "CLOSED"
    assert controller.resolve_canonical_stage("stage-s2")["status"] == "ACTIVE"


def test_human_approve_auto_enters_next_stage(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    result = sync_project_plan(tmp_path, controller=controller)
    assert result["auto_approved_plan_intake"] is True
    assert controller.resume_projection()["next_action"] == "REQUEST_EXECUTION"


def test_stage_dataset_path_is_loaded_from_stage_plan(tmp_path: Path) -> None:
    _write_plan(tmp_path, stage_two=False)
    stage_plan = tmp_path / "plan" / "STAGE_PLAN.md"
    stage_plan.write_text(stage_plan.read_text(encoding="utf-8") + "\n- Data: data/input.json\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "input.json").write_text("{}", encoding="utf-8")
    result = sync_project_plan(tmp_path, controller=_controller(tmp_path))
    checks = result["stage_data_validation"]
    assert any(item["path"] == "data/input.json" and item["exists"] is True for item in checks)


def test_stage_owned_missing_data_auto_generates(tmp_path: Path) -> None:
    _write_plan(tmp_path, stage_two=False)
    stage_plan = tmp_path / "plan" / "STAGE_PLAN.md"
    stage_plan.write_text(stage_plan.read_text(encoding="utf-8") + "\n- Data: generated/scene.json\n- Stage-owned: generated by this Stage\n", encoding="utf-8")
    result = sync_project_plan(tmp_path, controller=_controller(tmp_path))
    check = next(item for item in result["stage_data_validation"] if item["path"] == "generated/scene.json")
    assert check["exists"] is False
    assert check["classification"] == "STAGE_OWNED_WORK"
    assert check["next_action"] == "GENERATE_STAGE_OWNED_INPUT"


def test_existing_project_aligns_plan_with_journal(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    first = sync_project_plan(tmp_path, controller=controller)
    second = sync_project_plan(tmp_path, controller=controller)
    assert first["registered_stage_ids"] == ["stage-s1", "stage-s2"]
    assert second["registered_stage_ids"] == []
    assert controller.resolve_canonical_stage("stage-s1")["status"] == "ACTIVE"


def test_existing_project_does_not_restart_completed_stage(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    sync_project_plan(tmp_path, controller=controller)
    _complete_stage(controller, "stage-s1")
    sync_project_plan(tmp_path, controller=controller)
    revision = controller.revision
    sync_project_plan(tmp_path, controller=controller)
    assert controller.resolve_canonical_stage("stage-s1")["status"] == "CLOSED"
    assert controller.resolve_canonical_stage("stage-s2")["status"] == "ACTIVE"
    assert controller.revision == revision


def test_existing_project_e2e(tmp_path: Path) -> None:
    """Disposable E2E B: canonical journal history is resumed after restart."""

    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    sync_project_plan(tmp_path, controller=controller)
    _complete_stage(controller, "stage-s1")
    restarted = StageController.from_state(tmp_path / ".workflow-v2" / "journal.json")
    result = sync_project_plan(tmp_path, controller=restarted)
    assert result["registered_stage_ids"] == []
    assert restarted.resume_projection()["stage"]["stage_id"] == "stage-s2"
    assert restarted.resolve_canonical_stage("stage-s1")["status"] == "CLOSED"
    assert restarted.resolve_canonical_stage("stage-s2")["status"] == "ACTIVE"


def test_plan_source_change_is_detected(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    sync_project_plan(tmp_path, controller=controller)
    stage_plan = tmp_path / "plan" / "STAGE_PLAN.md"
    stage_plan.write_text(stage_plan.read_text(encoding="utf-8").replace("verify the result", "verify the changed result"), encoding="utf-8")
    result = sync_project_plan(tmp_path, controller=controller)
    assert result["plan_change"]["changed"] is True
    assert result["plan_change"]["impact"] == "FUTURE_STAGES"
    assert result["plan_change"]["technical_review_required"] is False


def test_future_stage_plan_change_auto_updates(tmp_path: Path) -> None:
    test_plan_source_change_is_detected(tmp_path)
    generated = (tmp_path / WORKFLOW_PLAN_RELATIVE_PATH).read_text(encoding="utf-8")
    assert "changed result" in generated


def test_completed_stage_semantic_change_requires_review(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    sync_project_plan(tmp_path, controller=controller)
    _complete_stage(controller, "stage-s1")
    sync_project_plan(tmp_path, controller=controller)
    stage_plan = tmp_path / "plan" / "STAGE_PLAN.md"
    stage_plan.write_text(stage_plan.read_text(encoding="utf-8").replace("build the bounded result", "replace the completed result"), encoding="utf-8")
    result = sync_project_plan(tmp_path, controller=controller)
    assert result["plan_change"]["impact"] == "COMPLETED_STAGE"
    assert result["plan_change"]["technical_review_required"] is True


def test_no_plan_folder_preserves_legacy_behavior(tmp_path: Path) -> None:
    result = sync_project_plan(tmp_path)
    assert result["status"] == "LEGACY_COMPATIBLE"
    assert not (tmp_path / "plan").exists()


def test_context_resume_reload_plan(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    sync_project_plan(tmp_path, controller=controller)
    (tmp_path / CURRENT_STATE_RELATIVE_PATH).unlink()
    (tmp_path / WORKFLOW_PLAN_RELATIVE_PATH).unlink()
    reloaded = StageController.from_state(tmp_path / ".workflow-v2" / "journal.json")
    result = sync_project_plan(tmp_path, controller=reloaded)
    assert result["context_recovery_order"][:4] == ["plan/REQUIREMENTS.md", "plan/STAGE_PLAN.md", "plan/WORKFLOW_PLAN.md", "journal"]
    assert reloaded.resume_projection()["stage"]["stage_id"] == "stage-s1"
    assert (tmp_path / CURRENT_STATE_RELATIVE_PATH).is_file()
    assert (tmp_path / WORKFLOW_PLAN_RELATIVE_PATH).is_file()


def test_plan_intake_uses_existing_canonical_brief_identity(tmp_path: Path) -> None:
    _write_plan(tmp_path)
    controller = _controller(tmp_path)
    first = sync_project_plan(tmp_path, controller=controller)
    project_id = first["canonical"].get("stage", {}).get("project_id")
    intake = ProjectRequirementsIntake(tmp_path)
    assert intake.state is not None
    assert intake.state["project_id"] == project_id
