import json
import pytest

from src.research_routing import parse_research_routing, validate_research_routing_contract
from src.contracts import ContractValidationError
from src.research_prompt_policy import routing_contract_prompt_text

def reply(status, action, design="DRAFT"):
    return "analysis\nROUTING_CONTRACT_V1: " + json.dumps({"research_status": status, "unresolved_gaps": ["gap"], "next_action": action, "design_status": design})

@pytest.mark.parametrize("status,action,design", [("CONTINUE","TARGETED_RESEARCH","DRAFT"),("NEEDS_EXPERIMENT","MINIMAL_EXPERIMENT","NOT_STARTED"),("BLOCKED","STOP","NOT_STARTED"),("SUFFICIENT","DESIGN_SYNTHESIS","DRAFT"),("SUFFICIENT","HUMAN_DESIGN_REVIEW","READY_FOR_HUMAN_REVIEW")])
def test_required_routes(status, action, design):
    assert parse_research_routing(reply(status, action, design))["next_action"] == action

@pytest.mark.parametrize("text", ["ROUTING_CONTRACT_V1: {}", reply("CONTINUE", "STOP") + "\nROUTING_CONTRACT_V1: {}", reply("UNKNOWN", "STOP"), reply("BLOCKED", "STOP", "READY_FOR_HUMAN_REVIEW")])
def test_invalid_routing_fails_closed(text):
    with pytest.raises(ContractValidationError):
        parse_research_routing(text)

def test_prompt_requirement_is_explicit():
    assert "ROUTING_CONTRACT_V1" in routing_contract_prompt_text()
