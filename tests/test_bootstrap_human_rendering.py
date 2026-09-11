from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from src.human_artifacts import build_human_review, sanitize_bounded_evidence
from src.workflow_runtime import WorkflowRuntime, _digest


def _bootstrap_evidence(*, complete: bool = True) -> dict[str, object]:
    return {
        "status": "VERIFIED" if complete else "PARTIAL",
        "validated": complete,
        "brief": {
            "project_goal": "通过最小变更验证 workflow 闭环",
            "observable_outcome": "目标测试 PASS 且 evidence 可审查",
            "scope": ["src/add.py"],
            "non_goals": ["Core", "installer/UI", "CodexPro"],
            "constraints": ["saved session", "无 API key"],
            "available_assets": ["README.md", "tests/test_add.py"],
            "acceptance": ["add(2,3)==5", "pytest PASS"],
            "human_preferences": ["证据优先"],
        },
        "discovery": {
            "status": "DISCOVERY_COMPLETE" if complete else None,
            "evidence_digest": "d" * 64 if complete else None,
            "candidate_zero": "Candidate 0 最小修复路线",
            "primary_recommendation": "保留 Candidate 0 并先做精确测试",
            "alternatives": ["cheap falsification", "不引入新架构"],
            "external_evidence": [],
            "project_inferences": ["项目推断：目标是局部可判定缺陷"],
            "evidence_gaps": ["证据不足：尚无本轮测试结果"],
        },
        "blueprint": {
            "status": "PROJECT_BLUEPRINT_READY" if complete else None,
            "evidence_digest": "b" * 64 if complete else None,
            "candidate_zero": "Candidate 0",
            "primary_route": {
                "title": "Candidate 0",
                "summary": "最小修复并完成闭环",
                "steps": ["运行精确测试", "检查 diff"],
            },
            "recommended_route": "Candidate 0",
            "alternatives": ["只运行测试的诊断路线"],
            "external_evidence": [],
            "project_inferences": [],
            "evidence_gaps": ["证据不足：尚无 NORMAL review"],
            "validation_plan": ["pytest PASS", "StageController validation"],
        },
        "stage_contract": {
            "created": False,
            "status": "NOT_CREATED",
            "brief_scope": ["src/add.py"],
            "allowed_paths": [],
            "protected_paths": [],
        },
        "artifacts": {
            "brief_path": ".research/PROJECT_BRIEF.json",
            "discovery_report_path": ".research/discovery/DISCOVERY_REPORT.json",
            "blueprint_manifest_path": ".research/blueprint/BLUEPRINT_MANIFEST.json",
            "brief_digest": "a" * 64,
            "discovery_report_digest": "c" * 64,
            "blueprint_manifest_digest": "e" * 64,
        },
    }


def test_bootstrap_renderer_shows_bounded_evidence_and_uncreated_contract_boundary() -> None:
    metadata = {
        "stage": {"status": "AWAITING_DESIGN_REVIEW"},
        "bootstrap_evidence": _bootstrap_evidence(),
    }
    content = build_human_review(metadata, event="design_review_requested")

    assert "项目目标：通过最小变更验证 workflow 闭环" in content
    assert "Candidate 0 最小修复路线" in content
    assert "Blueprint alternatives" in content
    assert "论文 / 成熟 OSS / 工程案例" in content
    assert "证据不足：未提供可验证的论文、成熟 OSS 或工程案例证据。" in content
    assert "项目推断：目标是局部可判定缺陷" in content
    assert "证据不足：尚无本轮测试结果" in content
    assert "Brief scope：" in content and "src/add.py" in content
    assert "Allowed paths：\n- 尚未声明" in content
    assert "Protected paths：\n- 尚未声明" in content


def test_bootstrap_renderer_drops_unknown_transport_and_secret_fields() -> None:
    evidence = _bootstrap_evidence()
    evidence["raw_response"] = "raw response must not render"
    evidence["prompt"] = "prompt must not render"
    evidence["discovery"]["external_evidence"] = ["sk-proj-12345678901234567890"]
    evidence["discovery"]["unknown_nested"] = {"token": "Bearer secret-value-1234567890"}
    content = build_human_review({"bootstrap_evidence": evidence}, event="design_review_requested")

    assert "raw response must not render" not in content
    assert "prompt must not render" not in content
    assert "sk-proj-" not in content
    assert "Bearer secret-value" not in content
    assert "unknown_nested" not in content


def test_bootstrap_renderer_sanitizes_urls_absolute_paths_and_keeps_relative_paths() -> None:
    evidence = _bootstrap_evidence()
    evidence["brief"]["project_goal"] = (
        "参考 https://example.invalid/research?token=abc，"
        "file:///C:/private/repo/secret.txt，"
        "D:\\private\\repo\\secret.txt 和 /private/repo/secret.txt"
    )
    evidence["brief"]["scope"] = [
        "src/add.py",
        ".research/discovery/DISCOVERY_REPORT.json",
        "D:\\private\\repo\\secret.txt",
        "/private/repo/secret.txt",
        "file:///C:/private/repo/secret.txt",
    ]
    evidence["discovery"]["external_evidence"] = [
        "https://example.invalid/paper",
        "sk-proj-123456789012345678901234",
    ]

    content = build_human_review({"bootstrap_evidence": evidence}, event="design_review_requested")

    assert "https://" not in content
    assert "file://" not in content
    assert "D:\\private\\repo\\secret.txt" not in content
    assert "/private/repo/secret.txt" not in content
    assert "sk-proj-" not in content
    assert "src/add.py" in content
    assert ".research/discovery/DISCOVERY_REPORT.json" in content
    assert "<EXTERNAL_URL>" in content
    assert "<LOCAL_PATH>" in content


