"""Provider-neutral executor contracts and bounded selection policy.

The Stage controller is the workflow authority; it requests an execution and
records a normalized result, but it does not know which coding agent produced
that result.  This module is intentionally small: providers advertise bounded
capabilities and receive an immutable :class:`ExecutionRequest`.  They return
an :class:`ExecutionResult`; only the controller may apply the result to Stage
state.

No provider is started here and no process, network, or MCP operation is
performed.  The CodexPro integration uses this seam with injected terminal
evidence in :mod:`src.codexpro_adapter`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .contracts import ContractValidationError, _ensure_relative_path, validate_against_schema


def _text(value: Any, field: str, *, required: bool = True, maximum: int = 4000) -> str:
    if not isinstance(value, str):
        raise ContractValidationError(f"{field} must be a string")
    checked = value.strip()
    if required and not checked:
        raise ContractValidationError(f"{field} must be a non-empty string")
    if len(checked) > maximum or "\x00" in checked:
        raise ContractValidationError(f"{field} is invalid or too long")
    return checked


def _string_tuple(values: Sequence[str] | None, field: str, *, paths: bool = False) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes, bytearray)):
        raise ContractValidationError(f"{field} must be an array")
    result: list[str] = []
    for index, value in enumerate(values):
        item = _text(value, f"{field}[{index}]", maximum=1000)
        if paths:
            item = _ensure_relative_path(item, f"{field}[{index}]")
        if item in result:
            raise ContractValidationError(f"{field} must not contain duplicates")
        result.append(item)
    return tuple(result)


@dataclass(frozen=True)
class ExecutionRequest:
    """Immutable, provider-neutral request authorized by a Stage.

    ``action_map`` is a detached JSON-friendly snapshot, not an executable
    object.  ``plan_id`` and ``task_id`` are optional because the current
    StageController request payload predates this seam; providers that emit
    ``codex_result.v1`` should supply them at the boundary.
    """

    stage_id: str
    objective: str
    iteration_index: int
    allowed_paths: tuple[str, ...]
    protected_paths: tuple[str, ...] = ()
    baseline_digest: str | None = None
    execution_mode: str = "PRIMARY"
    required_capabilities: tuple[str, ...] = ()
    preferred_provider: str | None = None
    action_map: Mapping[str, Any] = field(default_factory=dict)
    plan_id: str | None = None
    task_id: str | None = None
    workspace_root: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # Stage iteration and execution attempt are separate coordinates.  These
    # fields are appended with defaults to preserve positional compatibility
    # for existing providers.
    attempt_index: int = 1
    retry_budget: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "stage_id", _text(self.stage_id, "stage_id", maximum=256))
        object.__setattr__(self, "objective", _text(self.objective, "objective"))
        if isinstance(self.iteration_index, bool) or not isinstance(self.iteration_index, int) or self.iteration_index < 1:
            raise ContractValidationError("iteration_index must be a positive integer")
        object.__setattr__(self, "allowed_paths", _string_tuple(self.allowed_paths, "allowed_paths", paths=True))
        if not self.allowed_paths:
            raise ContractValidationError("allowed_paths must contain at least one path")
        object.__setattr__(self, "protected_paths", _string_tuple(self.protected_paths, "protected_paths", paths=True))
        if self.baseline_digest is not None:
            object.__setattr__(self, "baseline_digest", _text(self.baseline_digest, "baseline_digest", maximum=256))
        object.__setattr__(self, "execution_mode", _text(self.execution_mode, "execution_mode", maximum=64).upper())
        object.__setattr__(
            self,
            "required_capabilities",
            _string_tuple(self.required_capabilities, "required_capabilities", paths=False),
        )
        if self.preferred_provider is not None:
            object.__setattr__(self, "preferred_provider", _text(self.preferred_provider, "preferred_provider", maximum=128))
        if not isinstance(self.action_map, Mapping):
            raise ContractValidationError("action_map must be an object")
        object.__setattr__(self, "action_map", copy.deepcopy(dict(self.action_map)))
        if self.action_map:
            # ``ActionMap.to_dict()`` is the intended wire shape.  A provider
            # may receive a request only after the controller/policy layer has
            # validated and explicitly enabled the map.
            if self.action_map.get("validated") is not True:
                raise ContractValidationError("action_map must be validated before execution")
            if self.action_map.get("execute") is not True:
                raise ContractValidationError("action_map must explicitly enable execution")
        if self.plan_id is not None:
            object.__setattr__(self, "plan_id", _text(self.plan_id, "plan_id", maximum=256))
        if self.task_id is not None:
            object.__setattr__(self, "task_id", _text(self.task_id, "task_id", maximum=256))
        if self.workspace_root is not None:
            # This is configuration data only.  Do not resolve or access it in
            # the core contract; providers decide how their local runtime uses
            # an explicitly configured workspace.
            object.__setattr__(self, "workspace_root", _text(self.workspace_root, "workspace_root", maximum=2000))
        if not isinstance(self.metadata, Mapping):
            raise ContractValidationError("metadata must be an object")
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))
        if isinstance(self.attempt_index, bool) or not isinstance(self.attempt_index, int) or self.attempt_index < 1:
            raise ContractValidationError("attempt_index must be a positive integer")
        if self.retry_budget is not None:
            if isinstance(self.retry_budget, bool) or not isinstance(self.retry_budget, int) or self.retry_budget < 1:
                raise ContractValidationError("retry_budget must be a positive integer")

    @classmethod
    def from_stage_request(
        cls,
        request: Mapping[str, Any],
        *,
        plan_id: str | None = None,
        task_id: str | None = None,
        required_capabilities: Sequence[str] = (),
        execution_mode: str | None = None,
        preferred_provider: str | None = None,
        action_map: Mapping[str, Any] | None = None,
        workspace_root: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ExecutionRequest":
        """Convert one controller request without coupling it to a provider."""

        if not isinstance(request, Mapping):
            raise ContractValidationError("stage executor request must be an object")
        request_id = request.get("request_id")
        checked_task_id = task_id if task_id is not None else request_id
        if checked_task_id is None:
            raise ContractValidationError("stage executor request requires request_id or task_id")
        return cls(
            stage_id=request.get("stage_id"),
            objective=request.get("objective"),
            iteration_index=request.get("iteration_index"),
            allowed_paths=request.get("allowed_paths"),
            protected_paths=request.get("protected_paths", ()),
            baseline_digest=request.get("baseline_digest"),
            execution_mode=execution_mode or request.get("mode", "PRIMARY"),
            required_capabilities=tuple(required_capabilities),
            preferred_provider=preferred_provider,
            action_map=action_map or {},
            plan_id=plan_id,
            task_id=checked_task_id,
            workspace_root=workspace_root,
            metadata={
                "lane": request.get("lane", "PRIMARY"),
                "request_id": request_id,
                **(dict(metadata) if metadata is not None else {}),
            },
            attempt_index=request.get("attempt_index", request.get("attempt_number", 1)),
            retry_budget=request.get("retry_budget"),
        )

    def bounded_view(self) -> dict[str, Any]:
        """Return the non-secret request facts suitable for review/evidence."""

        safe_metadata = {
            key: copy.deepcopy(self.metadata[key])
            for key in (
                "request_id",
                "executor_request_id",
                "execution_profile",
                "execution_profile_authority",
                "standard_execution_failures",
                "lane",
                "expected_plan_hash",
                "abstraction_layer",
            )
            if key in self.metadata
        }
        return {
            "stage_id": self.stage_id,
            "objective": self.objective,
            "iteration_index": self.iteration_index,
            "allowed_paths": list(self.allowed_paths),
            "protected_paths": list(self.protected_paths),
            "baseline_digest": self.baseline_digest,
            "execution_mode": self.execution_mode,
            "required_capabilities": list(self.required_capabilities),
            "preferred_provider": self.preferred_provider,
            "action_map": copy.deepcopy(dict(self.action_map)),
            "plan_id": self.plan_id,
            "task_id": self.task_id,
            "workspace_root": self.workspace_root,
            "metadata": safe_metadata,
            "attempt_index": self.attempt_index,
            "retry_budget": self.retry_budget,
        }


@dataclass(frozen=True)
class ExecutionResult:
    """Provider output before the deterministic controller records it."""

    provider_id: str
    result: Mapping[str, Any]
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id", maximum=128))
        if not isinstance(self.result, Mapping):
            raise ContractValidationError("execution result must be an object")
        if not isinstance(self.evidence, Mapping):
            raise ContractValidationError("execution evidence must be an object")
        object.__setattr__(self, "result", copy.deepcopy(dict(self.result)))
        object.__setattr__(self, "evidence", copy.deepcopy(dict(self.evidence)))

    def to_stage_result(self) -> dict[str, Any]:
        """Return a detached result suitable for ``StageController``."""

        return copy.deepcopy(dict(self.result))


@dataclass(frozen=True)
class ExecutorDescriptor:
    """Bounded provider metadata used for discovery and selection.

    ``priority`` is an optional configured precedence.  Higher values win
    when no explicit user preference is supplied; the default ``0`` preserves
    the behavior of descriptors created before this field existed.
    """

    provider_id: str
    capabilities: tuple[str, ...] = ()
    execution_modes: tuple[str, ...] = ("PRIMARY",)
    available: bool = True
    reliability: float = 0.0
    cost: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # Keep this field after the existing fields so positional construction of
    # older descriptors remains source-compatible.
    priority: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id", maximum=128))
        object.__setattr__(self, "capabilities", _string_tuple(self.capabilities, "capabilities"))
        modes = tuple(_text(item, f"execution_modes[{index}]", maximum=64).upper() for index, item in enumerate(self.execution_modes))
        if not modes:
            raise ContractValidationError("execution_modes must contain at least one mode")
        object.__setattr__(self, "execution_modes", modes)
        if not isinstance(self.available, bool):
            raise ContractValidationError("available must be boolean")
        if isinstance(self.reliability, bool) or not isinstance(self.reliability, (int, float)) or not 0 <= self.reliability <= 1:
            raise ContractValidationError("reliability must be between 0 and 1")
        if isinstance(self.cost, bool) or not isinstance(self.cost, (int, float)) or self.cost < 0:
            raise ContractValidationError("cost must be a non-negative number")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ContractValidationError("priority must be an integer")
        if not isinstance(self.metadata, Mapping):
            raise ContractValidationError("descriptor metadata must be an object")
        object.__setattr__(self, "metadata", copy.deepcopy(dict(self.metadata)))

    def supports(self, request: ExecutionRequest) -> bool:
        return (
            self.available
            and request.execution_mode in self.execution_modes
            and set(request.required_capabilities).issubset(self.capabilities)
        )

    def bounded_view(self) -> dict[str, Any]:
        """Return metadata safe to include in a GPT review packet."""

        return {
            "provider_id": self.provider_id,
            "capabilities": list(self.capabilities),
            "execution_modes": list(self.execution_modes),
            "available": self.available,
            "reliability": self.reliability,
            "cost": self.cost,
            "priority": self.priority,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@runtime_checkable
class ExecutorProvider(Protocol):
    """Minimal provider seam implemented by native and handoff adapters."""

    @property
    def descriptor(self) -> ExecutorDescriptor:
        ...

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        ...


@dataclass(frozen=True)
class ExecutorSelection:
    """Deterministic selection outcome plus bounded decision evidence."""

    provider_id: str
    candidates: tuple[dict[str, Any], ...]
    reason: str
    review_recommended: bool = False

    def bounded_view(self) -> dict[str, Any]:
        return {
            "selected_provider": self.provider_id,
            "candidates": copy.deepcopy(list(self.candidates)),
            "reason": self.reason,
            "review_recommended": self.review_recommended,
        }


def discover_executors(providers: Sequence[ExecutorProvider]) -> tuple[ExecutorDescriptor, ...]:
    """Collect provider descriptors without starting or probing providers."""

    if isinstance(providers, (str, bytes, bytearray)):
        raise ContractValidationError("providers must be an array")
    descriptors: list[ExecutorDescriptor] = []
    seen: set[str] = set()
    for index, provider in enumerate(providers):
        descriptor = getattr(provider, "descriptor", None)
        if not isinstance(descriptor, ExecutorDescriptor):
            raise ContractValidationError(f"providers[{index}] must expose an ExecutorDescriptor")
        if descriptor.provider_id in seen:
            raise ContractValidationError(f"duplicate executor provider: {descriptor.provider_id}")
        seen.add(descriptor.provider_id)
        descriptors.append(descriptor)
    return tuple(descriptors)


def select_executor(
    request: ExecutionRequest,
    providers: Sequence[ExecutorProvider],
) -> ExecutorSelection:
    """Select one eligible provider with explicit preference then stable scoring.

    Eligibility is deterministic: unavailable providers, unsupported
    capabilities, and unsupported modes are excluded.  A user preference wins
    only when that provider is eligible.  Otherwise configured ``priority`` is
    maximized, reliability is maximized, cost is minimized, and provider id
    breaks ties.  The bounded review flag tells a caller when more than one
    eligible provider existed; it does not grant a provider authority to alter
    Stage state.
    """

    if not isinstance(request, ExecutionRequest):
        raise ContractValidationError("request must be an ExecutionRequest")
    descriptors = discover_executors(providers)
    eligible = [item for item in descriptors if item.supports(request)]
    if not eligible:
        raise ContractValidationError("no available executor satisfies the request capabilities and mode")
    preferred = request.preferred_provider
    if preferred is not None:
        chosen = next((item for item in eligible if item.provider_id == preferred), None)
        if chosen is not None:
            reason = "explicit user preference matched an eligible executor"
        else:
            chosen = sorted(
                eligible,
                key=lambda item: (-int(item.priority), -float(item.reliability), float(item.cost), item.provider_id),
            )[0]
            reason = "preferred executor was unavailable or ineligible; deterministic priority/reliability/cost policy selected a fallback"
    else:
        chosen = sorted(
            eligible,
            key=lambda item: (-int(item.priority), -float(item.reliability), float(item.cost), item.provider_id),
        )[0]
        reason = "deterministic configured priority/reliability/cost policy selected the eligible executor"
    return ExecutorSelection(
        provider_id=chosen.provider_id,
        candidates=tuple(item.bounded_view() for item in eligible),
        reason=reason,
        review_recommended=len(eligible) > 1 and preferred is None,
    )


def validate_execution_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the shared result schema without inferring a Stage decision."""

    if not isinstance(result, Mapping):
        raise ContractValidationError("execution result must be an object")
    detached = copy.deepcopy(dict(result))
    validate_against_schema(detached, "codex_result")
    return detached


__all__ = [
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutorDescriptor",
    "ExecutorProvider",
    "ExecutorSelection",
    "discover_executors",
    "select_executor",
    "validate_execution_result",
]
