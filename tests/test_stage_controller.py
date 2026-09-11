"""Scenario A-G for the product-facing Stage Controller layer."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from src.stage_controller import (
    StageController,
    StageControllerError,
    StageState,
    validate_stage_contract_v1,
)
from src.contracts import ContractValidationError


def contract(stage_id: str = "stage-a") -> dict:
    return {
        "schema_version": "stage_contract.v1",
        "project_id": "disposable-project",
        "repository_root": "D:/work/research_tools/disposable-stage",
        "stage_id": stage_id,
        "stage_name": "bounded fixture stage",
        "project_goal": "produce a user-visible bounded artifact",
        "stage_goal": "make the fixture artifact measurable",
        "user_visible_goal": "show a measurable fixture artifact to the user",
        "inputs": ["fixtures/input.txt"],
        "protected_paths": [".git", "production"],
        "allowed_paths": [".research", "tests"],
        "acceptance_description": "all checks pass and the artifact is reviewable",
        "required_checks": ["unit", "measurement"],
        "review_artifact_requirements": ["metrics_summary", "before_after"],
        "baseline": {"digest_source": "fixture-baseline", "metric": 1.0},
        "status": "PLANNED",
    }


def ready_result() -> dict:
    return {
        "status": "SUCCEEDED",
        "stage_id": "stage-a",
        "iteration_index": 1,
        "required_checks": {"unit": "PASS", "measurement": "PASS"},
        "review_artifacts": [
            {"type": "metrics_summary", "uri": "artifacts/metrics.txt"},
            {"type": "before_after", "uri": "artifacts/before-after.png"},
        ],
        "baseline_digest": "placeholder",
    }


class StageControllerScenarios(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = StageController(contract())
        self.controller.start_stage()
        self.baseline_digest = self.controller.state["baseline_digest"]

    def _ready(self) -> dict:
        result = ready_result()
        result["baseline_digest"] = self.baseline_digest
        return self.controller.mark_stage_ready(result)

    def test_scenario_a_start_is_explicit_and_contract_is_detached(self):
        original = contract("planned")
        controller = StageController(original)
        original["stage_goal"] = "attempted mutation"
        self.assertEqual(controller.show_stage("planned")["contract"]["stage_goal"], contract("planned")["stage_goal"])
        self.assertEqual(controller.show_stage("planned")["status"], StageState.PLANNED.value)
        controller.start_stage("planned")
        self.assertEqual(controller.show_stage("planned")["status"], StageState.ACTIVE.value)

    def test_scenario_b_normal_primary_is_default_and_goal_cannot_be_rewritten(self):
        request = self.controller.request_executor()
        self.assertEqual(request["request"]["lane"], "PRIMARY")
        self.assertEqual(request["request"]["mode"], "PRIMARY")
        self.assertEqual(request["request"]["objective"], contract()["stage_goal"])
        with self.assertRaises(ContractValidationError):
            self.controller.record_executor_result({"stage_goal": "model changed goal"})

    def test_scenario_c_explicit_major_challenger_has_one_challenger_maximum(self):
        self.controller.request_executor(mode="MAJOR_CHALLENGER", lane="PRIMARY")
        first = self.controller.request_executor(mode="MAJOR_CHALLENGER", lane="CHALLENGER")
        self.assertEqual(first["request"]["lane"], "CHALLENGER")
        self.assertEqual(self.controller.state["lanes"], ["PRIMARY", "CHALLENGER"])
        with self.assertRaises(StageControllerError):
            self.controller.request_executor(mode="MAJOR_CHALLENGER", lane="CHALLENGER")
        with self.assertRaises(StageControllerError):
            self.controller.request_executor(lane="CHALLENGER")

    def test_rejected_challenger_request_does_not_mutate_lane_bookkeeping(self):
        before = self.controller.state
        with self.assertRaises((StageControllerError, ContractValidationError)):
            self.controller.request_executor(mode="MAJOR_CHALLENGER", lane="CHALLENGER", iteration_index=0)
        after = self.controller.state
        self.assertEqual(after["challenger_count"], before["challenger_count"])
        self.assertEqual(after["lanes"], before["lanes"])
        self.assertEqual(after["executor_requests"], before["executor_requests"])

    def test_scenario_d_human_gate_is_decision_data_not_a_new_stage_state(self):
        event = self.controller.apply_decision("HUMAN_GATE", rationale="goal may need user judgment")
        self.assertEqual(event["stage"]["status"], StageState.ACTIVE.value)
        self.assertNotIn("HUMAN_GATE", StageState.__members__)
        with self.assertRaises(StageControllerError):
            self.controller.request_consultation(evidence_digest="evidence-1")
        resolved = self.controller.resolve_human_gate(True, rationale="keep the declared goal")
        self.assertEqual(resolved["stage"]["status"], StageState.ACTIVE.value)

    def test_scenario_e_reject_preserves_same_stage_and_feedback(self):
        self._ready()
        rejected = self.controller.reject_stage(rationale="need one more artifact")
        self.assertEqual(rejected["stage"]["status"], StageState.ACTIVE.value)
        self.assertEqual(rejected["stage"]["contract"]["stage_id"], "stage-a")
        self.assertEqual(rejected["stage"]["reviews"][-1]["outcome"], "REJECTED")
        self.assertEqual(rejected["stage"]["reviews"][-1]["rationale"], "need one more artifact")

    def test_scenario_f_approved_stage_does_not_start_next_stage(self):
        self._ready()
        approved = self.controller.approve_stage(rationale="human accepted candidate")
        self.assertEqual(approved["stage"]["status"], StageState.APPROVED.value)
        self.controller.register_stage(contract("stage-next"))
        self.assertEqual(self.controller.show_stage("stage-next")["status"], StageState.PLANNED.value)
        self.assertIsNone(self.controller.state["active_stage_id"])
        with self.assertRaises(StageControllerError):
            self.controller.request_executor("stage-next")
        started = self.controller.start_stage("stage-next")
        self.assertEqual(started["stage"]["status"], StageState.ACTIVE.value)

    def test_scenario_g_stop_fails_closed_for_executor_and_consultation(self):
        stopped = self.controller.stop_stage(rationale="user stopped this stage")
        self.assertEqual(stopped["stage"]["status"], StageState.STOPPED.value)
        with self.assertRaises(StageControllerError):
            self.controller.request_executor()
        with self.assertRaises(StageControllerError):
            self.controller.request_consultation(evidence_digest="new-evidence")
        with self.assertRaises(StageControllerError):
            self.controller.record_executor_result({"status": "SUCCEEDED"})
        with self.assertRaises(StageControllerError):
            self.controller.start_stage()


class StageControllerContractAndPersistence(unittest.TestCase):
    def test_stage_contract_v1_requires_all_stage_fields_and_planned_status(self):
        checked = validate_stage_contract_v1(contract())
        self.assertEqual(checked["lane_mode"], "PRIMARY")
        self.assertEqual(checked["status"], "PLANNED")
        for field in (
            "project_id", "repository_root", "stage_id", "stage_name", "project_goal",
            "stage_goal", "user_visible_goal", "inputs", "protected_paths", "allowed_paths",
            "acceptance_description", "required_checks", "review_artifact_requirements", "baseline", "status",
        ):
            incomplete = copy.deepcopy(contract())
            incomplete.pop(field)
            with self.subTest(field=field):
                with self.assertRaises(ContractValidationError):
                    validate_stage_contract_v1(incomplete)
        invalid = contract()
        invalid["status"] = "ACTIVE"
        with self.assertRaises(ContractValidationError):
            validate_stage_contract_v1(invalid)

    def test_persisted_state_rehydrates_and_rejects_tampered_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stage-state.json"
            controller = StageController(contract(), state_path=path)
            controller.start_stage()
            controller.request_consultation(mode="NORMAL", evidence_digest="evidence-1")
            self.assertTrue(path.is_file())
            restored = StageController.from_state(path)
            self.assertEqual(restored.status, StageState.ACTIVE.value)
            self.assertEqual(restored.state["consulted_evidence"], ["NORMAL:evidence-1"])
            with self.assertRaises(StageControllerError):
                restored.request_consultation(mode="NORMAL", evidence_digest="evidence-1")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["stages"]["stage-a"]["baseline"]["metric"] = 99
            tampered = Path(directory) / "tampered.json"
            tampered.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(StageControllerError):
                StageController.from_state(tampered)

    def _persisted_payload(self) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stage-state.json"
            controller = StageController(contract(), state_path=path)
            controller.start_stage()
            controller.request_consultation(mode="NORMAL", evidence_digest="evidence-1")
            return json.loads(path.read_text(encoding="utf-8"))

    def test_persisted_state_rejects_tampered_revision_or_event_sequence(self):
        payload = self._persisted_payload()
        with tempfile.TemporaryDirectory() as directory:
            tampered = Path(directory) / "tampered-revision.json"
            payload["revision"] -= 1
            tampered.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(StageControllerError):
                StageController.from_state(tampered)

        payload = self._persisted_payload()
        with tempfile.TemporaryDirectory() as directory:
            tampered = Path(directory) / "tampered-event-sequence.json"
            payload["events"][1]["revision"] = payload["events"][1]["revision"] + 1
            tampered.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(StageControllerError):
                StageController.from_state(tampered)

    def test_persisted_state_rejects_tampered_event_body_or_id(self):
        payload = self._persisted_payload()
        with tempfile.TemporaryDirectory() as directory:
            tampered = Path(directory) / "tampered-event-body.json"
            payload["events"][1]["details"]["actor"] = "tampered"
            tampered.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(StageControllerError):
                StageController.from_state(tampered)

        payload = self._persisted_payload()
        with tempfile.TemporaryDirectory() as directory:
            tampered = Path(directory) / "tampered-event-id.json"
            payload["events"][1]["event_id"] = "event-0000000000000000"
            tampered.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(StageControllerError):
                StageController.from_state(tampered)

    def test_persisted_state_rejects_stage_event_mirror_tampering(self):
        payload = self._persisted_payload()
        with tempfile.TemporaryDirectory() as directory:
            tampered = Path(directory) / "tampered-stage-events.json"
            payload["stages"]["stage-a"]["events"].pop()
            tampered.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(StageControllerError):
                StageController.from_state(tampered)

    def test_normal_and_fresh_consultations_are_bounded_and_deduplicated(self):
        controller = StageController(contract())
        controller.start_stage()
        normal = controller.request_consultation(mode="NORMAL", evidence_digest="e1", conversation_id="conv-a")
        fresh = controller.request_consultation(mode="FRESH", evidence_digest="e1", conversation_id="conv-b")
        self.assertEqual(normal["request"]["mode"], "NORMAL")
        self.assertEqual(fresh["request"]["mode"], "FRESH")
        with self.assertRaises(StageControllerError):
            controller.request_consultation(mode="FRESH", evidence_digest="e1")

    def test_stage_ready_requires_checks_artifacts_and_matching_baseline(self):
        controller = StageController(contract())
        controller.start_stage()
        with self.assertRaises(ContractValidationError):
            controller.mark_stage_ready(
                {"status": "SUCCEEDED", "required_checks": {"unit": "PASS"}, "review_artifacts": []}
            )
        result = ready_result()
        result["baseline_digest"] = controller.state["baseline_digest"]
        ready = controller.mark_stage_ready(result)
        self.assertEqual(ready["stage"]["status"], StageState.STAGE_READY.value)

    def test_invalid_ready_result_does_not_advance_iteration(self):
        controller = StageController(contract())
        controller.start_stage()
        request = controller.request_executor()
        with self.assertRaises(ContractValidationError):
            controller.record_executor_result(
                {
                    "status": "SUCCEEDED",
                    "stage_ready": True,
                    "required_checks": {"unit": "PASS"},
                    "review_artifacts": [],
                },
                request_id=request["request"]["request_id"],
            )
        self.assertEqual(controller.state["iteration_index"], 0)
        self.assertIsNone(controller.state["latest_result"])

    def test_unknown_result_status_fails_closed(self):
        controller = StageController(contract())
        controller.start_stage()
        with self.assertRaises(ContractValidationError):
            controller.record_executor_result({"status": "MAYBE"})
        self.assertEqual(controller.state["iteration_index"], 0)


if __name__ == "__main__":
    unittest.main()
