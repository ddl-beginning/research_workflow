"""Canonical method/evidence policy shared by GPT-facing workflow seams.

The workflow has several bounded consultations (Discovery, Blueprint, Stage
planning, replanning and closeout).  This module is the single source of the
method rule that is included in those prompts or recorded beside the
corresponding decision.  Keeping the rule here prevents one consultation
from silently using a weaker evidence standard than another.

The text is intentionally short and transport-neutral.  It is not a prompt,
response, transcript, or local project claim and must not be persisted as raw
consultation content.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping


METHOD_EVIDENCE_POLICY_ID = "method_evidence_policy.v1"

# Keep the layer names stable: they are useful in bounded review metadata and
# let callers distinguish an observation from a GPT recommendation without
# copying this prose into each prompt builder.
METHOD_EVIDENCE_LAYERS = (
    "REQUIREMENTS",
    "LOCAL_AUDIT",
    "GPT_HYPOTHESIS",
    "EXECUTION_EVIDENCE",
    "HUMAN_DECISION",
)

ROUTING_CONTRACT_OUTPUT_REQUIREMENT = """Research turn output requirement (routing_contract.v1): return the research body, then exactly one final line in the form ROUTING_CONTRACT_V1: <valid JSON object>. The object must contain only these keys and exact enums: research_status=CONTINUE|SUFFICIENT|NEEDS_EXPERIMENT|BLOCKED; next_action=TARGETED_RESEARCH|MINIMAL_EXPERIMENT|DESIGN_SYNTHESIS|HUMAN_DESIGN_REVIEW|STOP; design_status=NOT_STARTED|DRAFT|READY_FOR_HUMAN_REVIEW; unresolved_gaps is a string array. Do not emit a second routing marker or infer a route from prose."""

def routing_contract_prompt_text() -> str:
    return (ROUTING_CONTRACT_OUTPUT_REQUIREMENT + "\nRelations: CONTINUE requires TARGETED_RESEARCH; NEEDS_EXPERIMENT requires MINIMAL_EXPERIMENT; BLOCKED requires STOP. SUFFICIENT requires DESIGN_SYNTHESIS unless design_status is READY_FOR_HUMAN_REVIEW, which requires HUMAN_DESIGN_REVIEW. Use a single-line JSON object after the marker, outside Markdown fences. Choose values honestly; never emit enum alternatives separated by | as a value.")

CANONICAL_METHOD_EVIDENCE_POLICY = """Method and evidence policy (method_evidence_policy.v1):
1. Start from the real project/user-visible goal. Separate physical or operational constraints from representation/software choices, and remove unnecessary intermediate representations before designing a route.
2. Evidence priority is explicit: mature peer-reviewed papers, mature open-source projects, and reproducible engineering case studies（成熟论文/成熟开源项目/可复现实工程案例）优先 over weak or uncited claims. Record only bounded source/version/digest/URL references and local verification status.
3. Epistemic labels are mandatory: use the exact labels “项目事实”, “项目推断”, “证据不足”, “GPT建议”, “已执行证据”, and “用户决策”. Never promote “项目推断” or “GPT建议” to “项目事实”; preserve contradictions and negative evidence.
4. Compare Candidate 0 with at least one meaningful alternative route, not a strawman. State why the alternative is meaningful, what Candidate 0 preserves, and which bounded evidence supports the recommendation.
5. Actively expand the route space: search for at least one simpler or materially different method/architecture outside the current route and a cheap falsification. Do not anchor on sunk cost; if no meaningful alternative is found, record the search boundary and the exact label “证据不足”.
6. Governance and closeout are deterministic and safe: local audit and executable evidence precede architecture/Stage decisions; never auto-select production architecture, create/start a Stage, approve a Human Gate, or claim closeout from GPT alone. Require the existing deterministic contract and explicit human decision, and keep prompts, raw responses, credentials, browser state, and unrestricted paths out of evidence and human-facing artifacts.
"""


def method_evidence_policy_text(*, context: str | None = None) -> str:
    """Return the canonical policy with an optional bounded context label."""

    if context is None:
        return CANONICAL_METHOD_EVIDENCE_POLICY
    label = str(context).strip()
    if not label or len(label) > 80 or any(char in label for char in "\r\n\x00"):
        raise ValueError("context must be a short single-line label")
    return f"{CANONICAL_METHOD_EVIDENCE_POLICY}\nConsultation context: {label}\n"


def method_evidence_policy_reference() -> dict[str, Any]:
    """Return bounded metadata suitable for plans/decision receipts."""

    digest = hashlib.sha256(CANONICAL_METHOD_EVIDENCE_POLICY.encode("utf-8")).hexdigest()
    return {
        "policy_id": METHOD_EVIDENCE_POLICY_ID,
        "policy_digest": digest,
        "layers": list(METHOD_EVIDENCE_LAYERS),
        "key_rules": [
            "mature peer-reviewed papers, mature open-source projects, and reproducible engineering case studies",
            "exact labels: 项目事实, 项目推断, 证据不足, GPT建议, 已执行证据, 用户决策",
            "Candidate 0 versus at least one meaningful alternative route",
            "actively expand the route space and cheap falsification",
            "deterministic governance and explicit human decision; no synthetic auto-approval",
        ],
    }


def attach_method_evidence_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a decision envelope and attach only the policy reference."""

    result = dict(value)
    result["method_evidence_policy"] = method_evidence_policy_reference()
    return result


__all__ = [
    "CANONICAL_METHOD_EVIDENCE_POLICY",
    "METHOD_EVIDENCE_LAYERS",
    "METHOD_EVIDENCE_POLICY_ID",
    "attach_method_evidence_policy",
    "method_evidence_policy_reference",
    "method_evidence_policy_text",
    "ROUTING_CONTRACT_OUTPUT_REQUIREMENT",
    "routing_contract_prompt_text",
]
