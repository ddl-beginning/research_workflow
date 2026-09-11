"""Regression checks for the disposable GPT-first workflow self-test."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.workflow_self_test import run_self_test


REPO_ROOT = Path(__file__).resolve().parents[1]


class WorkflowSelfTestHarnessTests(unittest.TestCase):
    def test_human_gate_flow_is_bounded_and_keeps_raw_reply_out_of_files(self) -> None:
        # The run is still present while assertions inspect its three fixed
        # artifacts.  TemporaryDirectory then removes only this disposable run.
        with tempfile.TemporaryDirectory(
            dir=str(REPO_ROOT / ".tmp"),
            prefix="workflow_self_test_test_",
        ) as directory:
            result = run_self_test(repo_root=REPO_ROOT, run_dir=Path(directory))
            root = Path(directory)
            self.assertEqual(result["final_status"], "ACTIVE")
            self.assertEqual(result["status_history"], ["PLANNED", "ACTIVE"])
            self.assertEqual(result["metrics"]["action_dispatch_count"], 1)
            self.assertEqual(result["metrics"]["budget_rejected_repeat_count"], 1)
            self.assertTrue(result["pending_human_gate"])
            self.assertEqual(
                set(name for name in result["files"] if "/" not in name and "\\" not in name)
                & {"STAGE_EXECUTION_PLAN.md", "RESEARCH_DECISION_LOG.md", "HUMAN_REVIEW.md"},
                {"STAGE_EXECUTION_PLAN.md", "RESEARCH_DECISION_LOG.md", "HUMAN_REVIEW.md"},
            )
            serialized = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in root.rglob("*")
                if path.is_file()
            )
            self.assertNotIn("SELF_TEST_FAKE_RAW_CLOSEOUT_RESPONSE_DO_NOT_PERSIST", serialized)
            self.assertNotIn('"response_text"', serialized)
            decision_log = (root / "RESEARCH_DECISION_LOG.md").read_text(encoding="utf-8")
            self.assertIn("CONSULT-20260906-070055-bef2fdb6", decision_log)
            self.assertIn("6a9d0fc1-38f8-83ee-93e3-7e915ed17b76", decision_log)
            self.assertIn("PARTIAL_ACCEPT", decision_log)
            self.assertIn("ActionMap", decision_log)
            self.assertIn("fake Codex/Luna", decision_log)

    def test_stage_ready_closeout_uses_same_single_step_path(self) -> None:
        with tempfile.TemporaryDirectory(
            dir=str(REPO_ROOT / ".tmp"),
            prefix="workflow_self_test_test_",
        ) as directory:
            result = run_self_test(
                repo_root=REPO_ROOT,
                run_dir=Path(directory),
                closeout_decision="STAGE_READY",
            )
            self.assertEqual(result["final_status"], "STAGE_READY")
            self.assertIsNone(result["pending_human_gate"])
            self.assertTrue(all(value == "PASS" for value in result["checks"].values()))
            self.assertEqual(result["action_map"]["execute_before_validation"], False)
            self.assertEqual(result["action_map"]["execute_after_validation"], False)
            self.assertEqual(result["action_map"]["execute_after_enable"], True)


if __name__ == "__main__":
    unittest.main()
