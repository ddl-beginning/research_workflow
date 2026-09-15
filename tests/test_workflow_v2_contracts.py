"""Contract matrix for the Workflow V2 lifecycle architecture."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from src.contracts import sha256_json
from src.workflow_v2_contracts import (
    ASSESSMENT_VERDICTS,
    DECISION_BOUNDARIES,
    DOMAIN_SCHEMA_ROOTS,
    EFFECT_STATES,
    PUBLIC_COMMANDS,
    REPLAN_SUBTYPES,
    SHARED_SUPPORT_SCHEMA_ROOTS,
    STAGE_STATES,
    ContractValidationError,
    V2_SCHEMA_DIR,
    assess_observation,
    assessment_identity,
    derive_objective_fingerprint,
    load_v2_schema,
    observation_identity,
    validate_command_envelope,
    validate_correction_receipt,
    validate_decision,
    validate_decision_subject,
    validate_dependency_graph,
    validate_evidence_manifest,
    validate_operation_envelope,
    validate_provider_observation,
    validate_stage,
    validate_stage_assessment,
    validate_typed_failure,
    validate_typed_replan,
)


class WorkflowV2ContractMatrix(unittest.TestCase):
    def stage(self, **overrides):
        value = {
            "schema_version": "stage.v2",
            "stage_id": "stage-12345678",
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
                "max_attempts_total": 4,
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

    def observation(self, **overrides):
        value = {
            "schema_version": "provider_observation.v2",
            "observation_id": "observation-12345678",
            "stage_id": "stage-12345678",
            "iteration_id": "iteration-12345678",
            "attempt_id": "attempt-12345678",
            "provider_result_digest": "provider-result-12345678",
            "evidence_manifest_digest": "manifest-12345678",
            "provider_terminal_status": "FAILED",
            "raw_provider_claim": {"status": "SUCCEEDED", "retryable": True, "effect_state": "SETTLED"},
            "provenance": {"provider": "fixture-provider", "engine_digest": "engine-12345678"},
            "failure": {
                "schema_version": "typed_failure.v2",
                "failure_class": "PROVIDER_FAILURE",
                "code": "provider_failed",
                "operation_id": "operation-12345678",
                "retryability": "UNKNOWN",
                "effect_state": "UNKNOWN",
                "evidence_refs": ["evidence-12345678"],
                "authority": "CONTROLLER_POLICY",
            },
            "outputs": {"files": []},
            "immutable": True,
        }
        value.update(overrides)
        value["observation_id"] = observation_identity(value)
        return value

    def manifest(self, **overrides):
        value = {
            "schema_version": "evidence_manifest.v2",
            "manifest_id": "manifest-12345678",
            "stage_id": "stage-12345678",
            "attempt_id": "attempt-12345678",
            "required_artifact_paths": ["artifacts/result.json"],
            "changed_paths": ["artifacts/result.json"],
            "allowed_paths": ["artifacts"],
            "protected_paths": [".git", "secrets"],
            "path_inventory": [{"path": "artifacts/result.json", "sha256": "digest-12345678"}],
            "complete": True,
        }
        value.update(overrides)
        return value

    def gpt_decision(self, **overrides):
        value = {
            "schema_version": "decision.v2",
            "decision_id": "decision-12345678",
            "actor_kind": "GPT",
            "boundary": "TECHNICAL_REVIEW",
            "subject_id": "assessment-12345678",
            "subject_digest": "assessment-12345678",
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

    def test_schema_roots_are_exactly_seven_domain_and_three_shared(self):
        self.assertEqual(
            set(path.stem.removesuffix(".schema") for path in V2_SCHEMA_DIR.glob("*.json")),
            set(DOMAIN_SCHEMA_ROOTS) | set(SHARED_SUPPORT_SCHEMA_ROOTS),
        )
        for root in DOMAIN_SCHEMA_ROOTS + SHARED_SUPPORT_SCHEMA_ROOTS:
            self.assertEqual(load_v2_schema(root)["type"], "object")

    def test_state_and_command_hard_limits(self):
        self.assertEqual(STAGE_STATES, ("PLANNED", "ACTIVE", "READY", "CLOSED", "STOPPED"))
        self.assertEqual(len(PUBLIC_COMMANDS), 18)
        self.assertNotIn("BLOCKED", STAGE_STATES)
        self.assertNotIn("SUSPENDED", STAGE_STATES)
        self.assertNotIn("OPEN_ITERATION", PUBLIC_COMMANDS)
        self.assertNotIn("RETRY", PUBLIC_COMMANDS)

    def test_stage_rejects_writable_blocked_and_invalid_repair_depth(self):
        invalid = self.stage(status="BLOCKED")
        with self.assertRaises(ContractValidationError):
            validate_stage(invalid)
        invalid = self.stage(repair_depth=1)
        with self.assertRaises(ContractValidationError):
            validate_stage(invalid)
        invalid = self.stage(purpose="INFRASTRUCTURE_REPAIR", repair_depth=1, predecessor_stage_id=None)
        with self.assertRaises(ContractValidationError):
            validate_stage(invalid)
        valid = self.stage(
            purpose="INFRASTRUCTURE_REPAIR",
            repair_depth=1,
            predecessor_stage_id="parent-12345678",
            semantic_baseline_diff_digest="diff-12345678",
        )
        self.assertEqual(validate_stage(valid)["purpose"], "INFRASTRUCTURE_REPAIR")

    def test_failed_provider_observation_is_immutable_and_raw_claim_has_no_authority(self):
        observation = self.observation()
        checked = validate_provider_observation(observation)
        self.assertEqual(checked["provider_terminal_status"], "FAILED")
        self.assertEqual(checked["failure"]["retryability"], "UNKNOWN")
        self.assertEqual(checked["failure"]["effect_state"], "UNKNOWN")
        self.assertEqual(checked["raw_provider_claim"]["status"], "SUCCEEDED")
        self.assertEqual(checked["raw_provider_claim"]["effect_state"], "SETTLED")
        invalid = copy.deepcopy(observation)
        invalid["failure"]["authority"] = "PROVIDER_CLAIM"
        with self.assertRaises(ContractValidationError):
            validate_provider_observation(invalid)
        invalid = copy.deepcopy(observation)
        invalid["immutable"] = False
        with self.assertRaises(ContractValidationError):
            validate_provider_observation(invalid)

    def test_typed_failure_requires_controller_policy_and_effect_state(self):
        failure = self.observation()["failure"]
        self.assertEqual(validate_typed_failure(failure)["effect_state"], "UNKNOWN")
        for field, value in (("retryability", "RETRYABLE"), ("effect_state", "SETTLED")):
            modified = copy.deepcopy(failure)
            modified[field] = value
            self.assertEqual(validate_typed_failure(modified)[field], value)
        self.assertEqual(set(EFFECT_STATES), set((validate_operation_envelope({
            "schema_version": "operation_envelope.v2",
            "operation_id": "operation-12345678",
            "workspace_id": "workspace-12345678",
            "subject_id": "stage-12345678",
            "intent_digest": "intent-12345678",
            "effect_state": effect,
            "capability_manifest": {"query_by_operation_id": True, "idempotent_submit": False, "fence": False, "prove_not_sent": True},
            "status": "BLOCKED",
        })["effect_state"] for effect in EFFECT_STATES)))

    def test_failed_observation_can_receive_new_assessment_without_provider_success_rewrite(self):
        observation = self.observation()
        before = json.dumps(observation, sort_keys=True)
        receipt = {
            "schema_version": "correction_receipt.v2",
            "correction_id": "correction-12345678",
            "old_validator_code_digest": "validator-old-12345678",
            "new_validator_code_digest": "validator-new-12345678",
            "bug_evidence_digest": "bug-evidence-12345678",
            "unchanged_contract_digest": "contract-12345678",
            "review_identity": "review-12345678",
            "human_decision_id": "decision-12345678",
            "subject_type": "VALIDATOR_CORRECTION",
            "scope_expansion": False,
            "approved": True,
        }
        validate_correction_receipt(receipt)
        assessment = assess_observation(
            observation,
            self.manifest(),
            baseline_digest="baseline-12345678",
            validator_code_digest="validator-new-12345678",
            validation_contract_revision="admission.v2",
            correction_receipt=receipt,
            revalidation=True,
        )
        self.assertEqual(assessment["verdict"], "ADMISSIBLE")
        self.assertTrue(assessment["revalidation"])
        self.assertEqual(observation["provider_terminal_status"], "FAILED")
        self.assertEqual(json.dumps(observation, sort_keys=True), before)
        same_id = assessment_identity(
            stage_id="stage-12345678",
            baseline_digest="baseline-12345678",
            iteration_id="iteration-12345678",
            attempt_id="attempt-12345678",
            provider_result_digest="provider-result-12345678",
            evidence_manifest_digest="manifest-12345678",
            validator_code_digest="validator-new-12345678",
            validation_contract_revision="admission.v2",
            correction_receipt_digest=sha256_json(receipt),
        )
        self.assertEqual(assessment["assessment_id"], same_id)
        changed_id = assessment_identity(
            stage_id="stage-12345678",
            baseline_digest="baseline-12345678",
            iteration_id="iteration-12345678",
            attempt_id="attempt-12345678",
            provider_result_digest="provider-result-12345678",
            evidence_manifest_digest="manifest-12345678",
            validator_code_digest="validator-other-12345678",
            validation_contract_revision="admission.v2",
            correction_receipt_digest=sha256_json(receipt),
        )
        self.assertNotEqual(same_id, changed_id)
        self.assertEqual(validate_stage_assessment(assessment)["assessment_id"], same_id)

    def test_missing_evidence_is_insufficient_and_not_a_provider_retry(self):
        incomplete = self.manifest(complete=False, changed_paths=[])
        assessment = assess_observation(
            self.observation(),
            incomplete,
            baseline_digest="baseline-12345678",
            validator_code_digest="validator-12345678",
            validation_contract_revision="admission.v2",
        )
        self.assertEqual(assessment["verdict"], "INSUFFICIENT")
        self.assertEqual(assessment["attempt_id"], "attempt-12345678")
        self.assertFalse(assessment["revalidation"])
        with self.assertRaises(ContractValidationError):
            validate_evidence_manifest(self.manifest(path_inventory=[]))

    def test_objective_fingerprint_is_independent_of_stage_id_and_wording(self):
        first = derive_objective_fingerprint(
            project_goal="compare storage",
            acceptance_criteria=["recover writes", "measure reads"],
            target_identity="notebook",
            required_capability="python",
        )
        second = derive_objective_fingerprint(
            project_goal="compare storage",
            acceptance_criteria=["measure reads", "recover writes"],
            target_identity="notebook",
            required_capability="python",
        )
        self.assertEqual(first, second)
        self.assertNotEqual(first, derive_objective_fingerprint(
            project_goal="compare storage",
            acceptance_criteria=["recover writes", "measure reads"],
            target_identity="another-notebook",
            required_capability="python",
        ))

    def test_scope_expansion_cannot_be_validator_correction(self):
        receipt = {
            "schema_version": "correction_receipt.v2",
            "correction_id": "correction-12345678",
            "old_validator_code_digest": "validator-old-12345678",
            "new_validator_code_digest": "validator-new-12345678",
            "bug_evidence_digest": "bug-evidence-12345678",
            "unchanged_contract_digest": "contract-12345678",
            "review_identity": "review-12345678",
            "human_decision_id": "decision-12345678",
            "subject_type": "VALIDATOR_CORRECTION",
            "scope_expansion": True,
            "approved": True,
        }
        with self.assertRaises(ContractValidationError):
            validate_correction_receipt(receipt)

    def test_decision_subject_and_typed_replan(self):
        decision = self.gpt_decision()
        validate_decision(decision)
        self.assertEqual(validate_decision_subject(decision, subject_id="assessment-12345678", subject_digest="assessment-12345678", subject_version=1)["decision_id"], decision["decision_id"])
        with self.assertRaises(ContractValidationError):
            validate_decision_subject(decision, subject_id="assessment-other", subject_digest="assessment-12345678", subject_version=1)
        typed = validate_typed_replan(decision, "NEXT_ITERATION")
        self.assertIn(typed["replan_subtype"], REPLAN_SUBTYPES)
        with self.assertRaises(ContractValidationError):
            validate_typed_replan({**decision, "actor_kind": "HUMAN"}, "NEXT_ITERATION")
        with self.assertRaises(ContractValidationError):
            validate_decision({**decision, "provenance": {"request_count": 0}})

    def test_dependency_graph_is_bounded_acyclic_and_single_owner(self):
        edge = {
            "schema_version": "dependency.v2",
            "parent_id": "parent-12345678",
            "child_id": "child-12345678",
            "predicate": "child closeout satisfies input contract",
            "input_binding": {"digest": "input-12345678"},
            "output_contract_digest": "output-12345678",
            "authorization_decision_id": "decision-12345678",
            "child_status": "PLANNED",
        }
        self.assertEqual(len(validate_dependency_graph([edge], max_nodes=2, max_depth=1)), 1)
        with self.assertRaises(ContractValidationError):
            validate_dependency_graph([edge, {**edge, "parent_id": "other-parent-12345678"}], max_nodes=4, max_depth=2)
        cycle = {**edge, "parent_id": "child-12345678", "child_id": "parent-12345678"}
        with self.assertRaises(ContractValidationError):
            validate_dependency_graph([edge, cycle], max_nodes=3, max_depth=3)

    def test_command_and_effect_envelopes_are_typed(self):
        command = {
            "schema_version": "command_envelope.v2",
            "workspace_id": "workspace-12345678",
            "command_id": "command-12345678",
            "expected_revision": 0,
            "command_type": "ASSESS_RESULT",
            "subject_id": "stage-12345678",
            "payload_digest": "payload-12345678",
        }
        self.assertEqual(validate_command_envelope(command)["command_type"], "ASSESS_RESULT")
        with self.assertRaises(ContractValidationError):
            validate_command_envelope({**command, "command_type": "RETRY"})
        with self.assertRaises(ContractValidationError):
            validate_operation_envelope({
                "schema_version": "operation_envelope.v2",
                "operation_id": "operation-12345678",
                "workspace_id": "workspace-12345678",
                "subject_id": "stage-12345678",
                "intent_digest": "intent-12345678",
                "effect_state": "UNKNOWN",
                "capability_manifest": {"query_by_operation_id": False, "idempotent_submit": False, "fence": False, "prove_not_sent": False},
                "status": "SETTLED",
            })

    def test_evidence_manifest_preserves_required_vs_changed_scope(self):
        self.assertEqual(validate_evidence_manifest(self.manifest())["complete"], True)
        with self.assertRaises(ContractValidationError):
            validate_evidence_manifest(self.manifest(changed_paths=["outside.txt"]))
        with self.assertRaises(ContractValidationError):
            validate_evidence_manifest(self.manifest(changed_paths=["secrets/key.txt"]))


if __name__ == "__main__":
    unittest.main()
