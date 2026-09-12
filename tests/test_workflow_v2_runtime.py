"""Projection and adapter invariants for Phase 3."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from src.workflow_v2_controller import StageController
from src.workflow_v2_runtime import WorkflowRuntimeV2


def stage():
    return {
        "schema_version": "stage.v2",
        "stage_id": "stage-runtime-12345678",
        "workspace_id": "workspace-12345678",
        "project_id": "project-v2",
        "objective_fingerprint": "objective-12345678",
        "target_identity": "target/project",
        "required_capabilities": ["python"],
        "purpose": "DOMAIN",
        "repair_depth": 0,
        "baseline_digest": "baseline-12345678",
        "status": "PLANNED",
        "budgets": {
            "max_iterations": 2,
            "max_attempts_per_iteration": 2,
            "max_attempts_total": 3,
            "max_revalidation_ops": 2,
            "max_validator_revisions": 2,
            "max_dependency_nodes": 4,
            "max_dependency_depth": 2,
            "max_descendant_attempts": 4,
        },
        "owner_stage_id": None,
        "current_iteration_id": None,
        "current_assessment_id": None,
        "predecessor_stage_id": None,
        "semantic_baseline_diff_digest": None,
    }


class WorkflowV2RuntimeProjection(unittest.TestCase):
    def test_runtime_derives_all_route_fields_from_controller(self):
        controller = StageController(workspace_id="workspace-12345678")
        controller.register_stage(stage(), command_id="command-runtime-register-12345678")
        runtime = WorkflowRuntimeV2(controller)
        self.assertEqual(runtime.phase, "RUNNING")
        self.assertEqual(runtime.next_action, "START")
        self.assertEqual(runtime.attempt_count, 0)
        controller.start("stage-runtime-12345678", command_id="command-runtime-start-12345678")
        self.assertEqual(runtime.next_action, "REQUEST_EXECUTION")
        self.assertEqual(runtime.current_stage["iteration_count"], 1)

    def test_legacy_checkpoint_changes_do_not_change_canonical_route(self):
        controller = StageController(workspace_id="workspace-12345678")
        controller.register_stage(stage(), command_id="command-runtime-legacy-12345678")
        runtime = WorkflowRuntimeV2(controller, legacy_checkpoint={"phase": "BLOCKED", "next_action": "RECOVERY", "attempt_count": 999})
        before = runtime.resume()
        legacy = runtime.read_legacy_checkpoint()
        legacy["legacy_checkpoint"]["next_action"] = "START"
        self.assertEqual(runtime.resume(), before)
        self.assertEqual(runtime.next_action, "START")
        self.assertEqual(runtime.read_legacy_checkpoint()["legacy_checkpoint"]["next_action"], "RECOVERY")

    def test_read_only_resume_does_not_advance_revision_or_dispatch(self):
        controller = StageController(workspace_id="workspace-12345678")
        controller.register_stage(stage(), command_id="command-runtime-read-12345678")
        runtime = WorkflowRuntimeV2(controller)
        revision = controller.revision
        for _ in range(3):
            runtime.resume()
            _ = runtime.phase, runtime.next_action, runtime.next_actor, runtime.pending_gate, runtime.attempt_count
        self.assertEqual(controller.revision, revision)
        self.assertEqual(controller.state["attempts"], {})

    def test_submit_is_a_thin_command_adapter(self):
        controller = StageController(workspace_id="workspace-12345678")
        runtime = WorkflowRuntimeV2(controller)
        result = runtime.submit("REGISTER_STAGE", subject_id=stage()["stage_id"], payload={"stage": stage()}, command_id="command-runtime-submit-12345678")
        self.assertEqual(result["stage"]["status"], "PLANNED")
        self.assertEqual(controller.revision, 1)

    def test_initialize_persists_an_empty_identity_bound_journal_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "journal.json"
            controller = StageController(workspace_id="workspace-fresh-12345678", state_path=state_path)
            first = controller.initialize()
            second = controller.initialize()
            self.assertEqual(first, second)
            self.assertEqual(first["revision"], 0)
            self.assertEqual(first["stages"], {})
            self.assertEqual(StageController.from_state(state_path).state, first)


if __name__ == "__main__":
    unittest.main()
