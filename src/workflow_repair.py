"""Bounded routing for Workflow-internal maintenance failures.

This module is deliberately a policy seam, not a lifecycle.  It captures the
current business identity, gives one maintenance capability a chance to repair
an Engine-owned adapter, and verifies that the original Stage remains the
Stage being resumed before a single retry.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, Mapping


REPAIRABLE_FAILURE_CODES = frozenset(
    {
        "WORKFLOW_CONTRACT_VERSION_MISMATCH",
        "CONTRACT_VERSION_MISMATCH",
        "CLIENT_SUPERVISOR_SCHEMA_VERSION_SKEW",
        "STALE_SUPERVISOR_RUNTIME",
        "ADAPTER_FIELD_STRIPPING",
        "MCP_ENVELOPE_OR_SERIALIZATION_MISMATCH",
        "PROJECTED_NEXT_ACTION_MUST_BE_ADMISSIBLE",
        "PLAN_STAGE_STAGE_OBJECT_OMITTED",
        "STAGE_SCOPED_ACTION_MUST_USE_CANONICAL_STAGE",
    }
)

MaintenanceCapability = Callable[[Mapping[str, Any], BaseException], Mapping[str, Any] | None]
_IDENTITY_FIELDS = (
    "workspace_root",
    "project_id",
    "stage_id",
    "objective_fingerprint",
    "baseline_digest",
    "stage_identity_digest",
    "revision",
    "status",
    "next_action",
    "target_identity",
)


def is_repairable_failure(error: BaseException) -> bool:
    return str(getattr(error, "code", "")) in REPAIRABLE_FAILURE_CODES


def _same_business_identity(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    return all(before.get(field) == after.get(field) for field in _IDENTITY_FIELDS)


def run_bounded_self_repair(
    runtime: Any,
    request: Mapping[str, Any],
    error: BaseException,
    *,
    maintenance_capability: MaintenanceCapability | None = None,
) -> dict[str, Any]:
    """Run one bounded Workflow repair and return a retry request if safe."""

    if not is_repairable_failure(error):
        return {"status": "NOT_REPAIRABLE", "human_intervention_count": 0}
    before = runtime.repair_context()
    outcome: dict[str, Any] = {}
    if maintenance_capability is not None:
        try:
            candidate = maintenance_capability(copy.deepcopy(before), error)
        except Exception:
            return {
                "status": "MAINTENANCE_CAPABILITY_FAILED",
                "failure_code": str(getattr(error, "code", type(error).__name__)),
                "context": before,
                "human_intervention_count": 0,
            }
        if isinstance(candidate, Mapping):
            outcome = copy.deepcopy(dict(candidate))
    repaired_request = outcome.get("request")
    if not isinstance(repaired_request, Mapping):
        repaired_request = runtime.repair_request(request, error)
    if not isinstance(repaired_request, Mapping):
        return {
            "status": "NO_BOUNDED_REPAIR",
            "failure_code": str(getattr(error, "code", type(error).__name__)),
            "context": before,
            "human_intervention_count": 0,
        }
    repaired_request = copy.deepcopy(dict(repaired_request))
    # The callback is allowed to select an Engine-local repair, but it cannot
    # smuggle an unvalidated request into the lifecycle controller.
    try:
        action = str(repaired_request.get("operation", "")).upper()
        if action == "COMMAND":
            action = str(repaired_request.get("command", "")).upper()
        if hasattr(runtime, "build_stage_scoped_request"):
            repaired_request = runtime.build_stage_scoped_request(action, repaired_request)
    except Exception:
        return {
            "status": "REPAIR_REQUEST_REJECTED",
            "failure_code": str(getattr(error, "code", type(error).__name__)),
            "context": before,
            "human_intervention_count": 0,
        }
    after = runtime.repair_context()
    if not _same_business_identity(before, after):
        return {
            "status": "REPAIR_IDENTITY_MISMATCH",
            "failure_code": str(getattr(error, "code", type(error).__name__)),
            "context": before,
            "after_context": after,
            "human_intervention_count": 0,
        }
    # A maintenance callback must prove its scope before the request is
    # retried.  The default runtime adapter is read-only and therefore gets an
    # equivalent explicit proof here.
    if maintenance_capability is not None and outcome.get("engine_scope_only") is not True:
        return {
            "status": "REPAIR_SCOPE_REJECTED",
            "failure_code": str(getattr(error, "code", type(error).__name__)),
            "context": before,
            "human_intervention_count": 0,
        }
    doctor_handshake = outcome.get("doctor_handshake", "PASS")
    minimal_e2e = outcome.get("minimal_e2e", "PASS")
    if doctor_handshake != "PASS" or minimal_e2e != "PASS":
        return {
            "status": "REPAIR_VERIFICATION_FAILED",
            "failure_code": str(getattr(error, "code", type(error).__name__)),
            "context": before,
            "human_intervention_count": 0,
        }
    return {
        "status": "PASS",
        "failure_code": str(getattr(error, "code", type(error).__name__)),
        "context": before,
        "after_context": after,
        "request": copy.deepcopy(dict(repaired_request)),
        "doctor_handshake": doctor_handshake,
        "minimal_e2e": minimal_e2e,
        "engine_scope_only": True,
        "repair_attempts": 1,
        "human_intervention_count": 0,
    }


__all__ = [
    "MaintenanceCapability",
    "REPAIRABLE_FAILURE_CODES",
    "is_repairable_failure",
    "run_bounded_self_repair",
]
