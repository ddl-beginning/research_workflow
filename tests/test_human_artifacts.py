"""Offline checks for the bounded human artifact lifecycle writer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.human_artifacts import (
    CANONICAL_HUMAN_ARTIFACTS,
    HumanArtifactError,
    build_research_decision_log,
    build_human_review,
    generate_human_artifacts,
    write_human_artifacts,
)


def stage_metadata(status: str = "PLANNED") -> dict:
    return {
        "stage_id": "stage-artifact-fixture",
        "project_id": "project-artifact-fixture",
        "status": status,
        "iteration_index": 1,
        "contract": {
            "plan_id": "plan-artifact-fixture",
            "stage_name": "bounded fixture",
            "project_goal": "保留一个可审查的研究结果",
            "stage_goal": "核对有界阶段证据",
            "user_visible_goal": "展示阶段摘要",
            "required_checks": ["unit", "schema"],
            "review_artifact_requirements": ["metrics_summary"],
        },
    }


class HumanArtifactRenderingTests(unittest.TestCase):
    def test_generation_is_bounded_and_drops_transport_payloads(self) -> None:
        metadata = {
            "stage": {
                **stage_metadata(),
                "latest_result": {
                    "status": "SUCCEEDED",
                    "summary": "结果摘要",
                    "raw_response": "模型原始响应不应持久化",
                },
            },
            "consultation": {
                "consultation_id": "CONSULT-fixture",
                "mode": "FRESH",
                "evidence_digest": "digest-fixture",
                "workflow_decision": "HUMAN_GATE",
                "question_summary": "请审查阶段边界",
                "response_text": "模型原始响应不应持久化",
            },
        }

        documents = generate_human_artifacts(metadata, event="consultation_completed")
        self.assertEqual(set(documents), set(CANONICAL_HUMAN_ARTIFACTS))
        serialized = "\n".join(documents.values())
        self.assertIn("核对有界阶段证据", serialized)
        self.assertIn("HUMAN_GATE", serialized)
        self.assertNotIn("模型原始响应不应持久化", serialized)
        self.assertNotIn("raw_response", serialized)
        self.assertNotIn("response_text", serialized)
        for content in documents.values():
            self.assertLess(len(content.encode("utf-8")), 80_000)

    def test_human_gate_summary_requires_machine_pending_gate(self) -> None:
        content = build_human_review(
            event={
                "event": "planner_decision",
                "stage_id": "stage-artifact-fixture",
                "details": {"decision": "HUMAN_GATE", "rationale": "证据仍需人工核对"},
            },
            stage_metadata={"stage": stage_metadata("ACTIVE")},
        )
        self.assertIn("状态：未触发", content)
        self.assertIn("决策：未触发", content)
        self.assertIn("HUMAN_GATE", content)
        self.assertNotIn("待人工处理", content)

    def test_human_gate_summary_is_pending_when_controller_has_pending_gate(self) -> None:
        stage = stage_metadata("ACTIVE")
        stage["pending_human_gate"] = {
            "decision": "HUMAN_GATE",
            "rationale": "证据仍需人工核对",
        }
        content = build_human_review(
            event={
                "event": "planner_decision",
                "stage_id": "stage-artifact-fixture",
                "details": {"decision": "HUMAN_GATE", "rationale": "证据仍需人工核对"},
            },
            stage_metadata={"stage": stage},
        )
        self.assertIn("状态：待人工处理", content)
        self.assertIn("决策：HUMAN_GATE", content)
        self.assertNotIn("已通过", content)

    def test_decision_log_keeps_bounded_route_history_without_raw_response(self) -> None:
        metadata = {
            "stage": {
                **stage_metadata("ACTIVE"),
                "consultations": [
                    {
                        "created_at": "2026-09-06T10:00:00Z",
                        "receipt_path": ".consultations/receipt-1.json",
                        "consultation_id": "CONSULT-one",
                        "conversation_id": "conversation-one",
                        "type": "NORMAL_CONSULTATION",
                        "reason": "先验证当前证据",
                        "question_summary": "当前证据是否足够？",
                        "evidence_summary": "digest-one",
                        "gpt_conclusion_summary": "证据可继续验证",
                        "workflow_decision": "CONTINUE",
                        "codex_disposition": "ACCEPT",
                        "resulting_action": "继续当前 Stage",
                        "response_text": "raw response one",
                    },
                    {
                        "created_at": "2026-09-06T11:00:00Z",
                        "receipt_path": ".consultations/receipt-2.json",
                        "consultation_id": "CONSULT-two",
                        "conversation_id": "conversation-two",
                        "type": "FRESH_CLOSEOUT",
                        "reason": "closeout review",
                        "question_summary": "是否可以交付？",
                        "evidence_summary": "digest-two",
                        "gpt_conclusion_summary": "结果已具备审阅条件",
                        "workflow_decision": "STAGE_READY",
                        "codex_disposition": "WAIT_FOR_HUMAN",
                        "resulting_action": "等待人工审阅",
                    },
                ],
            }
        }
        content = build_research_decision_log(metadata, event="start_stage")
        self.assertIn("CONSULT-one", content)
        self.assertIn("CONSULT-two", content)
        self.assertIn("receipt-1.json", content)
        self.assertIn("conversation-two", content)
        self.assertIn("FRESH_CLOSEOUT", content)
        self.assertIn("Evidence 摘要：digest-two", content)
        self.assertIn("GPT 结论摘要：结果已具备审阅条件", content)
        self.assertIn("WORKFLOW_DECISION：STAGE_READY", content)
        self.assertIn("Resulting action：等待人工审阅", content)
        self.assertNotIn("raw response one", content)
        self.assertNotIn("response_text", content)

    def test_human_review_is_preserved_for_planning_start_and_continue(self) -> None:
        with tempfile.TemporaryDirectory(prefix="human-review-lifecycle-") as directory:
            root = Path(directory)
            write_human_artifacts(root, event="stage_planning", stage_metadata=stage_metadata())
            self.assertFalse((root / "HUMAN_REVIEW.md").exists())
            write_human_artifacts(
                root,
                event={"event": "planner_decision", "details": {"decision": "HUMAN_GATE", "rationale": "需人工确认"}},
                stage_metadata=stage_metadata("ACTIVE"),
            )
            review = root / "HUMAN_REVIEW.md"
            before = review.read_bytes()
            write_human_artifacts(root, event="start_stage", stage_metadata=stage_metadata("ACTIVE"))
            self.assertEqual(review.read_bytes(), before)
            write_human_artifacts(
                root,
                event="consultation_complete",
                stage_metadata=stage_metadata("ACTIVE"),
                consultation_metadata={
                    "consultation_id": "CONSULT-continue",
                    "mode": "NORMAL",
                    "workflow_decision": "CONTINUE",
                },
            )
            self.assertEqual(review.read_bytes(), before)


class HumanArtifactWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="human-artifacts-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_supported_lifecycle_events_create_and_update_fixed_root_files(self) -> None:
        paths = write_human_artifacts(
            self.root,
            event="stage_planning",
            stage_metadata=stage_metadata(),
        )
        self.assertEqual(set(paths), {"STAGE_EXECUTION_PLAN.md", "RESEARCH_DECISION_LOG.md"})
        self.assertTrue(all(path.parent == self.root.resolve() for path in paths.values()))
        self.assertTrue(all(path.is_file() for path in paths.values()))
        self.assertFalse((self.root / "HUMAN_REVIEW.md").exists())
        self.assertIn("PLANNED", (self.root / "STAGE_EXECUTION_PLAN.md").read_text(encoding="utf-8"))

        write_human_artifacts(
            self.root,
            event="start_stage",
            stage_metadata=stage_metadata("ACTIVE"),
        )
        self.assertIn("ACTIVE", (self.root / "STAGE_EXECUTION_PLAN.md").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "HUMAN_REVIEW.md").exists())

        write_human_artifacts(
            self.root,
            event="consultation_completed",
            stage_metadata=stage_metadata("ACTIVE"),
            consultation_metadata={
                "consultation_id": "CONSULT-fixture",
                "mode": "FRESH",
                "evidence_digest": "digest-fixture",
                "workflow_decision": "CONTINUE",
                "codex_disposition": "ACCEPT",
                "reason": "完成有界审查",
            },
        )
        decision_log = (self.root / "RESEARCH_DECISION_LOG.md").read_text(encoding="utf-8")
        self.assertIn("CONSULT-fixture", decision_log)
        self.assertIn("CONTINUE", decision_log)

        leftovers = [item for item in self.root.iterdir() if item.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_unsupported_event_fails_before_mutating_existing_files(self) -> None:
        write_human_artifacts(self.root, event="stage_planning", stage_metadata=stage_metadata())
        before = {
            name: (self.root / name).read_bytes()
            for name in ("STAGE_EXECUTION_PLAN.md", "RESEARCH_DECISION_LOG.md")
        }
        with self.assertRaises(HumanArtifactError) as raised:
            write_human_artifacts(self.root, event="executor_requested", stage_metadata=stage_metadata("ACTIVE"))
        self.assertEqual(raised.exception.code, "ARTIFACT_EVENT_UNSUPPORTED")
        after = {
            name: (self.root / name).read_bytes()
            for name in before
        }
        self.assertEqual(before, after)
        self.assertFalse((self.root / "HUMAN_REVIEW.md").exists())

    def test_existing_review_directory_is_ignored_until_a_reviewable_event(self) -> None:
        (self.root / "HUMAN_REVIEW.md").mkdir()
        write_human_artifacts(self.root, event="stage_planning", stage_metadata=stage_metadata())
        self.assertTrue((self.root / "HUMAN_REVIEW.md").is_dir())
        with self.assertRaises(HumanArtifactError) as raised:
            write_human_artifacts(
                self.root,
                event={"event": "planner_decision", "details": {"decision": "HUMAN_GATE"}},
                stage_metadata=stage_metadata("ACTIVE"),
            )
        self.assertEqual(raised.exception.code, "CANONICAL_ARTIFACT_INVALID")


if __name__ == "__main__":
    unittest.main()
