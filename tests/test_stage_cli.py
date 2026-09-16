"""Product-facing CLI tests for Step 10.

The consultation tests use a bounded fake runner.  No test in this module
opens a browser or sends a ChatGPT request.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.stage_cli import CliError, main
from src.human_artifacts import CANONICAL_HUMAN_ARTIFACTS
from src.stage_controller import StageController, StageState


def make_contract(root: Path, stage_id: str = "stage-001") -> dict:
    return {
        "schema_version": "stage_contract.v1",
        "plan_id": "plan-cli-test",
        "project_id": "cli-disposable",
        "repository_root": str(root),
        "stage_id": stage_id,
        "stage_name": "bounded fixture stage",
        "project_goal": "produce a user-visible bounded artifact",
        "stage_goal": "verify one local result",
        "user_visible_goal": "show a reviewable local result",
        "inputs": ["input.txt"],
        "protected_paths": [".git", ".auth"],
        "allowed_paths": [".research", "tests"],
        "acceptance_description": "checks pass and an artifact is reviewable",
        "required_checks": ["unit"],
        "review_artifact_requirements": ["metrics_summary"],
        "baseline": {"metric": 1.0},
        "status": "PLANNED",
    }


def invoke(argv: list[str], *, runner=None) -> tuple[int, dict | None, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv, **({"runner": runner} if runner is not None else {}))
    body = None
    if stdout.getvalue().strip():
        body = json.loads(stdout.getvalue())
    return code, body, stdout.getvalue(), stderr.getvalue()


class StageCliLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="stage-cli-")
        self.root = Path(self.temp.name)
        (self.root / ".research" / "stages" / "stage-001").mkdir(parents=True)
        (self.root / "input.txt").write_text("fixture\n", encoding="utf-8")
        self.contract_path = self.root / ".research" / "stages" / "stage-001" / "contract.json"
        self.contract_path.write_text(json.dumps(make_contract(self.root), indent=2), encoding="utf-8")
        self.prefix = ["--repo", str(self.root)]

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prepare_start_show_and_default_state_are_persistent(self):
        code, body, _out, err = invoke(self.prefix + ["prepare", "stage-001", "--contract", str(self.contract_path)])
        self.assertEqual(code, 0, err)
        self.assertEqual(body["stage"]["status"], "PLANNED")
        self.assertTrue((self.root / ".research" / "stage-state.json").is_file())
        self.assertEqual(
            {path.name for path in self.root.iterdir() if path.name.endswith(".md")},
            {"STAGE_EXECUTION_PLAN.md", "RESEARCH_DECISION_LOG.md"},
        )
        plan = (self.root / "STAGE_EXECUTION_PLAN.md").read_text(encoding="utf-8")
        self.assertIn("PLANNED", plan)
        self.assertIn("verify one local result", plan)

        code, body, _out, err = invoke(self.prefix + ["start", "stage-001"])
        self.assertEqual(code, 0, err)
        self.assertEqual(body["stage"]["status"], "ACTIVE")
        self.assertIn("ACTIVE", (self.root / "STAGE_EXECUTION_PLAN.md").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "HUMAN_REVIEW.md").exists())
        code, body, _out, err = invoke(self.prefix + ["show", "stage-001"])
        self.assertEqual(code, 0, err)
        self.assertEqual(body["stage"]["status"], "ACTIVE")
        self.assertNotIn("events", body["stage"])

    def test_prepare_leaves_an_existing_review_directory_untouched(self):
        (self.root / "HUMAN_REVIEW.md").mkdir()
        code, _body, _out, err = invoke(
            self.prefix + ["prepare", "stage-001", "--contract", str(self.contract_path)]
        )
        self.assertEqual(code, 0, err)
        self.assertTrue((self.root / ".research" / "stage-state.json").exists())
        self.assertTrue((self.root / "STAGE_EXECUTION_PLAN.md").exists())
        self.assertTrue((self.root / "HUMAN_REVIEW.md").is_dir())

    def test_artifact_write_failure_rolls_back_start_state(self):
        invoke(self.prefix + ["prepare", "--contract", str(self.contract_path)])
        state_path = self.root / ".research" / "stage-state.json"
        before = state_path.read_bytes()
        (self.root / "HUMAN_REVIEW.md").mkdir()

        code, _body, _out, err = invoke(self.prefix + ["start", "stage-001"])
        self.assertEqual(code, 0, err)
        self.assertNotEqual(state_path.read_bytes(), before)
        restored = StageController.from_state(state_path)
        self.assertEqual(restored.show_stage("stage-001")["status"], "ACTIVE")
        self.assertTrue((self.root / "HUMAN_REVIEW.md").is_dir())

    def test_invalid_transition_is_bounded_and_stop_alias_closes_stage(self):
        invoke(self.prefix + ["prepare", "--contract", str(self.contract_path)])
        code, body, _out, err = invoke(self.prefix + ["approve", "stage-001"])
        self.assertEqual(code, 2)
        self.assertIsNone(body)
        self.assertIn("APPROVE_REJECTED", err)
        code, body, _out, err = invoke(self.prefix + ["start", "stage-001"])
        self.assertEqual(code, 0, err)
        code, body, _out, err = invoke(self.prefix + ["pause", "stage-001", "--rationale", "user pause"])
        self.assertEqual(code, 0, err)
        self.assertEqual(body["stage"]["status"], "STOPPED")
        code, _body, _out, err = invoke(self.prefix + ["consult", "stage-001", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("STAGE_NOT_ACTIVE", err)

    def test_prepare_rejects_contract_outside_repo_and_repository_mismatch(self):
        outside = Path(self.temp.name).parent / "outside-contract.json"
        outside.write_text(json.dumps(make_contract(self.root)), encoding="utf-8")
        try:
            code, _body, _out, err = invoke(self.prefix + ["prepare", "--contract", str(outside)])
            self.assertEqual(code, 2)
            self.assertIn("PATH_OUTSIDE_REPOSITORY", err)
        finally:
            outside.unlink(missing_ok=True)

        mismatch = make_contract(self.root)
        mismatch["repository_root"] = str(self.root.parent)
        self.contract_path.write_text(json.dumps(mismatch), encoding="utf-8")
        code, _body, _out, err = invoke(self.prefix + ["prepare", "--contract", str(self.contract_path)])
        self.assertEqual(code, 2)
        self.assertIn("REPOSITORY_ROOT_MISMATCH", err)

    def test_show_artifacts_uses_latest_result_and_local_artifact_directory(self):
        invoke(self.prefix + ["prepare", "--contract", str(self.contract_path)])
        controller = StageController.from_state(self.root / ".research" / "stage-state.json")
        controller.start_stage("stage-001")
        controller.mark_stage_ready(
            {
                "status": "SUCCEEDED",
                "baseline_digest": controller.state["baseline_digest"],
                "required_checks": {"unit": "PASS"},
                "review_artifacts": [{"type": "metrics_summary", "path": ".research/stages/stage-001/artifacts/metrics.json"}],
            }
        )
        artifact = self.root / ".research" / "stages" / "stage-001" / "artifacts" / "metrics.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text('{"metric": 0.5}\n', encoding="utf-8")
        code, body, _out, err = invoke(self.prefix + ["show-artifacts", "stage-001"])
        self.assertEqual(code, 0, err)
        self.assertEqual(body["stage_status"], "STAGE_READY")
        self.assertTrue(any(item.get("path") == ".research/stages/stage-001/artifacts/metrics.json" for item in body["artifacts"]))


class StageCliConsultationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="stage-cli-consult-")
        self.root = Path(self.temp.name)
        stage_dir = self.root / ".research" / "stages" / "stage-001"
        stage_dir.mkdir(parents=True)
        self.contract_path = stage_dir / "contract.json"
        self.contract_path.write_text(json.dumps(make_contract(self.root)), encoding="utf-8")
        self.evidence = self.root / ".research" / "result.txt"
        self.evidence.write_text("bounded result\n", encoding="utf-8")
        self.prefix = ["--repo", str(self.root)]
        invoke(self.prefix + ["prepare", "--contract", str(self.contract_path)])
        invoke(self.prefix + ["start", "stage-001"])
        self.calls: list[dict] = []
        self.response_text = "bounded response\nWORKFLOW_DECISION: CONTINUE\n"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fake_runner(self, prompt: str, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        number = len(self.calls)
        packet_id = f"PACK-test-{number}"
        manifest_dir = self.root / ".consultations" / "staging" / packet_id
        manifest_dir.mkdir(parents=True, exist_ok=True)
        (manifest_dir / "context_manifest.json").write_text(
            json.dumps({"packet_id": packet_id}), encoding="utf-8"
        )
        return {
            "consultation_id": f"CONSULT-test-{number}",
            "request_count": 1,
            "response_text": self.response_text,
            "receipt_path": str(self.root / ".consultations" / f"CONSULT-test-{number}" / "receipt.json"),
            "receipt": {
                "conversation_id": f"conversation-{number}",
                "context_pack_id": packet_id,
            },
        }

    def test_dry_run_does_not_consume_evidence_or_call_bridge(self):
        code, body, _out, err = invoke(
            self.prefix + ["consult", "stage-001", "--evidence", ".research/result.txt", "--dry-run"],
            runner=self.fake_runner,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(body["status"], "dry_run")
        self.assertEqual(self.calls, [])
        state = json.loads((self.root / ".research" / "stage-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["stages"]["stage-001"]["consultation_requests"], [])

    def test_default_runner_dry_run_does_not_require_real_run(self):
        code, body, _out, err = invoke(self.prefix + ["fresh-review", "stage-001", "--dry-run"])
        self.assertEqual(code, 0, err)
        self.assertEqual(body["status"], "dry_run")
        self.assertEqual(body["mode"], "FRESH")

    def test_normal_consultation_reuses_previous_conversation_without_persisting_reply(self):
        code, body, _out, err = invoke(
            self.prefix + ["consult", "stage-001", "--evidence", ".research/result.txt"],
            runner=self.fake_runner,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(body["request_count"], 1)
        self.assertEqual(body["response"].splitlines()[-1], "WORKFLOW_DECISION: CONTINUE")
        changed = self.root / ".research" / "changed.txt"
        changed.write_text("new evidence\n", encoding="utf-8")
        code, body, _out, err = invoke(
            self.prefix + ["consult", "stage-001", "--evidence", ".research/changed.txt"],
            runner=self.fake_runner,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls[1]["mode"], "continue")
        # The bridge resolves continue_from as a consultation receipt id;
        # the conversation UUID remains a separate local identity field.
        self.assertEqual(self.calls[1]["continue_from"], "CONSULT-test-1")
        self.assertEqual(
            self.calls[1]["context_pack"]["previousPack"]["manifest"]["packet_id"],
            "PACK-test-1",
        )
        state = json.loads((self.root / ".research" / "stage-state.json").read_text(encoding="utf-8"))
        requests = state["stages"]["stage-001"]["consultation_requests"]
        self.assertEqual(requests[1]["conversation_id"], "conversation-1")
        decision_log = (self.root / "RESEARCH_DECISION_LOG.md").read_text(encoding="utf-8")
        self.assertIn("CONSULT-test-1", decision_log)
        self.assertIn("CONSULT-test-2", decision_log)
        self.assertIn("NORMAL", decision_log)
        self.assertIn("CONTINUE", decision_log)
        for name in ("STAGE_EXECUTION_PLAN.md", "RESEARCH_DECISION_LOG.md"):
            content = (self.root / name).read_text(encoding="utf-8")
            self.assertNotIn("bounded response", content)
            self.assertNotIn("response_text", content)
        self.assertFalse((self.root / "HUMAN_REVIEW.md").exists())
        index = json.loads((self.root / ".research" / "reviews" / "index.json").read_text(encoding="utf-8"))
        serialized = json.dumps(index, ensure_ascii=False)
        self.assertNotIn("bounded response", serialized)
        for item in index["consultations"]:
            self.assertIn("reason", item)
            self.assertIn("question_summary", item)
            self.assertIn("blocker_summary", item)
            self.assertIn("workflow_decision", item)
            self.assertIn("codex_disposition", item)
            self.assertLessEqual(len(item["question_summary"]), 500)
            self.assertLessEqual(len(item["blocker_summary"]), 500)
        self.assertEqual(index["consultations"][0]["workflow_decision"], "CONTINUE")
        self.assertIsNone(index["consultations"][0]["codex_disposition"])
        self.assertEqual([item["request_count"] for item in index["consultations"]], [1, 1])

    def test_real_stage_consultation_forwards_approved_project_binding(self):
        project_url = "https://chatgpt.com/g/g-p-example-project/project"
        (self.root / ".research" / "PROJECT_BRIEF.json").write_text(
            json.dumps({"brief": {"chatgpt_project_url": project_url}}), encoding="utf-8"
        )
        code, _body, _out, err = invoke(
            self.prefix + ["consult", "stage-001", "--evidence", ".research/result.txt"],
            runner=self.fake_runner,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls[0]["project_url"], project_url)

    def test_explicit_homepage_fallback_preserves_identity_and_labels_transport(self):
        code, body, _out, err = invoke(
            self.prefix + ["fresh-review", "stage-001", "--transport", "homepage_fallback",
                           "--evidence", ".research/result.txt"], runner=self.fake_runner)
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.calls[0]["project_url"])
        self.assertEqual(self.calls[0]["transport"], "homepage_fallback")
        facts = self.calls[0]["context_pack"]["establishedFacts"]
        self.assertIn("Project identity: cli-disposable", facts)
        self.assertIn("Stage identity: stage-001", facts)
        index = json.loads((self.root / ".research/reviews/index.json").read_text())
        self.assertEqual(index["consultations"][-1]["transport"], "homepage_fallback")

    def test_homepage_fallback_cannot_override_explicit_scope_contract(self):
        scoped_root = self.root / "scoped"
        scoped_root.mkdir()
        contract = make_contract(scoped_root)
        contract["metadata"] = {"project_scope_required": True}
        path = scoped_root / "contract.json"
        path.write_text(json.dumps(contract), encoding="utf-8")
        prefix = ["--repo", str(scoped_root)]
        self.assertEqual(invoke(prefix + ["prepare", "--contract", "contract.json"])[0], 0)
        self.assertEqual(invoke(prefix + ["start", "stage-001"])[0], 0)
        code, _, _, err = invoke(prefix + ["fresh-review", "--transport", "homepage_fallback"], runner=self.fake_runner)
        self.assertNotEqual(code, 0)
        self.assertIn("PROJECT_SCOPE_REQUIRED", err)
        self.assertEqual(self.calls, [])

    def test_fresh_review_is_new_conversation_and_protected_evidence_fails_closed(self):
        code, body, errout, err = invoke(
            self.prefix + ["fresh-review", "stage-001", "--evidence", ".research/result.txt"],
            runner=self.fake_runner,
        )
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.calls[0]["continue_from"])
        self.assertEqual(self.calls[0]["mode"], "fresh")
        decision_log = (self.root / "RESEARCH_DECISION_LOG.md").read_text(encoding="utf-8")
        self.assertIn("CONSULT-test-1", decision_log)
        self.assertIn("FRESH", decision_log)
        self.assertIn("CONTINUE", decision_log)
        protected = self.root / ".auth"
        protected.mkdir()
        secret_file = protected / "cookies.json"
        secret_file.write_text("{}", encoding="utf-8")
        code, _body, _out, err = invoke(
            self.prefix + ["consult", "stage-001", "--evidence", ".auth/cookies.json"],
            runner=self.fake_runner,
        )
        self.assertEqual(code, 2)
        self.assertIn("PROTECTED_PATH_REJECTED", err)
        self.assertEqual(len(self.calls), 1)

    def test_failed_bridge_is_recorded_without_retry_and_same_digest_is_blocked(self):
        def failed_runner(_prompt, **_kwargs):
            raise RuntimeError("external failure details must not be shown")

        code, _body, _out, err = invoke(
            self.prefix + ["consult", "stage-001", "--evidence", ".research/result.txt"],
            runner=failed_runner,
        )
        self.assertEqual(code, 2)
        self.assertIn("BRIDGE_EXTERNAL_FAILURE", err)
        self.assertNotIn("external failure details", err)
        code, _body, _out, err = invoke(
            self.prefix + ["consult", "stage-001", "--evidence", ".research/result.txt"],
            runner=self.fake_runner,
        )
        self.assertEqual(code, 2)
        self.assertIn("CONSULTATION_REJECTED", err)

    def test_artifact_failure_after_bridge_keeps_request_consumed(self):
        human_review = self.root / "HUMAN_REVIEW.md"
        human_review.mkdir()
        self.response_text = "bounded gate response\nWORKFLOW_DECISION: HUMAN_GATE\n"

        code, _body, _out, err = invoke(
            self.prefix + ["consult", "stage-001", "--evidence", ".research/result.txt"],
            runner=self.fake_runner,
        )
        self.assertEqual(code, 2)
        self.assertIn("CANONICAL_ARTIFACT_INVALID", err)
        state = json.loads((self.root / ".research" / "stage-state.json").read_text(encoding="utf-8"))
        stage = state["stages"]["stage-001"]
        self.assertEqual(stage["status"], "ACTIVE")
        self.assertEqual(len(stage["consultation_requests"]), 1)
        index = json.loads((self.root / ".research" / "reviews" / "index.json").read_text(encoding="utf-8"))
        artifact_error = index["artifact_errors"][-1]
        self.assertEqual(artifact_error["operation"], "consult")
        self.assertEqual(artifact_error["event"], "consultation_complete")
        self.assertEqual(artifact_error["failure_code"], "CANONICAL_ARTIFACT_INVALID")
        self.assertNotIn(self.response_text, json.dumps(index, ensure_ascii=False))
        before_retry = len(self.calls)
        code, _body, _out, err = invoke(
            self.prefix + ["consult", "stage-001", "--evidence", ".research/result.txt"],
            runner=self.fake_runner,
        )
        self.assertEqual(code, 2)
        self.assertIn("CONSULTATION_REJECTED", err)
        self.assertEqual(len(self.calls), before_retry)

    def test_human_gate_consultation_updates_review_artifact_without_raw_response(self):
        self.response_text = "bounded gate response\nWORKFLOW_DECISION: HUMAN_GATE\n"
        code, body, _out, err = invoke(
            self.prefix + ["consult", "stage-001", "--evidence", ".research/result.txt"],
            runner=self.fake_runner,
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(body["response"], self.response_text)
        human_review = (self.root / "HUMAN_REVIEW.md").read_text(encoding="utf-8")
        self.assertIn("HUMAN_GATE", human_review)
        self.assertIn("状态：未触发", human_review)
        self.assertNotIn("待人工处理", human_review)
        state = json.loads((self.root / ".research" / "stage-state.json").read_text(encoding="utf-8"))
        self.assertIsNone(state["stages"]["stage-001"]["pending_human_gate"])
        for name in CANONICAL_HUMAN_ARTIFACTS:
            content = (self.root / name).read_text(encoding="utf-8")
            self.assertNotIn(self.response_text, content)
            self.assertNotIn("response_text", content)

    def test_invalid_workflow_decision_is_failed_without_complete_entry_or_retry(self):
        responses = (
            "bounded missing marker response",
            "bounded unknown response\nWORKFLOW_DECISION: MAYBE\n",
            "bounded duplicate response\nWORKFLOW_DECISION: CONTINUE\nWORKFLOW_DECISION: REPLAN\n",
        )
        for index, response in enumerate(responses, start=1):
            with self.subTest(index=index):
                evidence = self.root / ".research" / f"invalid-{index}.txt"
                evidence.write_text(response, encoding="utf-8")
                self.response_text = response
                code, _body, _out, err = invoke(
                    self.prefix + ["consult", "stage-001", "--evidence", f".research/invalid-{index}.txt"],
                    runner=self.fake_runner,
                )
                self.assertEqual(code, 2)
                self.assertIn("GPT_DECISION_INVALID", err)

        index = json.loads((self.root / ".research" / "reviews" / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(index["consultations"]), len(responses))
        self.assertTrue(all(item["status"] == "failed" for item in index["consultations"]))
        self.assertTrue(all(item["failure_code"] == "GPT_DECISION_INVALID" for item in index["consultations"]))
        serialized = json.dumps(index, ensure_ascii=False)
        for response in responses:
            self.assertNotIn(response, serialized)
        self.assertNotIn("raw_response", serialized)

        # The controller has consumed every request, so retrying the first
        # evidence digest is rejected before the runner can be called again.
        before_retry = len(self.calls)
        self.response_text = "should never be sent\nWORKFLOW_DECISION: CONTINUE\n"
        code, _body, _out, err = invoke(
            self.prefix + ["consult", "stage-001", "--evidence", ".research/invalid-1.txt"],
            runner=self.fake_runner,
        )
        self.assertEqual(code, 2)
        self.assertIn("CONSULTATION_REJECTED", err)
        self.assertEqual(len(self.calls), before_retry)


if __name__ == "__main__":
    unittest.main()
