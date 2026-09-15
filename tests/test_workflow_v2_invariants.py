"""One named invariant test for every canonical V2 command."""

from __future__ import annotations

import unittest

from src.workflow_v2_controller import StageController, WorkflowV2ControllerError
from src.workflow_v2_contracts import assess_observation
import tests.test_workflow_v2_controller as _controller_fixtures


class CanonicalWorkflowV2Invariants(unittest.TestCase):
    def setUp(self):
        self.fixture = _controller_fixtures.WorkflowV2ControllerScenarios()

    def _active(self, stage_id="stage-12345678"):
        controller = StageController(workspace_id="workspace-12345678")
        controller.register_stage(self.fixture.stage(stage_id))
        controller.start(stage_id, command_id=f"command-start-{stage_id}")
        return controller

    def _observed_and_assessed(self, *, stage_id="stage-12345678", terminal="SUCCEEDED"):
        controller = self._active(stage_id)
        request = controller.request_execution(
            stage_id,
            command_id=f"command-request-{stage_id}",
            provenance={"provider": "fixture-provider", "engine_digest": "engine-12345678"},
        )
        attempt = request["attempt"]
        observation = self.fixture.observation(
            terminal,
            stage_id=stage_id,
            attempt_id=attempt["attempt_id"],
            iteration_id=attempt["iteration_id"],
            observation_id=f"observation-{stage_id}",
            evidence_manifest_digest=f"manifest-{stage_id}",
            provider_result_digest=f"provider-{stage_id}",
        )
        controller.record_observation(
            stage_id,
            observation,
            effect_state="SETTLED",
            command_id=f"command-observe-{stage_id}",
        )
        manifest = self.fixture.manifest(
            stage_id=stage_id,
            attempt_id=attempt["attempt_id"],
            manifest_id=observation["evidence_manifest_digest"],
        )
        assessment = assess_observation(
            observation,
            manifest,
            baseline_digest="baseline-12345678",
            validator_code_digest="validator-12345678",
            validation_contract_revision="admission.v2",
        )
        controller.assess_result(
            stage_id,
            assessment,
            command_id=f"command-assess-{stage_id}",
        )
        return controller, assessment

    def _ready(self, stage_id="stage-12345678"):
        controller, assessment = self._observed_and_assessed(stage_id=stage_id)
        controller.apply_gpt_decision(
            stage_id,
            self.fixture.gpt_decision(assessment["assessment_id"]),
            choice="STAGE_READY",
            command_id=f"command-ready-{stage_id}",
        )
        return controller

    def _human_decision(self, stage_id="stage-12345678"):
        return {
            "schema_version": "decision.v2",
            "decision_id": f"decision-human-{stage_id}",
            "actor_kind": "HUMAN",
            "boundary": "STAGE_PLANNING",
            "subject_id": stage_id,
            "subject_digest": "stage-subject-12345678",
            "subject_version": 1,
            "allowed_choices": ["APPROVE", "REJECT"],
            "requested_action": "approve bounded plan",
            "provenance": {"source": "human_instruction", "receipt_digest": "receipt-human-12345678"},
            "supersedes": None,
        }

    def test_register_stage_invariants(self):
        controller = StageController(workspace_id="workspace-12345678")
        result = controller.register_stage(self.fixture.stage(), command_id="command-register-invariant-12345678")
        self.assertEqual(result["stage"]["status"], "PLANNED")

    def test_start_invariants(self):
        controller = self._active()
        self.assertEqual(controller.show_stage()["status"], "ACTIVE")
        self.assertEqual(controller.show_stage()["iteration_count"], 1)

    def test_request_execution_invariants(self):
        controller = self._active()
        result = controller.request_execution("stage-12345678", command_id="command-request-invariant-12345678")
        self.assertEqual(result["attempt"]["status"], "REQUESTED")

    def test_record_observation_invariants(self):
        controller = self._active("stage-record-12345678")
        request = controller.request_execution("stage-record-12345678", command_id="command-record-request-12345678")
        attempt = request["attempt"]
        observation = self.fixture.observation(
            stage_id="stage-record-12345678",
            attempt_id=attempt["attempt_id"],
            iteration_id=attempt["iteration_id"],
            observation_id="observation-record-12345678",
            evidence_manifest_digest="manifest-record-12345678",
            provider_result_digest="provider-record-12345678",
        )
        result = controller.record_observation("stage-record-12345678", observation, effect_state="SETTLED", command_id="command-record-observe-12345678")
        self.assertEqual(result["observation"]["immutable"], True)

    def test_assess_result_invariants(self):
        controller, assessment = self._observed_and_assessed(stage_id="stage-assess-12345678")
        self.assertIn(assessment["assessment_id"], controller.state["assessments"])

    def test_apply_gpt_decision_invariants(self):
        controller, assessment = self._observed_and_assessed(stage_id="stage-gpt-12345678")
        result = controller.apply_gpt_decision(
            "stage-gpt-12345678",
            self.fixture.gpt_decision(assessment["assessment_id"]),
            choice="CONTINUE",
            command_id="command-gpt-continue-12345678",
        )
        self.assertEqual(result["stage"]["next_action"], "REQUEST_EXECUTION")

    def test_advance_iteration_invariants(self):
        controller, assessment = self._observed_and_assessed(stage_id="stage-advance-12345678", terminal="FAILED")
        decision = self.fixture.gpt_decision(assessment["assessment_id"])
        result = controller.apply_gpt_decision(
            "stage-advance-12345678",
            decision,
            choice="REPLAN",
            replan_subtype="NEXT_ITERATION",
            technical_change_digest="technical-change-12345678",
            solution_fingerprint="solution-v2-12345678",
            command_id="command-advance-12345678",
        )
        self.assertEqual(result["stage"]["iteration_count"], 2)

    def test_request_decision_invariants(self):
        controller = StageController(workspace_id="workspace-12345678")
        controller.register_stage(self.fixture.stage(), command_id="command-human-register-12345678")
        result = controller.request_decision(self._human_decision(), command_id="command-human-request-12345678")
        self.assertIsNone(result["decision"]["resolution"])

    def test_apply_decision_invariants(self):
        controller = StageController(workspace_id="workspace-12345678")
        controller.register_stage(self.fixture.stage(), command_id="command-human-apply-register-12345678")
        decision = self._human_decision()
        controller.request_decision(decision, command_id="command-human-apply-request-12345678")
        result = controller.apply_decision(
            "stage-12345678",
            payload={
                "decision_id": decision["decision_id"],
                "subject_digest": decision["subject_digest"],
                "subject_version": decision["subject_version"],
                "choice": "APPROVE",
            },
            command_id="command-human-apply-12345678",
        )
        self.assertEqual(result["decision"]["resolution"], "APPROVE")

    def test_add_dependency_invariants(self):
        controller = StageController(workspace_id="workspace-12345678")
        controller.register_stage(self.fixture.stage(), command_id="command-add-parent-12345678")
        child = self.fixture.stage("child-add-12345678")
        edge = {
            "schema_version": "dependency.v2",
            "parent_id": "stage-12345678",
            "child_id": "child-add-12345678",
            "predicate": "child closed output",
            "input_binding": {"digest": "input-add-12345678"},
            "output_contract_digest": "output-add-12345678",
            "authorization_decision_id": "decision-add-12345678",
            "child_status": "PLANNED",
        }
        self.fixture.authorize_dependency(controller, edge, "command-add-dependency-auth")
        result = controller.add_dependency("stage-12345678", edge, child, command_id="command-add-dependency-12345678")
        self.assertEqual(result["dependency"]["status"], "OPEN")

    def test_satisfy_dependency_invariants(self):
        controller = StageController(workspace_id="workspace-12345678")
        controller.register_stage(self.fixture.stage(), command_id="command-satisfy-parent-12345678")
        child = self.fixture.stage("child-satisfy-12345678")
        edge = {
            "schema_version": "dependency.v2",
            "parent_id": "stage-12345678",
            "child_id": "child-satisfy-12345678",
            "predicate": "child closed output",
            "input_binding": {"digest": "input-satisfy-12345678"},
            "output_contract_digest": "output-satisfy-12345678",
            "authorization_decision_id": "decision-satisfy-12345678",
            "child_status": "PLANNED",
        }
        self.fixture.authorize_dependency(controller, edge, "command-satisfy-dependency-auth")
        controller.add_dependency("stage-12345678", edge, child, command_id="command-satisfy-add-12345678")
        controller.start("child-satisfy-12345678", command_id="command-satisfy-start-12345678")
        request = controller.request_execution("child-satisfy-12345678", command_id="command-satisfy-request-12345678")
        attempt = request["attempt"]
        observation = self.fixture.observation(
            stage_id="child-satisfy-12345678",
            attempt_id=attempt["attempt_id"],
            iteration_id=attempt["iteration_id"],
            observation_id="observation-satisfy-12345678",
            evidence_manifest_digest="manifest-satisfy-12345678",
            provider_result_digest="provider-satisfy-12345678",
        )
        controller.record_observation("child-satisfy-12345678", observation, effect_state="SETTLED", command_id="command-satisfy-observe-12345678")
        assessment = assess_observation(
            observation,
            self.fixture.manifest(stage_id="child-satisfy-12345678", attempt_id=attempt["attempt_id"], manifest_id="manifest-satisfy-12345678"),
            baseline_digest="baseline-12345678",
            validator_code_digest="validator-12345678",
            validation_contract_revision="admission.v2",
        )
        controller.assess_result("child-satisfy-12345678", assessment, command_id="command-satisfy-assess-12345678")
        controller.apply_gpt_decision("child-satisfy-12345678", self.fixture.gpt_decision(assessment["assessment_id"]), choice="STAGE_READY", command_id="command-satisfy-ready-12345678")
        closed = controller.closeout("child-satisfy-12345678", no_integration_required=True, command_id="command-satisfy-close-12345678")
        result = controller.satisfy_dependency(
            "stage-12345678",
            payload={"child_id": "child-satisfy-12345678", "output_contract_digest": "output-satisfy-12345678", "closeout_identity": closed["closeout"]["closeout_id"]},
            command_id="command-satisfy-12345678",
        )
        self.assertEqual(result["dependency"]["status"], "SATISFIED")
        self.assertEqual(result["parent"]["status"], "PLANNED")

    def test_resolve_blocker_invariants(self):
        controller = self._active("stage-resolve-12345678")
        request = controller.request_execution("stage-resolve-12345678", command_id="command-resolve-request-12345678")
        operation_id = request["operation"]["operation_id"]
        controller.apply_receipt("stage-resolve-12345678", operation_id=operation_id, effect_state="UNKNOWN", command_id="command-resolve-unknown-12345678")
        with self.assertRaises(WorkflowV2ControllerError):
            controller.resolve_blocker("stage-resolve-12345678", blocker_id=operation_id, predicate_satisfied=True, command_id="command-resolve-blocker-12345678")

    def test_apply_receipt_invariants(self):
        controller = self._active("stage-receipt-12345678")
        request = controller.request_execution("stage-receipt-12345678", command_id="command-receipt-request-12345678")
        operation_id = request["operation"]["operation_id"]
        controller.apply_receipt("stage-receipt-12345678", operation_id=operation_id, effect_state="UNKNOWN", command_id="command-receipt-unknown-12345678")
        result = controller.apply_receipt(
            "stage-receipt-12345678",
            operation_id=operation_id,
            effect_state="SETTLED",
            settlement_proof="query-receipt-proof-12345678",
            command_id="command-receipt-settle-12345678",
        )
        self.assertEqual(result["operation"]["effect_state"], "SETTLED")

    def test_commit_integration_invariants(self):
        controller = self._ready("stage-integrate-12345678")
        result = controller.commit_integration("stage-integrate-12345678", command_id="command-integrate-12345678")
        self.assertEqual(result["operation"]["status"], "INTENT")

    def test_closeout_invariants(self):
        controller = self._ready("stage-closeout-12345678")
        result = controller.closeout("stage-closeout-12345678", no_integration_required=True, command_id="command-closeout-12345678")
        self.assertEqual(result["stage"]["status"], "CLOSED")

    def test_stop_invariants(self):
        controller = self._active("stage-stop-12345678")
        result = controller.stop(
            "stage-stop-12345678",
            explicit_human_termination=True,
            termination_reason="EXPLICIT_HUMAN_CANCELLATION",
            command_id="command-stop-12345678",
        )
        self.assertEqual(result["stage"]["status"], "STOPPED")


__all__ = ["CanonicalWorkflowV2Invariants"]
