"""Local Step 12 context-compaction and retention acceptance tests.

These tests intentionally stay on temporary project directories.  They do not
invoke the browser bridge, ChatGPT, discovery, or any project outside the
temporary fixture.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from src.artifact_retention import (
    ArtifactClass,
    ArtifactRetentionError,
    build_retention_manifest,
    cleanup_ephemeral,
    classify_artifact,
    load_retention_manifest,
    save_retention_manifest,
)
from src.contracts import load_schema, validate_instance
from src.project_context import (
    PROJECT_CONTEXT_MARKER,
    ProjectContextError,
    build_project_context,
    load_project_context,
    verify_project_context,
)
from src.project_intake import ProjectRequirementsIntake


class Step12ContextRetentionTests(unittest.TestCase):
    def _approved(self, root: Path, **brief: object) -> dict:
        intake = ProjectRequirementsIntake(root)
        intake.initialize(
            mode="USER_CONFIRMED_BRIEF",
            brief={"goal": "deliver a bounded result", "success_criteria": ["a user-visible result"]},
        )
        return intake.approve(rationale="confirmed for Step 12")

    def test_01_only_approved_brief_can_create_context(self) -> None:
        for state in ("draft", "waiting", "cancelled"):
            with self.subTest(state=state), tempfile.TemporaryDirectory(prefix="step12-state-") as directory:
                root = Path(directory)
                intake = ProjectRequirementsIntake(root)
                if state == "draft":
                    intake.initialize(mode="CODEX_REQUIREMENTS_INTERVIEW", rough_requirement="a rough request")
                elif state == "waiting":
                    intake.initialize(
                        mode="USER_CONFIRMED_BRIEF",
                        brief={"goal": "target", "success_criteria": ["done"]},
                    )
                else:
                    intake.initialize(mode="CODEX_REQUIREMENTS_INTERVIEW", rough_requirement="a rough request")
                    intake.cancel(reason="stop")
                with self.assertRaises(ProjectContextError):
                    build_project_context(root)
                self.assertFalse((root / ".research" / "PROJECT_CONTEXT.md").exists())

    def test_02_schema_and_marker_are_local(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step12-schema-") as directory:
            root = Path(directory)
            self._approved(root)
            built = build_project_context(root)
            validate_instance(built, load_schema("project_context"))
            self.assertIn(PROJECT_CONTEXT_MARKER, built["markdown"])
            self.assertEqual(built["path"], ".research/PROJECT_CONTEXT.md")

    def test_03_deterministic_idempotent_and_single_context_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step12-deterministic-") as directory:
            root = Path(directory)
            self._approved(root)
            first = build_project_context(
                root,
                target="target",
                inputs=["input"],
                outputs=["output"],
                decisions=[{"decision": "keep", "evidence_ref": ".research/evidence.md"}],
            )
            context_path = root / ".research" / "PROJECT_CONTEXT.md"
            before_mtime = context_path.stat().st_mtime_ns
            time.sleep(0.01)
            second = build_project_context(
                root,
                target="target",
                inputs=["input"],
                outputs=["output"],
                decisions=[{"evidence_ref": ".research/evidence.md", "decision": "keep"}],
            )
            self.assertEqual(first["context_digest"], second["context_digest"])
            self.assertEqual(first["markdown"], second["markdown"])
            self.assertEqual(before_mtime, context_path.stat().st_mtime_ns)
            self.assertEqual(
                sorted(path.name for path in (root / ".research").iterdir() if path.is_file()),
                ["ARTIFACT_RETENTION_MANIFEST.json", "PROJECT_BRIEF.json", "PROJECT_CONTEXT.md"],
            )

    def test_04_semantic_handoff_fields_are_retained_without_transcript(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step12-fields-") as directory:
            root = Path(directory)
            self._approved(root)
            built = build_project_context(
                root,
                target="the real target",
                inputs=["input-a"],
                outputs=["output-a"],
                user_visible_success=["success on screen"],
                constraints=["do not alter source"],
                non_goals=["no deployment"],
                preferences=["concise"],
                decisions=[{"decision": "selected primary", "evidence_ref": ".research/evidence.md"}],
                status="ACTIVE",
                selected_route={"route": "primary"},
                rejected_route={"route": "alternate", "short_reason": "insufficient evidence", "evidence_ref": ".research/reject.md"},
                unresolved_questions=["which sample next"],
                next_action="collect evidence",
                artifact_refs=[{"path": ".research/evidence.md", "class": "MILESTONE_EVIDENCE"}],
                git_identity={"commit_sha": "a" * 40},
            )
            handoff = built["context"]
            for field in (
                "target", "inputs", "outputs", "user_visible_success", "constraints", "non_goals",
                "preferences", "decisions", "status", "selected_route", "rejected_routes",
                "unresolved_questions", "next_action", "artifact_refs", "git_identity",
            ):
                self.assertIn(field, handoff)
            self.assertIn("the real target", built["markdown"])
            self.assertNotIn("transcript", built["markdown"].casefold())

    def test_05_transcript_and_secret_inputs_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step12-secret-") as directory:
            root = Path(directory)
            self._approved(root)
            with self.assertRaises(ProjectContextError):
                build_project_context(root, context={"transcript": ["user says secret"]})
            with self.assertRaises(ProjectContextError):
                build_project_context(root, context={"decisions": [{"token": "not persisted"}]})
            self.assertFalse((root / ".research" / "PROJECT_CONTEXT.md").exists())

    def test_06_resource_limit_and_explicit_selection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step12-limit-") as directory:
            root = Path(directory)
            self._approved(root)
            resources = []
            for index in range(9):
                path = root / f"input-{index}.txt"
                path.write_text(f"input {index}\n", encoding="utf-8")
                resources.append(str(path.relative_to(root)))
            built = build_project_context(root, resources=resources)
            self.assertEqual(len(built["resources"]), 9)
            extra = root / "extra.txt"
            extra.write_text("extra\n", encoding="utf-8")
            with self.assertRaises(ProjectContextError):
                build_project_context(root, resources=resources + ["extra.txt"])

    def test_07_retention_classes_and_ephemeral_cleanup_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step12-retention-") as directory:
            root = Path(directory)
            (root / ".research" / "ephemeral").mkdir(parents=True)
            (root / ".research" / "ephemeral" / "scratch.txt").write_text("scratch", encoding="utf-8")
            (root / "src").mkdir()
            (root / "src" / "business.py").write_text("keep", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "source.json").write_text("keep", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "HEAD").write_text("keep", encoding="utf-8")
            manifest = build_retention_manifest(
                [
                    {"path": ".research/ephemeral/scratch.txt", "class": "EPHEMERAL"},
                    {"path": ".research/milestones/result.json", "class": "MILESTONE_EVIDENCE"},
                    {"path": "src/business.py", "class": "ACTIVE_REFERENCE"},
                    {"path": "data/source.json", "class": "ACTIVE_REFERENCE"},
                ],
                project_id="project-test",
            )
            save_retention_manifest(root, manifest)
            self.assertEqual(classify_artifact("src/business.py"), ArtifactClass.ACTIVE_REFERENCE.value)
            result = cleanup_ephemeral(root, [".research/ephemeral/scratch.txt"])
            self.assertEqual(result["removed"], [".research/ephemeral/scratch.txt"])
            self.assertFalse((root / ".research" / "ephemeral" / "scratch.txt").exists())
            self.assertTrue((root / "src" / "business.py").exists())
            self.assertTrue((root / "data" / "source.json").exists())
            self.assertTrue((root / ".git" / "HEAD").exists())
            with self.assertRaises(ArtifactRetentionError):
                cleanup_ephemeral(root, ["src/business.py"])

    def test_08_path_and_secret_safety(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step12-path-") as directory:
            root = Path(directory)
            self._approved(root)
            safe = root / "safe.txt"
            safe.write_text("ordinary", encoding="utf-8")
            for bad in ("../outside.txt", ".research/../safe.txt", "C:/outside.txt", ".research/.env"):
                with self.subTest(bad=bad), self.assertRaises((ProjectContextError, ArtifactRetentionError)):
                    build_project_context(root, resources=[bad])
            secret = root / "safe-secret.txt"
            secret.write_text("Bearer abcdefghijklmnop123456", encoding="utf-8")
            with self.assertRaises(ProjectContextError):
                build_project_context(root, resources=["safe-secret.txt"])

    def test_09_load_verifies_markdown_and_manifest_digest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step12-tamper-") as directory:
            root = Path(directory)
            self._approved(root)
            built = build_project_context(root)
            self.assertTrue(load_project_context(root)["context_digest"] == built["context_digest"])
            context_path = root / ".research" / "PROJECT_CONTEXT.md"
            context_path.write_text(context_path.read_text(encoding="utf-8") + "tamper\n", encoding="utf-8")
            with self.assertRaises(ProjectContextError):
                load_project_context(root)

    def test_10_optional_step11_binding_and_verification(self) -> None:
        with tempfile.TemporaryDirectory(prefix="step12-binding-") as directory:
            root = Path(directory)
            intake = ProjectRequirementsIntake(root)
            intake.initialize(
                mode="USER_CONFIRMED_BRIEF",
                brief={
                    "goal": "target",
                    "success_criteria": ["done"],
                    "chatgpt_project_url": "https://chatgpt.com/g/g-p-other-project/project",
                    "chatgpt_project_binding": {"project_id": "local-project"},
                },
            )
            intake.approve()
            built = build_project_context(root)
            self.assertIn("chatgpt_project_url", built["markdown"])
            self.assertEqual(verify_project_context(root)["marker"], PROJECT_CONTEXT_MARKER)
            self.assertEqual(load_retention_manifest(root)["context_resource_count"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
