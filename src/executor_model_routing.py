"""Request-scoped model profiles for the native Codex executor.

The routing decision is deliberately independent from Stage lifecycle.  A
profile is selected for one executor request, then the provider binds the
profile to the real ``codex exec`` command.  No prompt, UI, or repository
instruction can change the profile mapping.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import ContractValidationError


STANDARD = "STANDARD"
FRONTIER = "FRONTIER"

# This is the single model/profile mapping source for V1.
PROFILE_BINDINGS: dict[str, dict[str, str]] = {
    STANDARD: {"model": "gpt-5.6-luna", "reasoning_effort": "max"},
    FRONTIER: {"model": "gpt-6-astra", "reasoning_effort": "low"},
}

AUTHORITATIVE_FRONTIER_AUTHORITY = "authoritative_executor_request"


@dataclass(frozen=True)
class RoutingDecision:
    execution_profile: str
    model: str
    reasoning_effort: str
    auth_mode: str
    profile_derivation_reason: str
    executor_request_id: str

    def bounded_view(self) -> dict[str, str]:
        return {
            "execution_profile": self.execution_profile,
            "actual_model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "auth_mode": self.auth_mode,
            "profile_derivation_reason": self.profile_derivation_reason,
            "executor_request_id": self.executor_request_id,
        }


def _bounded_text(value: Any, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum or "\x00" in value:
        raise ContractValidationError(f"{field} must be a bounded non-empty string")
    return value.strip()


def _requested_profile(metadata: Mapping[str, Any]) -> str | None:
    value = metadata.get("execution_profile")
    if value is None:
        return None
    profile = _bounded_text(value, "execution_profile", maximum=32).upper()
    if profile not in PROFILE_BINDINGS:
        raise ContractValidationError("execution_profile must be STANDARD or FRONTIER")
    return profile


def _same_objective_review_proves_unresolved(metadata: Mapping[str, Any]) -> bool:
    review = metadata.get("technical_review")
    if not isinstance(review, Mapping):
        return False
    return (
        review.get("authoritative") is True
        and review.get("same_semantic_objective") is True
        and review.get("unresolved") is True
    )


def derive_execution_profile(metadata: Mapping[str, Any] | None = None) -> tuple[str, str]:
    """Derive one stable profile without heuristic escalation.

    ``FRONTIER`` is accepted only from an explicitly authoritative executor
    request, or from two legal STANDARD failures plus an authoritative review
    that proves the same semantic objective is still unresolved.
    """

    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise ContractValidationError("executor routing metadata must be an object")
    requested = _requested_profile(metadata)
    if requested == FRONTIER:
        if metadata.get("execution_profile_authority") != AUTHORITATIVE_FRONTIER_AUTHORITY:
            raise ContractValidationError(
                "FRONTIER requires an authoritative executor request"
            )
        return FRONTIER, "authoritative_executor_request"
    if requested == STANDARD:
        return STANDARD, "explicit_executor_request_standard"

    failures = metadata.get("standard_execution_failures", 0)
    if isinstance(failures, bool) or not isinstance(failures, int):
        raise ContractValidationError("standard_execution_failures must be a non-negative integer")
    if failures < 0:
        raise ContractValidationError("standard_execution_failures must be non-negative")
    if failures >= 2 and _same_objective_review_proves_unresolved(metadata):
        return FRONTIER, "same_semantic_objective_two_standard_failures_authoritative_review"
    return STANDARD, "default_standard"


def derive_routing_decision(
    metadata: Mapping[str, Any] | None = None,
    *,
    executor_request_id: str | None = None,
    auth_mode: str = "chatgpt",
) -> RoutingDecision:
    if metadata is None:
        metadata = {}
    profile, reason = derive_execution_profile(metadata)
    checked_auth = _bounded_text(auth_mode, "auth_mode", maximum=32).lower()
    if checked_auth != "chatgpt":
        raise ContractValidationError("executor model routing requires ChatGPT auth")
    request_id = executor_request_id or metadata.get("executor_request_id") or metadata.get("request_id")
    request_id = _bounded_text(request_id, "executor_request_id", maximum=256)
    binding = PROFILE_BINDINGS[profile]
    return RoutingDecision(
        execution_profile=profile,
        model=binding["model"],
        reasoning_effort=binding["reasoning_effort"],
        auth_mode=checked_auth,
        profile_derivation_reason=reason,
        executor_request_id=request_id,
    )


__all__ = [
    "AUTHORITATIVE_FRONTIER_AUTHORITY",
    "FRONTIER",
    "PROFILE_BINDINGS",
    "RoutingDecision",
    "STANDARD",
    "derive_execution_profile",
    "derive_routing_decision",
]
