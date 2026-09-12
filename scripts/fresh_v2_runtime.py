"""Create and exercise one fresh, candidate-only Workflow V2 runtime.

The harness uses deterministic local provider/review adapters so validation is
reproducible and cannot mutate the frozen runtime or make an external call.
The lifecycle transitions themselves are the production V2 controller path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.workflow_v2_controller import StageController
from src.workflow_v2_contracts import assess_observation, observation_identity


RUN_ROOT = ROOT / "implementation_evidence" / "phase-6" / "fresh_v2_runtime"
JOURNAL_PATH = RUN_ROOT / "journal.json"
SELF_HOST_EVIDENCE = ROOT / "implementation_evidence" / "phase-7" / "self-host-validation.json"
WORKSPACE_ID = "workspace-fresh-v2-20260912"
STAGE_ID = "stage-self-host-v2-20260912"


def _stage() -> dict[str, Any]:
    return {
        "schema_version": "stage.v2",
        "stage_id": STAGE_ID,
        "workspace_id": WORKSPACE_ID,
        "project_id": "project-fresh-v2",
        "objective_fingerprint": "objective-self-host-v2-20260912",
        "target_identity": "candidate/implementation_evidence",
        "required_capabilities": ["python", "journal"],
        "purpose": "DOMAIN",
        "repair_depth": 0,
        "baseline_digest": "baseline-self-host-v2-20260912",
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


def _manifest(attempt_id: str) -> dict[str, Any]:
    return {
        "schema_version": "evidence_manifest.v2",
        "manifest_id": "manifest-self-host-v2-20260912",
        "stage_id": STAGE_ID,
        "attempt_id": attempt_id,
        "required_artifact_paths": ["artifacts/self-host-result.json"],
        "changed_paths": ["artifacts/self-host-result.json"],
        "allowed_paths": ["artifacts"],
        "protected_paths": [".git", "secrets"],
        "path_inventory": [{"path": "artifacts/self-host-result.json", "sha256": "artifact-self-host-12345678"}],
        "complete": True,
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    if JOURNAL_PATH.exists() or SELF_HOST_EVIDENCE.exists():
        raise RuntimeError("fresh V2 evidence already exists; refusing to overwrite it")

    controller = StageController(workspace_id=WORKSPACE_ID, state_path=JOURNAL_PATH)
    initial = controller.initialize()
    _write(
        RUN_ROOT / "runtime-manifest.json",
        {
            "evidence_kind": "fresh_v2_runtime_manifest",
            "workspace_id": WORKSPACE_ID,
            "journal_schema": controller.JOURNAL_SCHEMA,
            "initial_revision": initial["revision"],
            "initial_stage_count": len(initial["stages"]),
            "requirement_acceptance": {"mode": "bounded_validation_fixture", "digest": "requirement-fresh-v2-12345678"},
            "design_acceptance": {"mode": "bounded_validation_fixture", "digest": "design-fresh-v2-12345678"},
            "planning_acceptance": {"mode": "bounded_validation_fixture", "digest": "planning-fresh-v2-12345678"},
            "engine_manifest_digest": "engine-manifest-fresh-v2-12345678",
            "validator_manifest_digest": "validator-manifest-fresh-v2-12345678",
            "source": "candidate-only fresh runtime; no frozen .research state copied",
        },
    )

    receipts: list[dict[str, Any]] = []

    def remember(receipt: dict[str, Any]) -> dict[str, Any]:
        receipts.append({"command": receipt["command"]["command_type"], "revision": receipt["revision"], "event_id": receipt["event"]["event_id"]})
        return receipt

    remember(controller.register_stage(_stage(), command_id="command-fresh-register-12345678"))
    remember(controller.start(STAGE_ID, command_id="command-fresh-start-12345678"))
    requested = remember(
        controller.request_execution(
            STAGE_ID,
            command_id="command-fresh-request-12345678",
            request={"operation": "bounded self-host validation"},
            provenance={"provider": "fixture-provider", "engine_digest": "engine-fresh-v2-12345678"},
        )
    )
    attempt = requested["attempt"]
    observation = {
        "schema_version": "provider_observation.v2",
        "observation_id": "observation-fresh-v2-12345678",
        "stage_id": STAGE_ID,
        "iteration_id": attempt["iteration_id"],
        "attempt_id": attempt["attempt_id"],
        "provider_result_digest": "provider-result-fresh-v2-12345678",
        "evidence_manifest_digest": "manifest-self-host-v2-20260912",
        "provider_terminal_status": "SUCCEEDED",
        "raw_provider_claim": {"status": "SUCCEEDED", "source": "fixture-provider"},
        "provenance": {"provider": "fixture-provider", "engine_digest": "engine-fresh-v2-12345678"},
        "failure": None,
        "outputs": {"files": ["artifacts/self-host-result.json"]},
        "immutable": True,
    }
    observation["observation_id"] = observation_identity(observation)
    remember(controller.record_observation(STAGE_ID, observation, effect_state="SETTLED", command_id="command-fresh-observe-12345678"))
    assessment = assess_observation(
        observation,
        _manifest(attempt["attempt_id"]),
        baseline_digest="baseline-self-host-v2-20260912",
        validator_code_digest="validator-fresh-v2-12345678",
        validation_contract_revision="admission.v2",
    )
    remember(controller.assess_result(STAGE_ID, assessment, command_id="command-fresh-assess-12345678"))
    decision = {
        "schema_version": "decision.v2",
        "decision_id": "decision-fresh-gpt-v2-12345678",
        "actor_kind": "GPT",
        "boundary": "TECHNICAL_REVIEW",
        "subject_id": assessment["assessment_id"],
        "subject_digest": assessment["assessment_id"],
        "subject_version": 1,
        "allowed_choices": ["STAGE_READY"],
        "requested_action": "apply bounded technical review",
        "provenance": {
            "request_count": 1,
            "conversation_id": "conversation-fresh-v2-12345678",
            "response_digest": "response-fresh-v2-12345678",
            "packet_digest": "packet-fresh-v2-12345678",
        },
        "supersedes": None,
    }
    remember(controller.apply_gpt_decision(STAGE_ID, decision, choice="STAGE_READY", command_id="command-fresh-ready-12345678"))
    integration = remember(controller.commit_integration(STAGE_ID, command_id="command-fresh-integrate-12345678", target_manifest_digest="target-fresh-v2-12345678"))
    remember(controller.apply_receipt(STAGE_ID, operation_id=integration["operation"]["operation_id"], effect_state="SETTLED", settlement_proof="integration-receipt-proof-fresh-12345678", receipt={"verified": True}, command_id="command-fresh-receipt-12345678"))
    closed = remember(controller.closeout(STAGE_ID, verification_digest="verification-fresh-v2-12345678", command_id="command-fresh-closeout-12345678"))
    reloaded = StageController.from_state(JOURNAL_PATH)
    if reloaded.state != controller.state:
        raise RuntimeError("fresh V2 journal changed across authoritative reload")

    _write(
        SELF_HOST_EVIDENCE,
        {
            "evidence_kind": "candidate_self_host_validation",
            "mode": "local_bounded_controller_harness",
            "external_provider_or_gpt_called": False,
            "routing_contract": {"class": "STANDARD", "model": "gpt-5.6-luna", "reasoning_effort": "max"},
            "workspace_id": WORKSPACE_ID,
            "stage_id": STAGE_ID,
            "journal_path": str(JOURNAL_PATH),
            "initial_revision": initial["revision"],
            "final_revision": controller.revision,
            "reloaded_revision": reloaded.revision,
            "reload_projection_verified": True,
            "final_stage": closed["stage"],
            "command_receipts": receipts,
            "observation_id": observation["observation_id"],
            "assessment_id": assessment["assessment_id"],
            "decision_id": decision["decision_id"],
            "integration_operation_id": integration["operation"]["operation_id"],
            "provider_status_is_not_stage_decision": True,
            "frozen_runtime_copied": False,
        },
    )


if __name__ == "__main__":
    main()
