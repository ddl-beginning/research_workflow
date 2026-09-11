"""Regression coverage for the Step 15 Bootstrap-to-Stage planning boundary."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from scripts.stage_cli import main as stage_cli_main
from scripts.step15_acceptance import _bootstrap_fixture
from src.contracts import load_schema, validate_instance
from src.human_artifacts import CANONICAL_HUMAN_ARTIFACTS
from src.stage_controller import StageController, StageState
from src.stage_planning import (
    BOOTSTRAP_STATE_RELATIVE_PATH,
    STAGE_PLAN_RELATIVE_PATH,
    StagePlanningError,
    load_stage_plan,
    plan_stage,
    verify_stage_plan,
)


class Step15PlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="step15-test-")
        self.root = Path(self.temp.name)
        _bootstrap_fixture(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_plans_and_registers_one_planned_stage(self) -> None:
        result = plan_stage(self.root)
        validate_instance(result, load_schema("stage_planning"))
        contract_path = self.root / result["stage_contract_path"]
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        validate_instance(contract, load_schema("stage_contract.v1"))
        state = StageController.from_state(self.root / ".research" / "stage-state.json")
        stage = state.show_stage(result["stage_id"])
        self.assertEqual(stage["status"], StageState.PLANNED.value)
        self.assertIsNone(state.state["active_stage_id"])
        self.assertFalse(result["stage_started"])
        self.assertTrue((self.root / STAGE_PLAN_RELATIVE_PATH).is_file())
        self.assertEqual(verify_stage_plan(self.root)["stage_status"], StageState.PLANNED.value)

    def test_same_bootstrap_inputs_reuse_plan_without_new_registration_event(self) -> None:
        first = plan_stage(self.root)
        state_path = self.root / ".research" / "stage-state.json"
        before = json.loads(state_path.read_text(encoding="utf-8"))
        contract_bytes = (self.root / first["stage_contract_path"]).read_bytes()
        second = plan_stage(self.root)
        after = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertTrue(second["idempotent_reuse"])
        self.assertEqual(first["contract_digest"], second["contract_digest"])
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual(contract_bytes, (self.root / second["stage_contract_path"]).read_bytes())
        self.assertEqual(load_stage_plan(self.root)["stage_id"], first["stage_id"])

    def test_missing_bootstrap_state_fails_closed_before_stage_registration(self) -> None:
        (self.root / ".research" / "bootstrap_state.json").unlink()
        with self.assertRaises(StagePlanningError) as raised:
            plan_stage(self.root)
        self.assertEqual(raised.exception.code, "BOOTSTRAP_STATE_NOT_FOUND")
        self.assertFalse((self.root / ".research" / "stage-state.json").exists())

    def test_stage_cli_plan_stage_uses_the_same_boundary(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = stage_cli_main(["--repo", str(self.root), "plan-stage"])
        self.assertEqual(code, 0, errors.getvalue())
        result = json.loads(output.getvalue())
        self.assertEqual(result["marker"], "STAGE_PLANNING_PASS")
        self.assertEqual(result["stage_status"], "PLANNED")
        self.assertFalse(result["stage_started"])
        self.assertEqual(
            {path.name for path in self.root.iterdir() if path.name.endswith(".md")},
            {"STAGE_EXECUTION_PLAN.md", "RESEARCH_DECISION_LOG.md"},
        )
        plan = (self.root / "STAGE_EXECUTION_PLAN.md").read_text(encoding="utf-8")
        self.assertIn("PLANNED", plan)
        self.assertNotIn("raw_response", plan)

    def test_stage_cli_plan_stage_leaves_review_directory_untouched(self) -> None:
        (self.root / "HUMAN_REVIEW.md").mkdir()
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = stage_cli_main(["--repo", str(self.root), "plan-stage"])
        self.assertEqual(code, 0, errors.getvalue())
        self.assertTrue((self.root / ".research" / "stage-state.json").exists())
        self.assertTrue((self.root / "HUMAN_REVIEW.md").is_dir())

    def test_bootstrap_state_requires_complete_handoff_fields(self) -> None:
        state_path = self.root / BOOTSTRAP_STATE_RELATIVE_PATH
        original = json.loads(state_path.read_text(encoding="utf-8"))
        required = (
            "schema_version",
            "status",
            "markers",
            "next_action",
            "project_scope_verified",
            "discovery",
            "blueprint",
            "state_digest",
        )
        for field in required:
            state = dict(original)
            state.pop(field, None)
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            with self.subTest(field=field), self.assertRaises(StagePlanningError):
                plan_stage(self.root)
        state_path.write_text(json.dumps(original, sort_keys=True), encoding="utf-8")

    def test_bootstrap_state_digest_and_nested_bindings_are_checked(self) -> None:
        state_path = self.root / BOOTSTRAP_STATE_RELATIVE_PATH
        original = json.loads(state_path.read_text(encoding="utf-8"))
        for mutation in (
            ("state_digest", "0" * 64),
            ("discovery", {**original["discovery"], "report_digest": "0" * 64}),
            ("blueprint", {**original["blueprint"], "blueprint_path": ".research/blueprint/wrong.md"}),
        ):
            state = dict(original)
            state[mutation[0]] = mutation[1]
            state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
            with self.subTest(field=mutation[0]), self.assertRaises(StagePlanningError):
                plan_stage(self.root)
        state_path.write_text(json.dumps(original, sort_keys=True), encoding="utf-8")

    def test_discovery_pass_marker_is_mandatory(self) -> None:
        report_path = self.root / ".research" / "discovery" / "DISCOVERY_REPORT.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report.pop("marker", None)
        report_path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        with self.assertRaises(StagePlanningError) as raised:
            plan_stage(self.root)
        self.assertEqual(raised.exception.code, "DISCOVERY_NOT_PASS")
        self.assertFalse((self.root / ".research" / "stage-state.json").exists())

    def test_cli_rejects_noncanonical_state_before_writing_outputs(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = stage_cli_main(
                [
                    "--repo",
                    str(self.root),
                    "--state",
                    ".research/other-state.json",
                    "plan-stage",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("STATE_PATH_INVALID", errors.getvalue())
        self.assertFalse((self.root / ".research" / "stage-state.json").exists())
        self.assertFalse((self.root / STAGE_PLAN_RELATIVE_PATH).exists())
        self.assertFalse((self.root / ".research" / "stages").exists())

    def test_idempotent_reuse_revalidates_tampered_contract(self) -> None:
        first = plan_stage(self.root)
        contract_path = self.root / first["stage_contract_path"]
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract["stage_goal"] = "tampered"
        contract_path.write_text(json.dumps(contract, sort_keys=True), encoding="utf-8")
        plan_bytes = (self.root / STAGE_PLAN_RELATIVE_PATH).read_bytes()
        state_bytes = (self.root / ".research" / "stage-state.json").read_bytes()
        with self.assertRaises(StagePlanningError):
            plan_stage(self.root)
        self.assertEqual(plan_bytes, (self.root / STAGE_PLAN_RELATIVE_PATH).read_bytes())
        self.assertEqual(state_bytes, (self.root / ".research" / "stage-state.json").read_bytes())

    def test_verify_rejects_contract_path_and_contract_schema_tampering(self) -> None:
        result = plan_stage(self.root)
        plan_path = self.root / STAGE_PLAN_RELATIVE_PATH
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["stage_contract_path"] = "outside-contract.json"
        plan_path.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
        with self.assertRaises(StagePlanningError):
            verify_stage_plan(self.root)

        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["stage_contract_path"] = result["stage_contract_path"]
        plan_path.write_text(json.dumps(plan, sort_keys=True), encoding="utf-8")
        contract_path = self.root / result["stage_contract_path"]
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        contract.pop("stage_goal")
        contract_path.write_text(json.dumps(contract, sort_keys=True), encoding="utf-8")
        with self.assertRaises(StagePlanningError):
            verify_stage_plan(self.root)

    def test_verify_recomputes_input_digests_and_detects_changes(self) -> None:
        plan_stage(self.root)
        context_path = self.root / ".research" / "PROJECT_CONTEXT.md"
        context_path.write_text(context_path.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        with self.assertRaises(StagePlanningError) as raised:
            verify_stage_plan(self.root)
        self.assertEqual(raised.exception.code, "STAGE_PLAN_INPUT_MISMATCH")


if __name__ == "__main__":
    unittest.main()
