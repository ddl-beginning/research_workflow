"""Bounded legacy-orphan recovery and provider-handoff invariants."""

from __future__ import annotations

import copy

import pytest

from src.contracts import ContractValidationError
from src.workflow_v2_controller import StageController, build_provider_handoff_manifest
from src.workflow_v2_contracts import PROVIDER_HANDOFF_INVARIANT_VERSION, validate_operation_envelope


STAGE_ID = "stage-orphan-12345678"
WORKSPACE_ID = "workspace-orphan-12345678"
ITERATION_ID = "iteration-orphan-12345678"
OLD_ATTEMPT_ID = "attempt-orphan-12345678"
OLD_OPERATION_ID = "operation-orphan-12345678"


def _stage() -> dict:
    return {
        "schema_version": "stage.v2",
        "stage_id": STAGE_ID,
        "workspace_id": WORKSPACE_ID,
        "project_id": "project-orphan-12345678",
        "objective_fingerprint": "objective-orphan-12345678",
        "target_identity": "target/orphan",
        "required_capabilities": ["python"],
        "purpose": "DOMAIN",
        "repair_depth": 0,
        "baseline_digest": "baseline-orphan-12345678",
        "status": "PLANNED",
        "budgets": {
            "max_iterations": 1,
            "max_attempts_per_iteration": 1,
            "max_attempts_total": 1,
            "max_revalidation_ops": 1,
            "max_validator_revisions": 1,
            "max_dependency_nodes": 1,
            "max_dependency_depth": 1,
            "max_descendant_attempts": 1,
        },
        "owner_stage_id": None,
        "current_iteration_id": None,
        "current_assessment_id": None,
        "predecessor_stage_id": None,
        "semantic_baseline_diff_digest": None,
    }


def _orphan_controller() -> StageController:
    controller = StageController(workspace_id=WORKSPACE_ID)
    controller.register_stage(_stage(), command_id="command-orphan-register-12345678")
    controller.start(STAGE_ID, command_id="command-orphan-start-12345678")
    state = controller._journal  # Build a historical pre-invariant fixture without rewriting its events.
    state["attempts"][OLD_ATTEMPT_ID] = {
        "schema_version": "execution_attempt.v2",
        "attempt_id": OLD_ATTEMPT_ID,
        "stage_id": STAGE_ID,
        "iteration_id": ITERATION_ID,
        "request_id": "request-orphan-12345678",
        "request_digest": "request-digest-orphan-12345678",
        "provenance": {"provider": "workflow-v2-controller", "engine_digest": "engine-pre-invariant-12345678"},
        "purpose": "DOMAIN",
        "effect_state": "INTENT_COMMITTED",
        "attempt_index": 1,
        "committed": True,
        "status": "REQUESTED",
    }
    state["iterations"][ITERATION_ID] = {
        "schema_version": "semantic_iteration.v2",
        "iteration_id": ITERATION_ID,
        "stage_id": STAGE_ID,
        "index": 1,
        "solution_fingerprint": "objective-orphan-12345678",
        "opened_by": "START",
        "requires_next_iteration": False,
        "review_identity": None,
        "technical_change_digest": None,
    }
    state["stages"][STAGE_ID]["current_iteration_id"] = ITERATION_ID
    state["operations"][OLD_OPERATION_ID] = {
        "schema_version": "operation_envelope.v2",
        "operation_id": OLD_OPERATION_ID,
        "workspace_id": WORKSPACE_ID,
        "subject_id": OLD_ATTEMPT_ID,
        "intent_digest": "request-digest-orphan-12345678",
        "effect_state": "INTENT_COMMITTED",
        "capability_manifest": {
            "query_by_operation_id": False,
            "idempotent_submit": False,
            "fence": False,
            "prove_not_sent": False,
        },
        "status": "INTENT",
    }
    state["stage_runtime"][STAGE_ID]["current_attempt_id"] = OLD_ATTEMPT_ID
    state["stage_runtime"][STAGE_ID]["in_flight_operation_id"] = OLD_OPERATION_ID
    return controller


def _audit(operation_id: str) -> dict:
    return {
        "schema_version": "side_effect_audit.v1",
        "operation_id": operation_id,
        "classification": "NONE",
        "complete": True,
        "checked_scopes": ["project", "benchmark", "provider", "bridge", "codex"],
    }


