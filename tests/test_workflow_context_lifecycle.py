from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.artifact_retention import load_retention_manifest
from src.project_context import ProjectContextError, build_project_context, load_project_context
from src.workflow_runtime import (
    DEFAULT_CHECKPOINT_RELATIVE,
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


def _approved_runtime(root: Path) -> WorkflowRuntime:
    runtime = WorkflowRuntime(root)
    started = runtime.start(brief=_complete_brief())
    assert started["brief_state"] == "WAITING_USER_APPROVAL"
    approved = runtime.answer(mode="APPROVE")
    assert approved["brief_state"] == "APPROVED"
    return runtime


def _write_receipts(root: Path) -> None:
    discovery = root / ".research" / "discovery"
    discovery.mkdir(parents=True, exist_ok=True)
    (discovery / "CONSULTATION_STATE.json").write_text(
        json.dumps({"request_count": 1, "status": "COMPLETE"}) + "\n",
        encoding="utf-8",
    )
    receipts = root / ".research" / "consultations"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / "receipt.json").write_text(
        json.dumps({"status": "complete", "request_count": 1}) + "\n",
        encoding="utf-8",
    )


def test_brief_update_quarantines_derived_inputs_and_approval_allows_rebuild(tmp_path: Path) -> None:
    runtime = _approved_runtime(tmp_path)
    build_project_context(tmp_path, target="old approved context")
    (tmp_path / ".research" / "LOCAL_PROJECT_PROFILE.md").write_text("old profile\n", encoding="utf-8")
    (tmp_path / ".research" / "AVAILABLE_ASSETS.json").write_text("{\"old\": true}\n", encoding="utf-8")
    _write_receipts(tmp_path)
    old_checkpoint = (tmp_path / DEFAULT_CHECKPOINT_RELATIVE).read_bytes()

    updated = runtime.answer(mode="UPDATE", update={"project_goal": "new approved context"})
    assert updated["brief_state"] == "WAITING_USER_APPROVAL"
    assert updated["derived_artifact_quarantine"]["quarantined"] is True
    assert not (tmp_path / ".research" / "PROJECT_CONTEXT.md").exists()
    assert not (tmp_path / ".research" / "ARTIFACT_RETENTION_MANIFEST.json").exists()
    # Discovery/consultation receipts are not part of the explicit quarantine set.
    assert (tmp_path / ".research" / "discovery" / "CONSULTATION_STATE.json").is_file()
    assert (tmp_path / ".research" / "consultations" / "receipt.json").is_file()
    stale_path = tmp_path / updated["derived_artifact_quarantine"]["path"]
    invalidation = json.loads((stale_path / "INVALIDATION.json").read_text(encoding="utf-8"))
    assert invalidation["operation"] == "workflow_answer"
    assert invalidation["old_brief_revision"] < invalidation["new_brief_revision"]
    assert all("raw" not in key.casefold() for key in invalidation)
    assert all(item["relative_path"].startswith(".research/") for item in invalidation["files"])
    assert {item["relative_path"] for item in invalidation["files"]} == {
        ".research/PROJECT_CONTEXT.md",
        ".research/ARTIFACT_RETENTION_MANIFEST.json",
        ".research/LOCAL_PROJECT_PROFILE.md",
        ".research/AVAILABLE_ASSETS.json",
    }

    approved = runtime.answer(mode="APPROVE")
    assert approved["brief_state"] == "APPROVED"
    assert not (tmp_path / ".research" / "PROJECT_CONTEXT.md").exists()
    rebuilt = build_project_context(tmp_path)
    loaded = load_project_context(tmp_path)
    manifest = load_retention_manifest(tmp_path)
    assert rebuilt["brief_revision"] == approved["brief_revision"]
    assert loaded["brief_revision"] == approved["brief_revision"]
    assert manifest is not None
    assert manifest["brief_digest"] == loaded["brief_digest"]
    assert (tmp_path / DEFAULT_CHECKPOINT_RELATIVE).read_bytes() != old_checkpoint


def test_direct_context_tamper_is_not_automatically_rebuilt(tmp_path: Path) -> None:
    runtime = _approved_runtime(tmp_path)
    build_project_context(tmp_path)
    context_path = tmp_path / ".research" / "PROJECT_CONTEXT.md"
    original = context_path.read_text(encoding="utf-8")
    context_path.write_text(original + "tamper\n", encoding="utf-8")

    # Idempotent approval does not mutate the brief, so the lifecycle hook must
    # not treat an arbitrary load/tamper failure as permission to rebuild.
    result = runtime.answer(mode="APPROVE")
    assert result["derived_artifact_quarantine"] is None
    assert context_path.read_text(encoding="utf-8") == original + "tamper\n"
    with pytest.raises(ProjectContextError):
        load_project_context(tmp_path)
    assert not (tmp_path / ".research" / "stale").exists()


def test_quarantine_is_bounded_idempotent_and_rejects_stale_path_collision(tmp_path: Path) -> None:
    runtime = _approved_runtime(tmp_path)
    build_project_context(tmp_path)
    stale_root = tmp_path / ".research" / "stale"
    stale_root.write_text("not a directory\n", encoding="utf-8")
    checkpoint_path = tmp_path / DEFAULT_CHECKPOINT_RELATIVE
    checkpoint_before = checkpoint_path.read_bytes()
    brief_before = (tmp_path / ".research" / "PROJECT_BRIEF.json").read_bytes()
    context_before = (tmp_path / ".research" / "PROJECT_CONTEXT.md").read_bytes()

    with pytest.raises(WorkflowRuntimeError) as failure:
        runtime.answer(mode="UPDATE", update={"project_goal": "must fail closed"})
    assert failure.value.code == "DERIVED_ARTIFACT_QUARANTINE_FAILED"
    # The brief write happened before the guarded checkpoint boundary; the old
    # transaction is restored, including the old runnable checkpoint.
    assert checkpoint_path.read_bytes() == checkpoint_before
    assert (tmp_path / ".research" / "PROJECT_BRIEF.json").read_bytes() == brief_before
    assert (tmp_path / ".research" / "PROJECT_CONTEXT.md").read_bytes() == context_before
    assert stale_root.read_text(encoding="utf-8") == "not a directory\n"
    assert not list(tmp_path.glob(".research/stale/*"))


