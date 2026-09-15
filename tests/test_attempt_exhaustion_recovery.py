"""Regression coverage for attempt-budget exhaustion recovery."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from importlib import import_module

from src.workflow_v2_contracts import assess_observation, observation_identity
from src.workflow_v2_controller import StageController, WorkflowV2ControllerError


class AttemptExhaustionRecoveryRegression(unittest.TestCase):
    def setUp(self):
        self.fixture = import_module("tests.test_workflow_v2_controller").WorkflowV2ControllerScenarios()

    def _exhaust_current_iteration(self, *, terminal="FAILED", state_path=None, **stage_overrides):
        controller = StageController(workspace_id="workspace-12345678", state_path=state_path)
        stage_id = "stage-exhaustion-12345678"
        controller.register_stage(
            self.fixture.stage(stage_id, **stage_overrides),
            command_id="command-register-exhaustion-12345678",
        )
        controller.start(stage_id, command_id="command-start-exhaustion-12345678")
        attempts = []
        observations = []
        for index in range(2):
            request = controller.request_execution(
                stage_id,
                reason="RETRY" if index else "INITIAL",
                command_id=f"command-request-exhaustion-{index}-12345678",
            )
            attempt = request["attempt"]
            attempts.append(attempt)
            observation = self.fixture.observation(
                terminal if index == 1 else "FAILED",
                stage_id=stage_id,
                attempt_id=attempt["attempt_id"],
                iteration_id=attempt["iteration_id"],
                observation_id=f"observation-exhaustion-{index}-12345678",
                evidence_manifest_digest=f"manifest-exhaustion-{index}-12345678",
                provider_result_digest=f"provider-exhaustion-{index}-12345678",
            )
            observations.append(observation)
            controller.record_observation(
                stage_id,
                observation,
                effect_state="SETTLED",
                command_id=f"command-observe-exhaustion-{index}-12345678",
            )
        return controller, stage_id, attempts, observations

    def _assess_latest(self, controller, stage_id, attempt, observation):
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
            command_id="command-assess-exhaustion-12345678",
        )
        return assessment

    def test_attempt_zero_projects_normal_execution(self):
        controller = StageController(workspace_id="workspace-12345678")
        stage_id = "stage-attempt-zero-12345678"
        controller.register_stage(self.fixture.stage(stage_id), command_id="command-register-zero-12345678")
        controller.start(stage_id, command_id="command-start-zero-12345678")

        self.assertEqual(controller.show_stage(stage_id)["attempt_count"], 0)
        self.assertEqual(controller.show_stage(stage_id)["next_action"], "REQUEST_EXECUTION")

    def test_pre_dispatch_contract_failure_does_not_consume_provider_attempt(self):
        controller = StageController(workspace_id="workspace-12345678")
        stage_id = "stage-pre-dispatch-12345678"
        controller.register_stage(self.fixture.stage(stage_id), command_id="command-register-pre-dispatch-12345678")
        controller.start(stage_id, command_id="command-start-pre-dispatch-12345678")

        with self.assertRaisesRegex(WorkflowV2ControllerError, "INTENT_COMMIT_REQUIRES_RECOVERABLE_PROVIDER_HANDOFF"):
            controller.dispatch(
                "REQUEST_EXECUTION",
                subject_id=stage_id,
                payload={"request": {"operation": "REQUEST_EXECUTION"}},
                command_id="command-pre-dispatch-contract-failure-12345678",
            )

        budget = controller.attempt_budget_status(stage_id)
        self.assertEqual(budget["counted_attempts"], 0)
        self.assertFalse(budget["per_iteration_exhausted"])
        self.assertEqual(controller.show_stage(stage_id)["attempt_count"], 0)

    def test_real_provider_timeout_consumes_provider_attempt(self):
        controller = StageController(workspace_id="workspace-12345678")
        stage_id = "stage-provider-timeout-12345678"
        controller.register_stage(self.fixture.stage(stage_id), command_id="command-register-provider-timeout-12345678")
        controller.start(stage_id, command_id="command-start-provider-timeout-12345678")
        requested = controller.request_execution(stage_id, command_id="command-request-provider-timeout-12345678")
        attempt = requested["attempt"]
        observation = self.fixture.observation(
            "FAILED", stage_id=stage_id, attempt_id=attempt["attempt_id"], iteration_id=attempt["iteration_id"],
            observation_id="observation-provider-timeout-12345678", provider_result_digest="provider-timeout-12345678",
        )
        observation["failure"]["code"] = "PROVIDER_TIMEOUT"
        observation["observation_id"] = observation_identity(observation)
        controller.record_observation(stage_id, observation, effect_state="SETTLED", command_id="command-observe-provider-timeout-12345678")

        budget = controller.attempt_budget_status(stage_id)
        self.assertEqual(budget["counted_attempts"], 1)
        self.assertEqual(budget["counted_attempt_ids"], [attempt["attempt_id"]])

    def test_real_provider_failure_consumes_provider_attempt(self):
        controller = StageController(workspace_id="workspace-12345678")
        stage_id = "stage-provider-failure-12345678"
        controller.register_stage(self.fixture.stage(stage_id), command_id="command-register-provider-failure-12345678")
        controller.start(stage_id, command_id="command-start-provider-failure-12345678")
        requested = controller.request_execution(stage_id, command_id="command-request-provider-failure-12345678")
        attempt = requested["attempt"]
        observation = self.fixture.observation(
            "FAILED", stage_id=stage_id, attempt_id=attempt["attempt_id"], iteration_id=attempt["iteration_id"],
            observation_id="observation-provider-failure-12345678", provider_result_digest="provider-failure-12345678",
        )
        observation["failure"]["code"] = "PROVIDER_EXECUTION_FAILED"
        observation["observation_id"] = observation_identity(observation)
        controller.record_observation(stage_id, observation, effect_state="SETTLED", command_id="command-observe-provider-failure-12345678")

        budget = controller.attempt_budget_status(stage_id)
        self.assertEqual(budget["total_counted_attempts"], 1)

    def test_attempt_one_of_two_projects_next_execution(self):
        controller = StageController(workspace_id="workspace-12345678")
        stage_id = "stage-attempt-one-12345678"
        controller.register_stage(self.fixture.stage(stage_id), command_id="command-register-one-12345678")
        controller.start(stage_id, command_id="command-start-one-12345678")
        request = controller.request_execution(stage_id, command_id="command-request-one-12345678")
        attempt = request["attempt"]
        observation = self.fixture.observation(
            "FAILED",
            stage_id=stage_id,
            attempt_id=attempt["attempt_id"],
            iteration_id=attempt["iteration_id"],
            observation_id="observation-one-12345678",
            evidence_manifest_digest="manifest-one-12345678",
            provider_result_digest="provider-one-12345678",
        )
        controller.record_observation(stage_id, observation, effect_state="SETTLED", command_id="command-observe-one-12345678")

        self.assertEqual(controller.show_stage(stage_id)["attempt_count"], 1)
        self.assertEqual(controller.show_stage(stage_id)["next_action"], "REQUEST_EXECUTION")

    def test_exhausted_iteration_projects_assessment_recovery(self):
        controller, stage_id, _, _ = self._exhaust_current_iteration()

        projection = controller.resume_projection()

        self.assertEqual(projection["stage"]["attempt_count"], 2)
        self.assertIsNone(projection["stage"]["current_assessment_id"])
        self.assertEqual(projection["stage"]["next_action"], "ASSESS_RESULT")
        with self.assertRaisesRegex(WorkflowV2ControllerError, "per-iteration attempt budget exhausted"):
            controller.request_execution(
                stage_id,
                reason="RETRY",
                command_id="command-request-after-exhaustion-12345678",
            )

    def test_exhaustion_assessment_can_authorize_next_iteration(self):
        controller, stage_id, attempts, observations = self._exhaust_current_iteration()

        assessment = self._assess_latest(controller, stage_id, attempts[-1], observations[-1])
        decision = self.fixture.gpt_decision(assessment["assessment_id"])
        result = controller.apply_gpt_decision(
            stage_id,
            decision,
            choice="REPLAN",
            replan_subtype="NEXT_ITERATION",
            technical_change_digest="technical-change-exhaustion-12345678",
            solution_fingerprint="solution-next-iteration-12345678",
            command_id="command-next-iteration-exhaustion-12345678",
        )

        self.assertEqual(result["stage"]["iteration_count"], 2)
        self.assertEqual(result["stage"]["attempt_count"], 2)
        self.assertEqual(result["stage"]["next_action"], "REQUEST_EXECUTION")
        self.assertEqual(result["stage"]["project_id"], controller.state["stages"][stage_id]["project_id"])
        self.assertEqual(result["stage"]["stage_id"], stage_id)
        self.assertEqual(result["stage"]["objective_fingerprint"], controller.state["stages"][stage_id]["objective_fingerprint"])
        self.assertEqual(
            len(controller._attempts_for(controller.state, stage_id, result["stage"]["current_iteration_id"])),
            0,
        )
        self.assertEqual(len(controller._attempts_for(controller.state, stage_id, attempts[0]["iteration_id"])), 2)
        request = controller.request_execution(
            stage_id,
            command_id="command-request-after-next-iteration-12345678",
        )
        self.assertEqual(request["attempt"]["iteration_id"], result["stage"]["current_iteration_id"])

    def test_stage_ready_assessment_does_not_create_next_iteration(self):
        controller, stage_id, attempts, observations = self._exhaust_current_iteration(terminal="SUCCEEDED")

        assessment = self._assess_latest(controller, stage_id, attempts[-1], observations[-1])
        result = controller.apply_gpt_decision(
            stage_id,
            self.fixture.gpt_decision(assessment["assessment_id"]),
            choice="STAGE_READY",
            command_id="command-stage-ready-after-exhaustion-12345678",
        )

        self.assertEqual(result["stage"]["status"], "READY")
        self.assertEqual(result["stage"]["iteration_count"], 1)
        self.assertEqual(result["stage"]["current_assessment_id"], assessment["assessment_id"])

    def test_continue_does_not_bypass_global_iteration_limit(self):
        budgets = dict(self.fixture.stage()["budgets"])
        budgets["max_iterations"] = 1
        controller, stage_id, attempts, observations = self._exhaust_current_iteration(budgets=budgets)
        assessment = self._assess_latest(controller, stage_id, attempts[-1], observations[-1])

        result = controller.apply_gpt_decision(
            stage_id,
            self.fixture.gpt_decision(assessment["assessment_id"]),
            choice="CONTINUE",
            command_id="command-continue-global-limit-12345678",
        )

        self.assertFalse(result.get("auto_next_iteration", False))
        self.assertEqual(result["stage"]["iteration_count"], 1)
        self.assertTrue(controller.attempt_budget_status(stage_id)["max_iteration_exhausted"])
        with self.assertRaisesRegex(WorkflowV2ControllerError, "attempt budget exhausted"):
            controller.request_execution(stage_id, reason="CONTINUE", command_id="command-no-global-bypass-12345678")

    def test_pending_human_gate_remains_the_projected_control_action(self):
        controller, stage_id, _, _ = self._exhaust_current_iteration()
        decision = {
            "schema_version": "decision.v2",
            "decision_id": "decision-human-exhaustion-12345678",
            "actor_kind": "HUMAN",
            "boundary": "STAGE_PLANNING",
            "subject_id": stage_id,
            "subject_digest": "stage-subject-exhaustion-12345678",
            "subject_version": 1,
            "allowed_choices": ["APPROVE", "REJECT"],
            "requested_action": "resolve bounded recovery gate",
            "provenance": {"source": "human_instruction", "receipt_digest": "receipt-human-exhaustion-12345678"},
            "supersedes": None,
        }
        controller.request_decision(decision, command_id="command-human-gate-exhaustion-12345678")

        self.assertEqual(controller.show_stage(stage_id)["next_action"], "APPLY_DECISION")

    def test_terminal_stop_preserves_terminal_semantics_after_exhaustion(self):
        controller, stage_id, _, _ = self._exhaust_current_iteration()

        result = controller.stop(stage_id, command_id="command-stop-after-exhaustion-12345678")

        self.assertEqual(result["stage"]["status"], "STOPPED")
        self.assertIsNone(result["stage"]["next_action"])

    def test_reload_keeps_exhaustion_recovery_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            controller, stage_id, attempts, observations = self._exhaust_current_iteration(state_path=path)
            reloaded = StageController.from_state(path)
            self.assertEqual(reloaded.show_stage(stage_id)["next_action"], "ASSESS_RESULT")

            assessment = self._assess_latest(reloaded, stage_id, attempts[-1], observations[-1])
            reloaded = StageController.from_state(path)
            first = reloaded.apply_gpt_decision(
                stage_id,
                self.fixture.gpt_decision(assessment["assessment_id"]),
                choice="REPLAN",
                replan_subtype="NEXT_ITERATION",
                technical_change_digest="technical-change-reload-12345678",
                solution_fingerprint="solution-reload-12345678",
                command_id="command-next-reload-12345678",
            )
            reloaded = StageController.from_state(path)
            second = reloaded.apply_gpt_decision(
                stage_id,
                self.fixture.gpt_decision(assessment["assessment_id"]),
                choice="REPLAN",
                replan_subtype="NEXT_ITERATION",
                technical_change_digest="technical-change-reload-12345678",
                solution_fingerprint="solution-reload-12345678",
                command_id="command-next-reload-12345678",
            )

            self.assertEqual(first["stage"]["iteration_count"], 2)
            self.assertEqual(second["stage"]["iteration_count"], 2)
            self.assertEqual(second["stage"]["current_iteration_id"], first["stage"]["current_iteration_id"])
            self.assertEqual(len(reloaded.state["assessments"]), 1)
            self.assertEqual(len(reloaded.state["iterations"]), 2)

    def test_continue_with_exhausted_iteration_advances_before_clearing_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            controller, stage_id, attempts, observations = self._exhaust_current_iteration(state_path=path)
            assessment = self._assess_latest(controller, stage_id, attempts[-1], observations[-1])
            applied = controller.apply_gpt_decision(
                stage_id,
                self.fixture.gpt_decision(assessment["assessment_id"]),
                choice="CONTINUE",
                command_id="command-continue-clears-assessment-12345678",
            )
            self.assertIsNone(controller.show_stage(stage_id)["current_assessment_id"])
            self.assertTrue(applied["auto_next_iteration"])
            self.assertEqual(applied["stage"]["iteration_count"], 2)

            reloaded = StageController.from_state(path)
            with self.assertRaisesRegex(WorkflowV2ControllerError, "current semantic iteration"):
                reloaded.assess_result(
                    stage_id,
                    assessment,
                    command_id="command-replay-assessment-after-continue-12345678",
                )
            self.assertIn(assessment["assessment_id"], reloaded.state["assessments"])


if __name__ == "__main__":
    unittest.main()
