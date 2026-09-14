"""Transition and crash invariants for the canonical V2 controller."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from src.contracts import sha256_json
from src.workflow_v2_controller import StageController, WorkflowV2ControllerError
from src.workflow_v2_contracts import assess_observation, dependency_authorization_digest, observation_identity


class WorkflowV2ControllerScenarios(unittest.TestCase):
    def stage(self, stage_id: str = "stage-12345678", **overrides):
        value = {
            "schema_version": "stage.v2",
            "stage_id": stage_id,
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
        value.update(overrides)
        return value

    def manifest(self, stage_id="stage-12345678", attempt_id="attempt-12345678", **overrides):
        value = {
            "schema_version": "evidence_manifest.v2",
            "manifest_id": "manifest-12345678",
            "stage_id": stage_id,
            "attempt_id": attempt_id,
            "required_artifact_paths": ["artifacts/result.json"],
            "changed_paths": ["artifacts/result.json"],
            "allowed_paths": ["artifacts"],
            "protected_paths": [".git", "secrets"],
            "path_inventory": [{"path": "artifacts/result.json", "sha256": "artifact-12345678"}],
            "complete": True,
        }
        value.update(overrides)
        return value

    def observation(self, terminal="SUCCEEDED", **overrides):
        value = {
            "schema_version": "provider_observation.v2",
            "observation_id": "observation-12345678",
            "stage_id": "stage-12345678",
            "iteration_id": "iteration-command-12345678",
            "attempt_id": "attempt-12345678",
            "provider_result_digest": "provider-result-12345678",
            "evidence_manifest_digest": "manifest-12345678",
            "provider_terminal_status": terminal,
            "raw_provider_claim": {"status": "SUCCEEDED", "retryable": True},
            "provenance": {"provider": "fixture-provider", "engine_digest": "engine-12345678"},
            "failure": None,
            "outputs": {"files": ["artifacts/result.json"]},
            "immutable": True,
        }
        if terminal != "SUCCEEDED":
            value["failure"] = {
                "schema_version": "typed_failure.v2",
                "failure_class": "PROVIDER_FAILURE",
                "code": "provider_failed",
                "operation_id": "operation-command-12345678",
                "retryability": "RETRYABLE",
                "effect_state": "SETTLED",
                "evidence_refs": ["evidence-12345678"],
                "authority": "CONTROLLER_POLICY",
            }
        value.update(overrides)
        value["observation_id"] = observation_identity(value)
        return value

    def gpt_decision(self, assessment_id: str, **overrides):
        value = {
            "schema_version": "decision.v2",
            "decision_id": "decision-12345678",
            "actor_kind": "GPT",
            "boundary": "TECHNICAL_REVIEW",
            "subject_id": assessment_id,
            "subject_digest": assessment_id,
            "subject_version": 1,
            "allowed_choices": ["CONTINUE", "REPLAN:ENGINEERING_FIX", "REPLAN:NEXT_ITERATION", "REPLAN:BASELINE_CHANGE", "STAGE_READY"],
            "requested_action": "APPLY_GPT_DECISION",
            "provenance": {
                "request_count": 1,
                "conversation_id": "conversation-12345678",
                "response_digest": "response-12345678",
                "packet_digest": "packet-12345678",
            },
            "supersedes": None,
        }
        value.update(overrides)
        return value

    def dependency_decision(self, edge):
        return {
            "schema_version": "decision.v2",
            "decision_id": edge["authorization_decision_id"],
            "actor_kind": "HUMAN",
            "boundary": "STAGE_PLANNING",
            "subject_id": edge["child_id"],
            "subject_type": "DEPENDENCY",
            "subject_digest": dependency_authorization_digest(edge),
            "subject_version": 1,
            "allowed_choices": ["ACCEPT_DEPENDENCY", "REJECT_DEPENDENCY"],
            "requested_action": "authorize bounded dependency",
            "provenance": {"source": "human_instruction", "receipt_digest": "receipt-dependency-12345678"},
            "supersedes": None,
        }

    def authorize_dependency(self, controller, edge, prefix):
        decision = self.dependency_decision(edge)
        controller.request_decision(decision, command_id=f"{prefix}-request-12345678")
        controller.apply_decision(
            edge["child_id"],
            payload={
                "decision_id": decision["decision_id"],
                "subject_digest": decision["subject_digest"],
                "subject_version": decision["subject_version"],
                "choice": "ACCEPT_DEPENDENCY",
            },
            command_id=f"{prefix}-accept-12345678",
        )

    def setUp(self):
        self.controller = StageController(workspace_id="workspace-12345678")
        self.controller.register_stage(self.stage())
        self.controller.start("stage-12345678", command_id="command-start-12345678")

    def make_assessment(self, observation=None, *, revalidation=False, correction=None):
        observation = observation or self.observation()
        return assess_observation(
            observation,
            self.manifest(attempt_id=observation["attempt_id"], manifest_id=observation["evidence_manifest_digest"]),
            baseline_digest="baseline-12345678",
            validator_code_digest="validator-new-12345678" if correction else "validator-12345678",
            validation_contract_revision="admission.v2",
            correction_receipt=correction,
            revalidation=revalidation,
        )

    def test_full_canonical_execution_review_integration_closeout(self):
        request = self.controller.request_execution("stage-12345678", command_id="command-request-12345678", request={"work": "bounded"}, provenance={"provider": "fixture-provider", "engine_digest": "engine-12345678"})
        attempt = request["attempt"]
        observation = self.observation(attempt_id=attempt["attempt_id"], iteration_id=attempt["iteration_id"], observation_id="observation-12345679", evidence_manifest_digest="manifest-12345679", provider_result_digest="provider-result-12345679")
        manifest = self.manifest(attempt_id=attempt["attempt_id"], manifest_id="manifest-12345679")
        self.controller.record_observation("stage-12345678", observation, effect_state="SETTLED", command_id="command-observe-12345678")
        assessment = assess_observation(observation, manifest, baseline_digest="baseline-12345678", validator_code_digest="validator-12345678", validation_contract_revision="admission.v2")
        self.controller.assess_result("stage-12345678", assessment, command_id="command-assess-12345678")
        decision = self.gpt_decision(assessment["assessment_id"])
        ready = self.controller.apply_gpt_decision("stage-12345678", decision, choice="STAGE_READY", command_id="command-ready-12345678")
        self.assertEqual(ready["stage"]["status"], "READY")
        integration = self.controller.commit_integration("stage-12345678", command_id="command-integrate-12345678", target_manifest_digest="target-manifest-12345678")
        self.controller.apply_receipt("stage-12345678", command_id="command-receipt-12345678", operation_id=integration["operation"]["operation_id"], effect_state="SETTLED", settlement_proof="integration-receipt-proof-12345678", receipt={"verified": True})
        closeout = self.controller.closeout("stage-12345678", command_id="command-closeout-12345678", verification_digest="verification-12345678")
        self.assertEqual(closeout["stage"]["status"], "CLOSED")
        self.assertIsNone(self.controller.state["executable_owner_stage_id"])
        self.assertEqual(self.controller.resume_projection()["next_action"], "REGISTER_STAGE")
        with self.assertRaises(WorkflowV2ControllerError):
            self.controller.request_execution("stage-12345678", command_id="command-after-close-12345678")

    def test_resume_projection_exposes_resolved_gpt_decision_for_human_output(self):
        request = self.controller.request_execution("stage-12345678", command_id="command-human-output-request-12345678")
        attempt = request["attempt"]
        observation = self.observation(
            attempt_id=attempt["attempt_id"],
            iteration_id=attempt["iteration_id"],
            observation_id="observation-human-output-12345678",
            evidence_manifest_digest="manifest-human-output-12345678",
            provider_result_digest="provider-human-output-12345678",
        )
        self.controller.record_observation(
            "stage-12345678", observation, effect_state="SETTLED", command_id="command-human-output-observe-12345678"
        )
        assessment = self.make_assessment(observation)
        self.controller.assess_result("stage-12345678", assessment, command_id="command-human-output-assess-12345678")
        decision = self.gpt_decision(assessment["assessment_id"], allowed_choices=["BLOCKED"])
        self.controller.apply_gpt_decision(
            "stage-12345678", decision, choice="BLOCKED", command_id="command-human-output-blocked-12345678"
        )

        projection = self.controller.resume_projection()
        self.assertEqual(projection["next_action"], "APPLY_GPT_DECISION")
        self.assertEqual(projection["gpt_decision"], "BLOCKED")
        self.assertEqual(projection["gpt_decision_id"], decision["decision_id"])

    def test_command_idempotency_and_stale_revision_are_fail_closed(self):
        stage = self.stage("stage-idempotent").copy()
        first = self.controller.register_stage(stage, command_id="command-idempotent-12345678")
        repeated = self.controller.register_stage(stage, command_id="command-idempotent-12345678")
        self.assertEqual(first, repeated)
        with self.assertRaises(WorkflowV2ControllerError):
            self.controller.register_stage({**stage, "target_identity": "different-target"}, command_id="command-idempotent-12345678")
        revision = self.controller.revision
        self.controller.register_stage(self.stage("stage-stale-12345678"), command_id="command-stale-12345678")
        with self.assertRaises(WorkflowV2ControllerError):
            self.controller.start("stage-idempotent", command_id="command-stale-start-12345678", expected_revision=revision)
        with self.assertRaises(WorkflowV2ControllerError):
            self.controller.dispatch(
                "REGISTER_STAGE",
                subject_id="different-subject-12345678",
                payload={"stage": stage},
                command_id="command-idempotent-12345678",
            )

    def test_failed_observation_cannot_be_marked_ready_without_revalidation(self):
        request = self.controller.request_execution("stage-12345678", command_id="command-failed-ready-request-12345678")
        attempt = request["attempt"]
        observation = self.observation(
            "FAILED",
            attempt_id=attempt["attempt_id"],
            iteration_id=attempt["iteration_id"],
            evidence_manifest_digest="manifest-failed-ready-12345678",
            provider_result_digest="provider-failed-ready-12345678",
        )
        self.controller.record_observation("stage-12345678", observation, effect_state="SETTLED", command_id="command-failed-ready-observe-12345678")
        assessment = self.make_assessment(observation)
        self.assertEqual(assessment["verdict"], "REJECTED")
        self.controller.assess_result("stage-12345678", assessment, command_id="command-failed-ready-assess-12345678")
        with self.assertRaises(WorkflowV2ControllerError):
            self.controller.apply_gpt_decision(
                "stage-12345678",
                self.gpt_decision(assessment["assessment_id"]),
                choice="STAGE_READY",
                command_id="command-failed-ready-decision-12345678",
            )

    def test_apply_receipt_requires_proof_and_binds_receipt_bytes(self):
        request = self.controller.request_execution("stage-12345678", command_id="command-receipt-proof-request-12345678")
        operation_id = request["operation"]["operation_id"]
        with self.assertRaises(WorkflowV2ControllerError):
            self.controller.apply_receipt(
                "stage-12345678",
                operation_id=operation_id,
                effect_state="SETTLED",
                receipt={"verified": True},
                command_id="command-receipt-proof-missing-12345678",
            )
        receipt = {"verified": True}
        with self.assertRaises(WorkflowV2ControllerError):
            self.controller.apply_receipt(
                "stage-12345678",
                operation_id=operation_id,
                effect_state="SETTLED",
                settlement_proof="settlement-proof-12345678",
                receipt=receipt,
                receipt_digest="wrong-receipt-digest-12345678",
                command_id="command-receipt-proof-forged-12345678",
            )
        result = self.controller.apply_receipt(
            "stage-12345678",
            operation_id=operation_id,
            effect_state="SETTLED",
            settlement_proof="settlement-proof-12345678",
            receipt=receipt,
            receipt_digest=sha256_json(receipt),
            command_id="command-receipt-proof-valid-12345678",
        )
        self.assertEqual(result["operation"]["effect_state"], "SETTLED")

    def test_failed_observation_revalidation_creates_assessment_not_provider_success(self):
        request = self.controller.request_execution("stage-12345678", command_id="command-failed-request-12345678", provenance={"provider": "fixture-provider", "engine_digest": "engine-12345678"})
        attempt = request["attempt"]
        observation = self.observation("FAILED", attempt_id=attempt["attempt_id"], iteration_id=attempt["iteration_id"], observation_id="observation-failed-12345678", evidence_manifest_digest="manifest-failed-12345678", provider_result_digest="provider-failed-12345678")
        manifest = self.manifest(attempt_id=attempt["attempt_id"], manifest_id="manifest-failed-12345678")
        self.controller.record_observation("stage-12345678", observation, effect_state="SETTLED", command_id="command-failed-observe-12345678")
        receipt = {
            "schema_version": "correction_receipt.v2",
            "correction_id": "correction-12345678",
            "old_validator_code_digest": "validator-old-12345678",
            "new_validator_code_digest": "validator-new-12345678",
            "bug_evidence_digest": "bug-evidence-12345678",
            "unchanged_contract_digest": "contract-12345678",
            "review_identity": "review-12345678",
            "human_decision_id": "decision-correction-12345678",
            "subject_type": "VALIDATOR_CORRECTION",
            "scope_expansion": False,
            "approved": True,
        }
        correction_decision = {
            "schema_version": "decision.v2",
            "decision_id": receipt["human_decision_id"],
            "actor_kind": "HUMAN",
            "boundary": "TECHNICAL_REVIEW",
            "subject_id": "VALIDATOR_CORRECTION",
            "subject_type": "VALIDATOR_CORRECTION",
            "subject_digest": "correction-subject-12345678",
            "subject_version": 1,
            "allowed_choices": ["ACCEPT_CORRECTION", "REJECT_CORRECTION"],
            "requested_action": "accept validator correction",
            "provenance": {"source": "human_instruction", "receipt_digest": "human-receipt-12345678"},
            "supersedes": None,
        }
        self.controller.request_decision(correction_decision, command_id="command-correction-request-12345678")
        self.controller.apply_decision(
            "VALIDATOR_CORRECTION",
            payload={
                "decision_id": correction_decision["decision_id"],
                "subject_digest": correction_decision["subject_digest"],
                "subject_version": correction_decision["subject_version"],
                "choice": "ACCEPT_CORRECTION",
            },
            command_id="command-correction-accept-12345678",
        )
        before = copy.deepcopy(self.controller.state["observations"])
        assessment = assess_observation(observation, manifest, baseline_digest="baseline-12345678", validator_code_digest="validator-new-12345678", validation_contract_revision="admission.v2", correction_receipt=receipt, revalidation=True)
        result = self.controller.assess_result("stage-12345678", assessment, correction_receipt=receipt, command_id="command-revalidate-12345678")
        self.assertEqual(result["assessment"]["verdict"], "ADMISSIBLE")
        self.assertEqual(self.controller.state["observations"], before)
        self.assertEqual(self.controller.state["observations"][observation["observation_id"]]["provider_terminal_status"], "FAILED")
        self.assertEqual(self.controller.state["stages"]["stage-12345678"]["current_assessment_id"], assessment["assessment_id"])

    def test_engineering_fix_retry_stays_in_same_iteration_and_needs_typed_decision(self):
        request = self.controller.request_execution("stage-12345678", command_id="command-retry-first-12345678", provenance={"provider": "fixture-provider", "engine_digest": "engine-12345678"})
        attempt = request["attempt"]
        observation = self.observation("FAILED", attempt_id=attempt["attempt_id"], iteration_id=attempt["iteration_id"], observation_id="observation-retry-12345678", evidence_manifest_digest="manifest-retry-12345678", provider_result_digest="provider-retry-12345678")
        self.controller.record_observation("stage-12345678", observation, effect_state="SETTLED", command_id="command-retry-observe-12345678")
        assessment = self.make_assessment(observation)
        self.controller.assess_result("stage-12345678", assessment, command_id="command-retry-assess-12345678")
        with self.assertRaises(WorkflowV2ControllerError):
            self.controller.request_execution("stage-12345678", command_id="command-retry-without-decision-12345678", reason="ENGINEERING_FIX", provenance={"provider": "fixture-provider", "engine_digest": "engine-12345678"})
        decision = self.gpt_decision(assessment["assessment_id"])
        self.controller.apply_gpt_decision("stage-12345678", decision, choice="REPLAN", replan_subtype="ENGINEERING_FIX", command_id="command-engineering-fix-12345678")
        retry = self.controller.request_execution("stage-12345678", command_id="command-retry-second-12345678", reason="ENGINEERING_FIX", provenance={"provider": "fixture-provider", "engine_digest": "engine-12345678"})
        self.assertEqual(retry["attempt"]["iteration_id"], attempt["iteration_id"])
        self.assertEqual(self.controller.show_stage("stage-12345678")["attempt_count"], 2)

    def test_gpt_next_iteration_requires_semantic_change_and_increments_once(self):
        request = self.controller.request_execution("stage-12345678", command_id="command-next-first-12345678", provenance={"provider": "fixture-provider", "engine_digest": "engine-12345678"})
        attempt = request["attempt"]
        observation = self.observation("FAILED", attempt_id=attempt["attempt_id"], iteration_id=attempt["iteration_id"], observation_id="observation-next-12345678", evidence_manifest_digest="manifest-next-12345678", provider_result_digest="provider-next-12345678")
        self.controller.record_observation("stage-12345678", observation, effect_state="SETTLED", command_id="command-next-observe-12345678")
        assessment = self.make_assessment(observation)
        self.controller.assess_result("stage-12345678", assessment, command_id="command-next-assess-12345678")
        decision = self.gpt_decision(assessment["assessment_id"])
        result = self.controller.apply_gpt_decision("stage-12345678", decision, choice="REPLAN", replan_subtype="NEXT_ITERATION", technical_change_digest="change-12345678", solution_fingerprint="solution-v2-12345678", command_id="command-next-iteration-12345678")
        self.assertEqual(result["stage"]["iteration_count"], 2)
        self.assertEqual(result["stage"]["attempt_count"], 1)
        with self.assertRaises(WorkflowV2ControllerError):
            self.controller.advance_iteration("stage-12345678", command_id="command-next-duplicate-12345678", payload={"requires_next_iteration": True, "review_identity": "review-12345678", "technical_change_digest": "change-12345679"})

    def test_unknown_external_effect_blocks_assessment_and_stop(self):
        request = self.controller.request_execution("stage-12345678", command_id="command-unknown-request-12345678", provenance={"provider": "fixture-provider", "engine_digest": "engine-12345678"})
        attempt = request["attempt"]
        observation = self.observation(attempt_id=attempt["attempt_id"], iteration_id=attempt["iteration_id"], observation_id="observation-unknown-12345678", evidence_manifest_digest="manifest-unknown-12345678", provider_result_digest="provider-unknown-12345678")
        self.controller.record_observation("stage-12345678", observation, command_id="command-unknown-observe-12345678")
        with self.assertRaises(WorkflowV2ControllerError):
            self.controller.assess_result("stage-12345678", self.make_assessment(observation), command_id="command-unknown-assess-12345678")
        with self.assertRaises(WorkflowV2ControllerError):
            self.controller.stop("stage-12345678", command_id="command-unknown-stop-12345678")

    def test_dependency_child_owns_token_and_parent_remains_planned_until_explicit_start(self):
        controller = StageController(workspace_id="workspace-12345678")
        controller.register_stage(self.stage())
        parent = controller.show_stage("stage-12345678")
        self.assertEqual(parent["status"], "PLANNED")
        child = self.stage("child-12345678")
        edge = {
            "schema_version": "dependency.v2",
            "parent_id": "stage-12345678",
            "child_id": "child-12345678",
            "predicate": "child closed output",
            "input_binding": {"digest": "input-12345678"},
            "output_contract_digest": "output-12345678",
            "authorization_decision_id": "decision-dependency-12345678",
            "child_status": "PLANNED",
        }
        self.authorize_dependency(controller, edge, "command-dep-planned-auth")
        result = controller.add_dependency("stage-12345678", edge, child, command_id="command-dep-planned-12345678")
        self.assertEqual(result["stage"]["status"], "PLANNED")
        self.assertEqual(result["child"]["status"], "PLANNED")
        self.assertEqual(controller.state["executable_owner_stage_id"], "child-12345678")

    def test_dependency_child_closes_normally_and_returns_owner_without_parent_transition(self):
        controller = StageController(workspace_id="workspace-12345678")
        controller.register_stage(self.stage(), command_id="command-parent-register-12345678")
        child = self.stage("child-12345678")
        edge = {
            "schema_version": "dependency.v2",
            "parent_id": "stage-12345678",
            "child_id": "child-12345678",
            "predicate": "child closed output",
            "input_binding": {"digest": "input-12345678"},
            "output_contract_digest": "output-12345678",
            "authorization_decision_id": "decision-dependency-12345678",
            "child_status": "PLANNED",
        }
        self.authorize_dependency(controller, edge, "command-dep-close-auth")
        controller.add_dependency("stage-12345678", edge, child, command_id="command-dep-close-12345678")
        controller.start("child-12345678", command_id="command-child-start-12345678")
        request = controller.request_execution("child-12345678", command_id="command-child-request-12345678", provenance={"provider": "fixture-provider", "engine_digest": "engine-12345678"})
        attempt = request["attempt"]
        observation = self.observation(
            stage_id="child-12345678",
            attempt_id=attempt["attempt_id"],
            iteration_id=attempt["iteration_id"],
            observation_id="observation-child-12345678",
            evidence_manifest_digest="manifest-child-12345678",
            provider_result_digest="provider-child-12345678",
        )
        manifest = self.manifest(stage_id="child-12345678", attempt_id=attempt["attempt_id"], manifest_id="manifest-child-12345678")
        controller.record_observation("child-12345678", observation, effect_state="SETTLED", command_id="command-child-observe-12345678")
        assessment = assess_observation(observation, manifest, baseline_digest="baseline-12345678", validator_code_digest="validator-12345678", validation_contract_revision="admission.v2")
        controller.assess_result("child-12345678", assessment, command_id="command-child-assess-12345678")
        controller.apply_gpt_decision("child-12345678", self.gpt_decision(assessment["assessment_id"]), choice="STAGE_READY", command_id="command-child-ready-12345678")
        closed = controller.closeout("child-12345678", command_id="command-child-closeout-12345678", no_integration_required=True, verification_digest="verification-child-12345678")
        result = controller.satisfy_dependency("stage-12345678", child_id="child-12345678", output_contract_digest="output-12345678", closeout_identity=closed["closeout"]["closeout_id"], command_id="command-dep-satisfy-12345678")
        self.assertEqual(result["parent"]["status"], "PLANNED")
        self.assertEqual(result["dependency"]["status"], "SATISFIED")
        self.assertEqual(controller.state["executable_owner_stage_id"], "stage-12345678")

    def test_failed_commit_does_not_publish_partial_journal_and_tampered_events_are_rejected(self):
        controller = StageController(workspace_id="workspace-12345678")
        original_persist = controller._persist

        def fail_persist(_payload):
            raise OSError("simulated crash before journal replace")

        controller._persist = fail_persist
        with self.assertRaises(OSError):
            controller.register_stage(self.stage(), command_id="command-crash-12345678")
        self.assertEqual(controller.revision, 0)
        self.assertEqual(controller.state["stages"], {})

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            persisted = StageController(workspace_id="workspace-12345678", state_path=path)
            persisted.register_stage(self.stage(), command_id="command-tamper-12345678")
            payload = persisted.state
            payload["events"][0]["result"]["stage"]["status"] = "ACTIVE"
            tampered = Path(directory) / "tampered.json"
            tampered.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(WorkflowV2ControllerError):
                StageController.from_state(tampered)
            payload = persisted.state
            payload["commands"]["command-tamper-12345678"]["receipt"]["stage"]["status"] = "ACTIVE"
            tampered_receipt = Path(directory) / "tampered-receipt.json"
            tampered_receipt.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(WorkflowV2ControllerError):
                StageController.from_state(tampered_receipt)

    def test_persisted_journal_reloads_with_zero_replay_and_closed_zero_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            controller = StageController(workspace_id="workspace-12345678", state_path=path)
            controller.register_stage(self.stage(), command_id="command-persist-register-12345678")
            controller.start("stage-12345678", command_id="command-persist-start-12345678")
            restored = StageController.from_state(path)
            self.assertEqual(restored.revision, controller.revision)
            self.assertEqual(restored.resume_projection()["next_action"], "REQUEST_EXECUTION")
            self.assertEqual(len(restored.state["attempts"]), 0)

    def test_cross_process_cas_reloads_latest_and_projection_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.json"
            first = StageController(workspace_id="workspace-12345678", state_path=path)
            first.initialize()
            second = StageController(workspace_id="workspace-12345678", state_path=path)
            first.register_stage(self.stage(), command_id="command-cross-process-register-12345678")
            with self.assertRaises(WorkflowV2ControllerError):
                second.start("stage-12345678", command_id="command-cross-process-stale-12345678", expected_revision=0)
            started = second.start("stage-12345678", command_id="command-cross-process-start-12345678")
            self.assertEqual(started["stage"]["status"], "ACTIVE")
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["stages"]["stage-12345678"]["target_identity"] = "tampered-target"
            tampered = Path(directory) / "tampered-projection.json"
            tampered.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(WorkflowV2ControllerError):
                StageController.from_state(tampered)


if __name__ == "__main__":
    unittest.main()