def test_bootstrap_renderer_sanitizes_root_level_absolute_paths() -> None:
    for raw_path in ("/", "/tmp", "/tmp/", "C:\\", "C:/"):
        assert sanitize_bounded_evidence(raw_path) == "<LOCAL_PATH>"

    evidence = _bootstrap_evidence()
    evidence["brief"]["project_goal"] = (
        "POSIX roots: / /tmp /tmp/ "
        "Windows roots: C:\\ C:/"
    )

    content = build_human_review({"bootstrap_evidence": evidence}, event="design_review_requested")

    assert (
        "POSIX roots: <LOCAL_PATH> <LOCAL_PATH> <LOCAL_PATH> "
        "Windows roots: <LOCAL_PATH> <LOCAL_PATH>"
    ) in content
    for raw_path in ("/tmp", "/tmp/", "C:\\", "C:/"):
        assert raw_path not in content


def test_bootstrap_renderer_makes_missing_evidence_explicit() -> None:
    evidence = _bootstrap_evidence(complete=False)
    evidence["discovery"] = {}
    evidence["blueprint"] = {}
    content = build_human_review({"bootstrap_evidence": evidence}, event="design_review_requested")

    assert "证据已验证：否" in content
    assert "Discovery 状态：未提供" in content
    assert "Blueprint 状态：未提供" in content
    assert "证据不足：未提供可验证的论文、成熟 OSS 或工程案例证据。" in content
    assert "Allowed paths：\n- 尚未声明" in content
    assert "Protected paths：\n- 尚未声明" in content


def test_renderer_does_not_mutate_input_state() -> None:
    metadata = {
        "stage": {"status": "AWAITING_DESIGN_REVIEW"},
        "bootstrap_evidence": _bootstrap_evidence(),
    }
    before = copy.deepcopy(metadata)
    build_human_review(metadata, event="design_review_requested")
    assert metadata == before


def test_runtime_composes_verified_report_manifest_and_brief_without_state_mutation(tmp_path: Path) -> None:
    brief = {
        "project_id": "project-render-fixture",
        "revision": 1,
        "state": "APPROVED",
        "goal": "render bounded Bootstrap evidence",
        "desired_outcome": "reviewable artifact",
        "scope": ["src/add.py"],
        "non_goals": ["Core"],
        "constraints": ["no secrets"],
        "available_assets": ["README.md"],
        "acceptance_criteria": ["artifact is bounded"],
        "preferences": ["evidence first"],
    }
    research = tmp_path / ".research"
    discovery_path = research / "discovery" / "DISCOVERY_REPORT.json"
    manifest_path = research / "blueprint" / "BLUEPRINT_MANIFEST.json"
    discovery_path.parent.mkdir(parents=True)
    manifest_path.parent.mkdir(parents=True)
    discovery_digest = "d" * 64
    blueprint_digest = "b" * 64
    report = {
        "schema_version": "discovery_report.v1",
        "marker": "GPT_PROJECT_DISCOVERY_PASS",
        "status": "DISCOVERY_COMPLETE",
        "project_id": brief["project_id"],
        "brief_digest": _digest(brief),
        "evidence_digest": discovery_digest,
        "primary_recommendation": {"summary": "Candidate 0"},
        "alternatives": ["one alternative"],
        "facts_needing_local_verification": ["证据不足：missing test"],
        "relevant_non_repo_methods": ["项目推断：bounded route"],
    }
    manifest = {
        "schema_version": "blueprint_manifest.v1",
        "marker": "GPT_CODEX_BLUEPRINT_REVIEW_PASS",
        "status": "PROJECT_BLUEPRINT_READY",
        "project_id": brief["project_id"],
        "brief_digest": _digest(brief),
        "discovery_digest": discovery_digest,
        "blueprint_digest": blueprint_digest,
        "primary_route": {"title": "Candidate 0", "summary": "minimal route", "steps": ["test"]},
        "alternatives": ["alternative"],
        "open_questions": ["证据不足：normal review"],
        "validation_plan": ["test"],
    }
    discovery_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    report_file_digest = hashlib.sha256(discovery_path.read_bytes()).hexdigest()
    manifest_file_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    bootstrap = {
        "schema_version": "bootstrap_state.v1",
        "project_id": brief["project_id"],
        "brief_digest": _digest(brief),
        "project_scope_verified": True,
        "context_digest": "c" * 64,
        "discovery_digest": discovery_digest,
        "blueprint_digest": blueprint_digest,
        "discovery": {"report_digest": report_file_digest},
        "blueprint": {"manifest_digest": manifest_file_digest},
        "stage_created": False,
        "stage_started": False,
    }
    transition = {"stage_created": False, "stage_started": False}
    captured: list[dict[str, object]] = []

    def writer(_root: Path, metadata: dict[str, object], **_kwargs: object) -> None:
        captured.append(copy.deepcopy(metadata))

    runtime = WorkflowRuntime(tmp_path, human_artifact_writer=writer)
    before = {
        "report": discovery_path.read_bytes(),
        "manifest": manifest_path.read_bytes(),
    }
    runtime._write_orchestrator_artifacts(
        brief=brief,
        transition={**transition, "bootstrap_state": bootstrap},
        review={"revision": 1, "stage_created": False, "stage_started": False},
        event_name="design_review_requested",
        decision="HUMAN_GATE",
    )
    after = {
        "report": discovery_path.read_bytes(),
        "manifest": manifest_path.read_bytes(),
    }
    assert before == after
    assert len(captured) == 1
    evidence = captured[0]["bootstrap_evidence"]
    assert evidence["validated"] is True
    assert evidence["brief"]["scope"] == ["src/add.py"]
    assert evidence["stage_contract"]["created"] is False
    assert evidence["stage_contract"]["allowed_paths"] == []