def test_quarantine_receipt_write_failure_rolls_back_and_does_not_advance_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _approved_runtime(tmp_path)
    build_project_context(tmp_path)
    checkpoint_path = tmp_path / DEFAULT_CHECKPOINT_RELATIVE
    checkpoint_before = checkpoint_path.read_bytes()
    brief_path = tmp_path / ".research" / "PROJECT_BRIEF.json"
    brief_before = brief_path.read_bytes()
    context_path = tmp_path / ".research" / "PROJECT_CONTEXT.md"
    context_before = context_path.read_bytes()

    import src.workflow_runtime as workflow_runtime_module

    def fail_receipt(*args: object, **kwargs: object) -> None:
        raise OSError("injected receipt write failure")

    monkeypatch.setattr(workflow_runtime_module, "_atomic_json_write", fail_receipt)
    with pytest.raises(WorkflowRuntimeError) as failure:
        runtime.answer(mode="UPDATE", update={"project_goal": "must not become ready"})
    assert failure.value.code == "DERIVED_ARTIFACT_QUARANTINE_FAILED"
    assert checkpoint_path.read_bytes() == checkpoint_before
    assert brief_path.read_bytes() == brief_before
    assert context_path.read_bytes() == context_before
    assert not list(tmp_path.glob(".research/stale/*"))


def test_checkpoint_write_failure_rolls_back_brief_quarantine_and_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _approved_runtime(tmp_path)
    build_project_context(tmp_path)
    checkpoint_path = tmp_path / DEFAULT_CHECKPOINT_RELATIVE
    checkpoint_before = checkpoint_path.read_bytes()
    brief_path = tmp_path / ".research" / "PROJECT_BRIEF.json"
    brief_before = brief_path.read_bytes()
    context_path = tmp_path / ".research" / "PROJECT_CONTEXT.md"
    context_before = context_path.read_bytes()

    def fail_checkpoint(*args: object, **kwargs: object) -> dict[str, object]:
        raise WorkflowRuntimeError("CHECKPOINT_WRITE_FAILED", "injected checkpoint failure")

    monkeypatch.setattr(runtime, "_save_checkpoint", fail_checkpoint)
    with pytest.raises(WorkflowRuntimeError) as failure:
        runtime.answer(mode="UPDATE", update={"project_goal": "checkpoint must fail closed"})
    assert failure.value.code == "CHECKPOINT_WRITE_FAILED"
    assert checkpoint_path.read_bytes() == checkpoint_before
    assert brief_path.read_bytes() == brief_before
    assert context_path.read_bytes() == context_before
    assert not list(tmp_path.glob(".research/stale/*"))


def test_initial_start_checkpoint_failure_removes_new_brief_and_leaves_no_stale_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = WorkflowRuntime(tmp_path)

    def fail_checkpoint(*args: object, **kwargs: object) -> dict[str, object]:
        raise WorkflowRuntimeError("CHECKPOINT_WRITE_FAILED", "injected initial checkpoint failure")

    monkeypatch.setattr(runtime, "_save_checkpoint", fail_checkpoint)
    with pytest.raises(WorkflowRuntimeError) as failure:
        runtime.start(brief=_complete_brief())
    assert failure.value.code == "CHECKPOINT_WRITE_FAILED"
    assert not (tmp_path / ".research" / "PROJECT_BRIEF.json").exists()
    assert not (tmp_path / DEFAULT_CHECKPOINT_RELATIVE).exists()
    assert not list(tmp_path.glob(".research/stale/*"))


def test_quarantine_rollback_failure_is_explicitly_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _approved_runtime(tmp_path)
    build_project_context(tmp_path)
    checkpoint_path = tmp_path / DEFAULT_CHECKPOINT_RELATIVE
    checkpoint_before = checkpoint_path.read_bytes()
    brief_path = tmp_path / ".research" / "PROJECT_BRIEF.json"
    brief_before = brief_path.read_bytes()

    import src.workflow_runtime as workflow_runtime_module

    def fail_receipt(*args: object, **kwargs: object) -> None:
        raise OSError("injected receipt write failure")

    monkeypatch.setattr(workflow_runtime_module, "_atomic_json_write", fail_receipt)
    monkeypatch.setattr(workflow_runtime_module, "_rollback_derived_moves", lambda moves: ["PROJECT_CONTEXT.md"])
    with pytest.raises(WorkflowRuntimeError) as failure:
        runtime.answer(mode="UPDATE", update={"project_goal": "rollback must be explicit"})
    assert failure.value.code == "WORKFLOW_TRANSACTION_ROLLBACK_FAILED"
    assert checkpoint_path.read_bytes() == checkpoint_before
    assert brief_path.read_bytes() == brief_before
    # The response is an explicit compound failure; no success/result object
    # is returned while the moved artifact remains in the stale transaction.
    assert list(tmp_path.glob(".research/stale/*"))