def test_legacy_orphan_operation_can_be_abandoned_without_fabricating_receipt():
    controller = _orphan_controller()
    before = copy.deepcopy(controller.state["operations"][OLD_OPERATION_ID])
    result = controller.resolve_legacy_orphan(
        STAGE_ID,
        operation_id=OLD_OPERATION_ID,
        effect_class="REVERSIBLE_LOCAL_RESEARCH",
        side_effect_audit=_audit(OLD_OPERATION_ID),
        command_id="command-orphan-resolve-12345678",
    )
    assert result["legacy_resolution"]["classification"] == "ORPHANED_UNRECOVERABLE_PROVIDER_HANDOFF"
    assert result["legacy_resolution"]["side_effect_evidence"] == "NONE"
    assert result["old_operation"] == before
    assert "receipt" not in controller.state["operations"][OLD_OPERATION_ID]
    assert controller.resume_projection()["next_action"] == "REQUEST_EXECUTION"


def test_legacy_orphan_resolution_preserves_old_history():
    controller = _orphan_controller()
    old_attempt = copy.deepcopy(controller.state["attempts"][OLD_ATTEMPT_ID])
    old_operation = copy.deepcopy(controller.state["operations"][OLD_OPERATION_ID])
    old_revision = controller.revision
    controller.resolve_legacy_orphan(
        STAGE_ID,
        operation_id=OLD_OPERATION_ID,
        effect_class="REVERSIBLE_LOCAL_RESEARCH",
        side_effect_audit=_audit(OLD_OPERATION_ID),
        command_id="command-orphan-history-12345678",
    )
    assert controller.revision == old_revision + 1
    assert controller.state["attempts"][OLD_ATTEMPT_ID] == old_attempt
    assert controller.state["operations"][OLD_OPERATION_ID] == old_operation
    assert controller.state["legacy_resolutions"]


def test_legacy_orphan_creates_new_attempt_not_redispatch():
    controller = _orphan_controller()
    old_operation = copy.deepcopy(controller.state["operations"][OLD_OPERATION_ID])
    controller.resolve_legacy_orphan(
        STAGE_ID,
        operation_id=OLD_OPERATION_ID,
        effect_class="REVERSIBLE_LOCAL_RESEARCH",
        side_effect_audit=_audit(OLD_OPERATION_ID),
        command_id="command-orphan-new-attempt-12345678",
    )
    result = controller.request_execution(
        STAGE_ID,
        reason="LEGACY_ORPHAN_RECOVERY",
        command_id="command-orphan-request-12345678",
        request={"objective": "bounded local research"},
        provenance={"provider": "fixture-provider", "engine_digest": "engine-new-12345678"},
    )
    assert result["attempt"]["attempt_id"] != OLD_ATTEMPT_ID
    assert result["operation"]["operation_id"] != OLD_OPERATION_ID
    assert result["operation"]["provider_handoff_manifest"]["workflow_operation_id"] == result["operation"]["operation_id"]
    assert controller.state["operations"][OLD_OPERATION_ID] == old_operation


def test_intent_commit_requires_provider_handoff_manifest():
    controller = StageController(workspace_id=WORKSPACE_ID)
    controller.register_stage(_stage(), command_id="command-manifest-register-12345678")
    controller.start(STAGE_ID, command_id="command-manifest-start-12345678")
    with pytest.raises(Exception, match="INTENT_COMMIT_REQUIRES_RECOVERABLE_PROVIDER_HANDOFF"):
        controller.dispatch(
            "REQUEST_EXECUTION",
            subject_id=STAGE_ID,
            payload={"request": {"objective": "missing handoff"}},
            command_id="command-manifest-missing-12345678",
        )
    assert controller.revision == 2
    assert controller.state["attempts"] == {}


