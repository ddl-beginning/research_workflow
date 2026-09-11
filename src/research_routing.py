"""Strict parser and validator for Research Routing Contract V1."""
from __future__ import annotations
import json, re
from typing import Any, Mapping
from .contracts import ContractValidationError, validate_against_schema

RESEARCH_STATUSES = frozenset({"CONTINUE", "SUFFICIENT", "NEEDS_EXPERIMENT", "BLOCKED"})
NEXT_ACTIONS = frozenset({"TARGETED_RESEARCH", "MINIMAL_EXPERIMENT", "DESIGN_SYNTHESIS", "HUMAN_DESIGN_REVIEW", "STOP"})
DESIGN_STATUSES = frozenset({"NOT_STARTED", "DRAFT", "READY_FOR_HUMAN_REVIEW"})
_MARKER = re.compile(r"^\s*ROUTING_CONTRACT_V1\s*:\s*(\{.*\})\s*$")

def validate_research_routing_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping): raise ContractValidationError("routing contract must be an object")
    obj = dict(value)
    validate_against_schema(obj, "research_routing_contract.v1")
    if not isinstance(obj["unresolved_gaps"], list) or any(not isinstance(x, str) or not x.strip() for x in obj["unresolved_gaps"]):
        raise ContractValidationError("unresolved_gaps must contain non-empty strings")
    if len(set(obj["unresolved_gaps"])) != len(obj["unresolved_gaps"]): raise ContractValidationError("unresolved_gaps must not contain duplicates")
    status, action, design = obj["research_status"], obj["next_action"], obj["design_status"]
    expected = {"CONTINUE":"TARGETED_RESEARCH", "NEEDS_EXPERIMENT":"MINIMAL_EXPERIMENT", "BLOCKED":"STOP"}
    if status in expected and action != expected[status]: raise ContractValidationError(f"{status} requires {expected[status]}")
    if status == "SUFFICIENT":
        required = "HUMAN_DESIGN_REVIEW" if design == "READY_FOR_HUMAN_REVIEW" else "DESIGN_SYNTHESIS"
        if action != required: raise ContractValidationError(f"SUFFICIENT requires {required}")
    if action == "HUMAN_DESIGN_REVIEW" and design != "READY_FOR_HUMAN_REVIEW": raise ContractValidationError("HUMAN_DESIGN_REVIEW requires READY_FOR_HUMAN_REVIEW")
    if action == "DESIGN_SYNTHESIS" and status != "SUFFICIENT": raise ContractValidationError("DESIGN_SYNTHESIS requires SUFFICIENT")
    if status == "BLOCKED" and design == "READY_FOR_HUMAN_REVIEW": raise ContractValidationError("BLOCKED cannot be design-ready")
    return {"research_status": status, "unresolved_gaps": list(obj["unresolved_gaps"]), "next_action": action, "design_status": design}

def parse_research_routing(response_text: str) -> dict[str, Any]:
    if not isinstance(response_text, str) or not response_text.strip(): raise ContractValidationError("GPT response is empty")
    matches = [_MARKER.match(line) for line in response_text.splitlines()]
    matches = [m for m in matches if m]
    if len(matches) != 1: raise ContractValidationError("response must contain exactly one ROUTING_CONTRACT_V1 marker")
    if response_text.rstrip().splitlines()[-1].strip() != response_text.splitlines()[[i for i, line in enumerate(response_text.splitlines()) if _MARKER.match(line)][0]].strip():
        raise ContractValidationError("routing contract must be the final non-empty line")
    try: payload = json.loads(matches[0].group(1))
    except json.JSONDecodeError as exc: raise ContractValidationError("routing contract JSON is malformed") from exc
    return validate_research_routing_contract(payload)
