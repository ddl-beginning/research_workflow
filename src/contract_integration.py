"""Small, deterministic Contract-driven workflow seam.

The module is deliberately a policy/identity layer.  It does not create a
second lifecycle state machine; callers hand the validated routing decision to
the existing StageController.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .contracts import ContractValidationError, canonical_json, sha256_json

CONTRACT_INTEGRATION_SCHEMA_VERSION = "contract_integration.v1"
MACHINE_SCHEMA_VERSION = "workflow_machine.v1"
WORKFLOW_DECISIONS = frozenset({"CONTINUE", "REPLAN", "STAGE_READY", "HUMAN_GATE", "BLOCKED"})
CAPABILITY_OWNERS = {
    "requirements.intake": "GPT", "research": "GPT", "design.synthesis": "GPT",
    "design.human_review": "Human", "stage.execution": "Codex", "review.technical": "GPT",
}
CAPABILITIES = frozenset(CAPABILITY_OWNERS)


def canonical_capability_contracts() -> dict[str, dict[str, Any]]:
    """Expose the existing V1 Contract seam as a transient lookup.

    No persisted registry is created. Versions and refs match the accepted
    Contract Integration V1 invocation identity; ownership lives here once.
    """
    return {name: {"actor": owner, "contract_ref": f"{name}@1.0",
                   "contract_version": "1.0", "machine_schema_version": MACHINE_SCHEMA_VERSION,
                   "policy": {}} for name, owner in CAPABILITY_OWNERS.items()}
_DECISION = re.compile(r"^\s*WORKFLOW_DECISION\s*:\s*([A-Z_]+)\s*$", re.MULTILINE)


class ContractIntegrationError(ContractValidationError):
    pass


@dataclass(frozen=True)
class CanonicalContract:
    capability: str
    contract_version: str
    machine_schema_version: str
    policy: Mapping[str, Any]
    contract_ref: str
    digest: str


def _text(value: Any, field: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit or "\x00" in value:
        raise ContractIntegrationError(f"{field} must be bounded non-empty text")
    return value.strip()


def resolve_canonical_contract(capability: str, registry: Mapping[str, Mapping[str, Any]]) -> CanonicalContract:
    capability = _text(capability, "capability")
    if capability not in CAPABILITIES:
        raise ContractIntegrationError(f"unsupported capability: {capability}")
    value = registry.get(capability)
    if not isinstance(value, Mapping):
        raise ContractIntegrationError(f"no canonical contract for capability: {capability}")
    if isinstance(value.get("contracts"), list):
        raise ContractIntegrationError("multiple contracts cannot be authoritative in one invocation")
    contract_version = _text(value.get("contract_version"), "contract_version")
    schema_version = _text(value.get("machine_schema_version"), "machine_schema_version")
    policy = value.get("policy", {})
    if not isinstance(policy, Mapping):
        raise ContractIntegrationError("contract policy must be an object")
    ref = _text(value.get("contract_ref", f"{capability}@{contract_version}"), "contract_ref")
    digest = sha256_json({"capability": capability, "contract_version": contract_version,
                          "machine_schema_version": schema_version, "policy": dict(policy), "contract_ref": ref})
    return CanonicalContract(capability, contract_version, schema_version, dict(policy), ref, digest)


def assemble_context(contract: CanonicalContract, *, requirement_ref: Mapping[str, Any] | None = None,
                     design_ref: Mapping[str, Any] | None = None, stage_ref: Mapping[str, Any] | None = None,
                     evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    def bounded(value: Mapping[str, Any] | None, name: str) -> dict[str, Any] | None:
        if value is None: return None
        if not isinstance(value, Mapping): raise ContractIntegrationError(f"{name} must be an object")
        return {str(k): v for k, v in value.items() if str(k) not in {"prompt", "raw_response", "cookies", "token"}}
    context = {"contract": {"capability": contract.capability, "contract_version": contract.contract_version,
                              "machine_schema_version": contract.machine_schema_version, "contract_ref": contract.contract_ref,
                              "digest": contract.digest},
               "requirement_ref": bounded(requirement_ref, "requirement_ref"),
               "design_ref": bounded(design_ref, "design_ref"), "stage_ref": bounded(stage_ref, "stage_ref"),
               "evidence": bounded(evidence, "evidence")}
    context["context_digest"] = sha256_json(context)
    return context


def parse_machine_routing(response_text: str, *, schema_version: str = MACHINE_SCHEMA_VERSION) -> dict[str, Any]:
    if schema_version != MACHINE_SCHEMA_VERSION:
        raise ContractIntegrationError("unknown machine schema version")
    if not isinstance(response_text, str) or not response_text.strip():
        raise ContractIntegrationError("machine response is empty")
    matches = _DECISION.findall(response_text)
    if len(matches) != 1 or matches[0] not in WORKFLOW_DECISIONS:
        raise ContractIntegrationError("response must contain exactly one allowed WORKFLOW_DECISION")
    nonempty = [line.strip() for line in response_text.splitlines() if line.strip()]
    if not nonempty or not _DECISION.fullmatch(nonempty[-1]):
        raise ContractIntegrationError("WORKFLOW_DECISION must be the final non-empty line")
    return {"machine_schema_version": schema_version, "workflow_decision": matches[0], "next_action": matches[0]}


def validate_transition(capability: str, decision: str, allowed: Mapping[str, set[str]] | None = None) -> str:
    capability = _text(capability, "capability")
    decision = _text(decision, "workflow_decision")
    if capability not in CAPABILITIES or decision not in WORKFLOW_DECISIONS:
        raise ContractIntegrationError("unknown capability or workflow decision")
    defaults = {capability: set(WORKFLOW_DECISIONS)} if allowed is None else allowed
    if decision not in defaults.get(capability, set()):
        raise ContractIntegrationError("illegal capability transition")
    return decision


def invoke_contract(capability: str, registry: Mapping[str, Mapping[str, Any]], runner: Callable[[dict[str, Any]], str],
                    *, requirement_ref: Mapping[str, Any] | None = None, design_ref: Mapping[str, Any] | None = None,
                    stage_ref: Mapping[str, Any] | None = None, evidence: Mapping[str, Any] | None = None,
                    allowed: Mapping[str, set[str]] | None = None) -> dict[str, Any]:
    contract = resolve_canonical_contract(capability, registry)
    context = assemble_context(contract, requirement_ref=requirement_ref, design_ref=design_ref, stage_ref=stage_ref, evidence=evidence)
    response = runner(context)
    routing = parse_machine_routing(response, schema_version=contract.machine_schema_version)
    decision = validate_transition(capability, routing["workflow_decision"], allowed)
    return {"contract": {"capability": contract.capability, "contract_version": contract.contract_version,
                          "machine_schema_version": contract.machine_schema_version, "contract_ref": contract.contract_ref,
                          "digest": contract.digest}, "context_digest": context["context_digest"],
            "routing": routing, "transition": {"validated": True, "decision": decision},
            "provenance": {"requirement_ref": requirement_ref, "design_ref": design_ref, "stage_ref": stage_ref}}


__all__ = ["CAPABILITIES", "CONTRACT_INTEGRATION_SCHEMA_VERSION", "MACHINE_SCHEMA_VERSION", "WORKFLOW_DECISIONS",
           "CanonicalContract", "ContractIntegrationError", "resolve_canonical_contract", "assemble_context",
           "parse_machine_routing", "validate_transition", "invoke_contract"]