def test_new_operation_has_reconciliation_identity_and_no_secrets():
    request = {"objective": "bounded", "allowed_paths": ["src"]}
    manifest = build_provider_handoff_manifest(
        operation_id="operation-manifest-12345678",
        stage_id=STAGE_ID,
        iteration_id=ITERATION_ID,
        attempt_id="attempt-manifest-12345678",
        request_id="request-manifest-12345678",
        request=request,
        provenance={"provider": "fixture-provider", "engine_digest": "engine-12345678"},
        purpose="DOMAIN",
    )
    assert manifest["reconciliation_identity"]
    assert manifest["idempotency_key"]
    assert manifest["reconstructible_request_descriptor"]["request"] == request
    with pytest.raises(ContractValidationError):
        build_provider_handoff_manifest(
            operation_id="operation-secret-12345678",
            stage_id=STAGE_ID,
            iteration_id=ITERATION_ID,
            attempt_id="attempt-secret-12345678",
            request_id="request-secret-12345678",
            request={"token": "must-not-persist"},
            provenance={"provider": "fixture-provider", "engine_digest": "engine-12345678"},
            purpose="DOMAIN",
        )


def test_post_invariant_missing_handoff_is_engine_bug_not_legacy_recovery():
    controller = _orphan_controller()
    controller._journal["operations"][OLD_OPERATION_ID]["handoff_invariant_version"] = PROVIDER_HANDOFF_INVARIANT_VERSION
    with pytest.raises(Exception, match="CRITICAL_PROVIDER_HANDOFF_INVARIANT_VIOLATION"):
        controller.resolve_legacy_orphan(
            STAGE_ID,
            operation_id=OLD_OPERATION_ID,
            effect_class="REVERSIBLE_LOCAL_RESEARCH",
            side_effect_audit=_audit(OLD_OPERATION_ID),
            command_id="command-orphan-post-invariant-12345678",
        )
    with pytest.raises(ContractValidationError):
        validate_operation_envelope(controller.state["operations"][OLD_OPERATION_ID])


def test_same_stage_identity_survives_legacy_recovery_and_human_intervention_stays_zero():
    controller = _orphan_controller()
    before = controller.show_stage(STAGE_ID)
    controller.resolve_legacy_orphan(
        STAGE_ID,
        operation_id=OLD_OPERATION_ID,
        effect_class="REVERSIBLE_LOCAL_RESEARCH",
        side_effect_audit=_audit(OLD_OPERATION_ID),
        command_id="command-orphan-identity-12345678",
    )
    after = controller.show_stage(STAGE_ID)
    resolution = next(iter(controller.state["legacy_resolutions"].values()))
    assert before["stage_id"] == after["stage_id"] == STAGE_ID
    assert before["project_id"] == after["project_id"]
    assert before["target_identity"] == after["target_identity"]
    assert resolution["human_intervention_count"] == 0


def test_resume_routes_legacy_orphan_to_recovery_before_observation():
    controller = _orphan_controller()
    projection = controller.resume_projection()
    assert projection["next_action"] == "RESOLVE_LEGACY_ORPHAN"
    assert projection["next_actor"] == "Controller"


def test_ambiguous_or_external_side_effect_audit_refuses_legacy_abandonment():
    controller = _orphan_controller()
    audit = _audit(OLD_OPERATION_ID)
    audit["classification"] = "AMBIGUOUS"
    with pytest.raises(Exception, match="complete NONE side-effect audit"):
        controller.resolve_legacy_orphan(
            STAGE_ID,
            operation_id=OLD_OPERATION_ID,
            effect_class="REVERSIBLE_LOCAL_RESEARCH",
            side_effect_audit=audit,
            command_id="command-orphan-ambiguous-12345678",
        )
    assert controller.revision == 2
    assert controller.state["legacy_resolutions"] == {}


def test_new_attempt_is_post_invariant_and_durable_in_one_controller_commit():
    controller = _orphan_controller()
    controller.resolve_legacy_orphan(
        STAGE_ID,
        operation_id=OLD_OPERATION_ID,
        effect_class="REVERSIBLE_LOCAL_RESEARCH",
        side_effect_audit=_audit(OLD_OPERATION_ID),
        command_id="command-orphan-atomic-12345678",
    )
    before = controller.revision
    result = controller.request_execution(
        STAGE_ID,
        reason="LEGACY_ORPHAN_RECOVERY",
        command_id="command-orphan-atomic-request-12345678",
        request={"objective": "atomic handoff"},
    )
    assert controller.revision == before + 1
    operation = result["operation"]
    assert operation["handoff_invariant_version"] == PROVIDER_HANDOFF_INVARIANT_VERSION
    assert operation["provider_handoff_manifest"]["dispatch_state"] == "PREPARED"
    assert controller.state["events"][-1]["result"]["operation"]["provider_handoff_manifest"] == operation["provider_handoff_manifest"]
