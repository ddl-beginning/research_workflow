from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from src.execution_integration_planning import (
    EXECUTION_INTEGRATION_STAGE_ID,
    ExecutionIntegrationPlanningError,
    plan_execution_integration_stage,
    start_execution_integration_stage,
    verify_execution_integration_stage_plan,
)
from src.stage_controller import StageController, StageState


def _source_contract(root: Path, *, target: str | None = None) -> dict:
    contract = {
        "schema_version": "stage_contract.v1",
        "plan_id": "plan-implementation-fixture",
        "project_id": "delivery-fixture",
        "repository_root": str(root),
        "stage_id": "implementation",
        "stage_name": "implementation",
        "project_goal": "deliver a bounded local result",
        "stage_goal": "produce a reviewed result",
        "user_visible_goal": "show the reviewed result",
        "inputs": ["input.txt"],
        "protected_paths": [".git", ".research/stage-state.json"],
        "allowed_paths": [".research/stages/implementation"],
        "acceptance_description": "the implementation result is reviewed",
        "required_checks": ["unit"],
        "review_artifact_requirements": ["summary"],
        "baseline": {
            "brief_digest": "1" * 64,
            "design_digest": "2" * 64,
            "design_package_digest": "3" * 64,
        },
        "status": "PLANNED",
    }
    if target is not None:
        contract["integration_target"] = target
    return contract


class ExecutionIntegrationStagePlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="execution-integration-plan-")
        self.root = Path(self.temp.name)
        (self.root / "input.txt").write_text("fixture\n", encoding="utf-8")
        self.target = ".research/stages/implementation/notebook_storage.py"
        target_path = self.root / self.target
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("reviewed result\n", encoding="utf-8")
        state_path = self.root / ".research" / "stage-state.json"
        self.controller = StageController(_source_contract(self.root), state_path=state_path)
        self.controller.start_stage("implementation", actor="fixture", rationale="fixture start")
        self.controller.mark_stage_ready(
            {
                "status": "SUCCEEDED",
                "baseline_digest": self.controller.state["baseline_digest"],
                "required_checks": {"unit": "PASS"},
                "review_artifacts": [{"type": "summary", "uri": "summary.txt"}],
            },
            stage_id="implementation",
        )
        self.controller.approve_stage("implementation", actor="fixture", rationale="fixture closeout")
        source_contract_path = self.root / ".research" / "stages" / "implementation" / "contract.json"
        source_contract_path.parent.mkdir(parents=True, exist_ok=True)
        source_contract_path.write_text(
            json.dumps(self.controller.show_stage("implementation")["contract"], sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_registers_distinct_stage_and_starts_through_controller(self) -> None:
        planned = plan_execution_integration_stage(self.root, integration_target=self.target)
        self.assertEqual(planned["stage_id"], EXECUTION_INTEGRATION_STAGE_ID)
        self.assertEqual(planned["stage_status"], StageState.PLANNED.value)
        self.assertEqual(planned["source_stage_status"], StageState.APPROVED.value)
        self.assertTrue((self.root / planned["stage_contract_path"]).is_file())
        self.assertTrue((self.root / planned["stage_plan_path"]).is_file())

        state_path = self.root / ".research" / "stage-state.json"
        state = StageController.from_state(state_path)
        self.assertEqual(state.show_stage("implementation")["status"], StageState.APPROVED.value)
        self.assertEqual(state.show_stage(EXECUTION_INTEGRATION_STAGE_ID)["status"], StageState.PLANNED.value)
        revision_before_start = state.state["revision"]

        started = start_execution_integration_stage(self.root)
        self.assertEqual(started["stage"]["status"], StageState.ACTIVE.value)
        state = StageController.from_state(state_path)
        self.assertEqual(state.show_stage("implementation")["status"], StageState.APPROVED.value)
        self.assertEqual(state.show_stage(EXECUTION_INTEGRATION_STAGE_ID)["status"], StageState.ACTIVE.value)
        self.assertEqual(state.state["revision"], revision_before_start + 1)
        self.assertEqual(verify_execution_integration_stage_plan(self.root)["stage_status"], StageState.ACTIVE.value)

    def test_rerun_is_idempotent_and_does_not_touch_previous_stage(self) -> None:
        first = plan_execution_integration_stage(self.root, integration_target=self.target)
        state_path = self.root / ".research" / "stage-state.json"
        state_before = state_path.read_bytes()
        plan_path = self.root / first["stage_plan_path"]
        plan_before = plan_path.read_bytes()
        second = plan_execution_integration_stage(self.root, integration_target=self.target)
        self.assertTrue(second["idempotent_reuse"])
        self.assertEqual(state_before, state_path.read_bytes())
        self.assertEqual(plan_before, plan_path.read_bytes())
        state = StageController.from_state(state_path)
        self.assertEqual(state.show_stage("implementation")["status"], StageState.APPROVED.value)
        self.assertEqual(state.show_stage(EXECUTION_INTEGRATION_STAGE_ID)["status"], StageState.PLANNED.value)

    def test_conflicting_target_fails_closed_before_registration(self) -> None:
        # Rebind the source contract to an explicit planning target, then ask
        # for a conflicting one.  The planner must expose the ambiguity rather
        # than treating the changed file list as an implicit target.
        source = self.controller.show_stage("implementation")["contract"]
        source["integration_target"] = self.target
        self.controller = StageController.from_state(self.root / ".research" / "stage-state.json")
        # The persisted source contract is immutable; emulate a source result
        # carrying a target instead, which is also a supported source-of-truth.
        source_record = self.controller._stages["implementation"]  # noqa: SLF001 - fixture construction
        source_record["latest_stage_result"]["integration_target"] = self.target
        source_record["latest_result"]["integration_target"] = self.target
        self.controller.save_state()
        conflicting = ".research/stages/implementation/other.py"
        (self.root / conflicting).write_text("other\n", encoding="utf-8")
        with self.assertRaises(ExecutionIntegrationPlanningError) as raised:
            plan_execution_integration_stage(self.root, integration_target=conflicting)
        self.assertEqual(raised.exception.code, "EXECUTION_INTEGRATION_TARGET_AMBIGUOUS")
        self.assertFalse((self.root / ".research" / "stages" / EXECUTION_INTEGRATION_STAGE_ID).exists())

    def test_non_approved_source_cannot_create_delivery_stage(self) -> None:
        state_path = self.root / ".research" / "stage-state.json"
        # The fixture state is already APPROVED.  Recreate the source Stage
        # from a clean path so this test exercises the real ACTIVE gate rather
        # than silently reloading the approved snapshot.
        state_path.unlink()
        controller = StageController(_source_contract(self.root), state_path=state_path)
        controller.start_stage("implementation")
        with self.assertRaises(ExecutionIntegrationPlanningError) as raised:
            plan_execution_integration_stage(self.root, integration_target=self.target)
        self.assertEqual(raised.exception.code, "EXECUTION_INTEGRATION_SOURCE_NOT_CLOSED")
        self.assertFalse((self.root / ".research" / "stages" / EXECUTION_INTEGRATION_STAGE_ID).exists())


if __name__ == "__main__":
    unittest.main()
