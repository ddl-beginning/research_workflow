import pytest

from src.contract_integration import (
    ContractIntegrationError, assemble_context, invoke_contract,
    parse_machine_routing, resolve_canonical_contract, validate_transition,
)

REGISTRY = {"research": {"contract_version": "1.1", "machine_schema_version": "workflow_machine.v1",
                          "contract_ref": "research.v1.1", "policy": {"route": "sufficiency"}}}

def test_one_canonical_contract_and_separate_versions():
    c = resolve_canonical_contract("research", REGISTRY)
    assert c.contract_version == "1.1"
    assert c.machine_schema_version == "workflow_machine.v1"
    with pytest.raises(ContractIntegrationError):
        resolve_canonical_contract("research", {"research": {"contracts": [REGISTRY["research"]]}})

def test_parse_minimal_machine_language_fail_closed():
    assert parse_machine_routing("analysis\nWORKFLOW_DECISION: STAGE_READY")["next_action"] == "STAGE_READY"
    with pytest.raises(ContractIntegrationError):
        parse_machine_routing("WORKFLOW_DECISION: MAYBE")
    with pytest.raises(ContractIntegrationError):
        parse_machine_routing("WORKFLOW_DECISION: CONTINUE\nmore")

def test_e2e_assembly_invocation_parse_transition_and_identity():
    result = invoke_contract("research", REGISTRY, lambda ctx: "WORKFLOW_DECISION: CONTINUE",
                             requirement_ref={"ref": "req-1", "revision": 2},
                             design_ref={"ref": "design-1"}, stage_ref={"ref": "stage-1"},
                             evidence={"receipt": "r-1"}, allowed={"research": {"CONTINUE"}})
    assert result["transition"]["validated"] is True
    assert result["contract"]["contract_ref"] == "research.v1.1"
    assert result["provenance"]["stage_ref"]["ref"] == "stage-1"

def test_illegal_transition_rejected():
    with pytest.raises(ContractIntegrationError):
        validate_transition("research", "STAGE_READY", {"research": {"CONTINUE"}})
