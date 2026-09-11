"""Deterministic finite state logic for the Stage-Oriented Research Supervisor.

There is intentionally no worker loop in this module.  Each public operation
advances state by one explicit event and returns a JSON-serializable event
record, making ordinary iterations quiet and Human Gate transitions visible.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence

from .contracts import (
    ContractValidationError,
    canonical_json,
    compare_to_baseline,
    freeze_baseline,
    sha256_json,
    validate_against_schema,
    validate_result_report,
    validate_scoped_task,
    validate_stage_contract,
)
from .research_prompt_policy import attach_method_evidence_policy


class SupervisorError(RuntimeError):
    """Base error for invalid state transitions."""


class DecisionType(str, Enum):
    CONTINUE = "CONTINUE"
    REPLAN = "REPLAN"
    STAGE_READY = "STAGE_READY"
    HUMAN_GATE = "HUMAN_GATE"
    BLOCKED = "BLOCKED"


class StageStatus(str, Enum):
    ACTIVE = "ACTIVE"
    STAGE_READY = "STAGE_READY"
    HUMAN_GATE = "HUMAN_GATE"
    BLOCKED = "BLOCKED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class CriticMode(str, Enum):
    NONE = "NONE"
    MAJOR_CHALLENGER = "MAJOR_CHALLENGER"


# ``comparison_mode`` is deliberately an alias-shaped enum rather than a
# second set of values.  The contract historically called this field
# ``critic_mode``; keeping both names lets a caller describe the same bounded
# comparison without creating another mode that could fan out indefinitely.
ComparisonMode = CriticMode


RESOURCE_CLASSES = (
    "user_plan",
    "stage_contract",
    "baseline",
    "latest_result",
    "critic_packet",
    "decision_history",
    "stop_rules",
    "acceptance",
    "safety",
)


def _enum_value(value: str | Enum) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def architecture_check(
    same_abstraction_count: int,
    user_visible_failure: bool | None = None,
    *,
    threshold: int = 2,
    result_status: str | None = None,
    user_visible_improvement: bool | None = None,
    no_user_visible_improvement: bool | None = None,
    trigger: str | None = None,
) -> dict[str, Any]:
    """Evaluate the finite architecture-reset trigger.

    The stage contract uses a deliberately small trigger: two consecutive
    results at one abstraction layer with no user-visible improvement ask the
    planner to replan.  The primary ``route`` is always ``KEEP_COURSE`` or
    ``REPLAN``; ``legacy_route`` is retained only for old audit readers.  This
    function only reports the check; it never mutates a stage or opens a Human
    Gate.  A major uncertainty is a separate, explicit Human Gate condition
    handled by :func:`planner_decision`.

    ``user_visible_failure`` is retained as the legacy spelling for
    ``no_user_visible_improvement``.  New callers may provide either of the
    more precise flags, but conflicting flags are rejected instead of being
    silently reconciled.
    """

    if not isinstance(same_abstraction_count, int) or isinstance(same_abstraction_count, bool):
        raise ContractValidationError("same_abstraction_count must be an integer")
    for name, value in (
        ("user_visible_failure", user_visible_failure),
        ("user_visible_improvement", user_visible_improvement),
        ("no_user_visible_improvement", no_user_visible_improvement),
    ):
        if value is not None and not isinstance(value, bool):
            raise ContractValidationError(f"{name} must be boolean")
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 2:
        raise ContractValidationError("architecture threshold must be an integer >= 2")
    if trigger is not None and (not isinstance(trigger, str) or not trigger.strip()):
        raise ContractValidationError("architecture trigger must be a non-empty string")
    supplied = [
        value
        for value in (
            user_visible_failure,
            None if user_visible_improvement is None else not user_visible_improvement,
            no_user_visible_improvement,
        )
        if value is not None
    ]
    if supplied and any(value != supplied[0] for value in supplied[1:]):
        raise ContractValidationError("architecture improvement flags disagree")
    unresolved = supplied[0] if supplied else False
    triggered = unresolved and same_abstraction_count >= threshold
    trigger_name = trigger or (
        "same_abstraction_no_improvement" if triggered else "same_abstraction_check"
    )
    route = "REPLAN" if triggered else "KEEP_COURSE"
    return attach_method_evidence_policy({
        "triggered": triggered,
        "trigger": trigger_name,
        "threshold": threshold,
        "same_abstraction_count": same_abstraction_count,
        # Preserve the old field for consumers that already render it, while
        # exposing the precise condition used by the new Scenario B contract.
        "user_visible_failure": unresolved,
        "user_visible_improvement": not unresolved,
        "no_user_visible_improvement": unresolved,
        "route": route,
        # Keep the former label as an audit-only compatibility field.  The
        # primary route is intentionally one of KEEP_COURSE/REPLAN.
        "legacy_route": "ARCHITECTURE_RESET" if triggered else None,
        "reason": (
            f"{same_abstraction_count} consecutive iterations show no user-visible improvement "
            f"at one abstraction layer"
            if triggered
            else "architecture-reset threshold not met"
        ),
        # The architecture check is a planner trigger, not a Human Gate.
        "planner_decision": "REPLAN" if triggered else None,
        "result_status": result_status,
    })


def _architecture_checkpoint(
    trigger: str,
    route: str,
    reason: str,
    *,
    same_abstraction_count: int = 0,
    threshold: int = 2,
) -> dict[str, Any]:
    """Build a positive audit checkpoint for lifecycle events.

    Lifecycle checkpoints (stage start, core replacement preparation and
    closeout) are evidence that an architecture decision was considered; they
    are not result-trigger calculations.  They therefore deliberately carry
    ``triggered=True`` even when their route is KEEP_COURSE.
    """

    if route not in {"KEEP_COURSE", "REPLAN"}:
        raise ContractValidationError("architecture checkpoint route must be KEEP_COURSE or REPLAN")
    if not isinstance(trigger, str) or not trigger.strip():
        raise ContractValidationError("architecture checkpoint trigger must be non-empty")
    return attach_method_evidence_policy({
        "triggered": True,
        "trigger": trigger,
        "threshold": threshold,
        "same_abstraction_count": same_abstraction_count,
        "user_visible_failure": False,
        "user_visible_improvement": True,
        "no_user_visible_improvement": False,
        "route": route,
        "legacy_route": "ARCHITECTURE_RESET" if route == "REPLAN" else None,
        "planner_decision": route if route == "REPLAN" else None,
        "reason": reason,
        "result_status": None,
    })


def planner_decision(
    *,
    result_status: str,
    stage_ready: bool = False,
    human_gate_required: bool = False,
    architecture_reset: bool = False,
    scientific_result: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Map one result event to exactly one of the five planner decisions.

    Priority is intentionally explicit: a blocked executor result is terminal;
    a major uncertainty pauses the route for human choice; an architecture
    check asks for a silent replan; a ready milestone waits for stage review; a
    failed/falsified result asks for a new plan; otherwise the ordinary
    iteration continues silently.
    """

    status = str(result_status).upper()
    science = str(scientific_result).upper() if scientific_result is not None else None
    if status == "BLOCKED":
        decision = DecisionType.BLOCKED
        default_reason = "executor reported BLOCKED"
    elif human_gate_required:
        decision = DecisionType.HUMAN_GATE
        default_reason = "human judgment is required before another route is selected"
    elif architecture_reset:
        decision = DecisionType.REPLAN
        default_reason = "architecture check requires a new route at the same stage"
    elif status in {"FAILED", "FAILURE", "ERROR"} or science == "FALSIFIED":
        # A failed/error result can never be promoted to a ready milestone,
        # even when a caller accidentally leaves stage_ready set. An explicit
        # architecture/human gate still takes precedence for bounded failure
        # sequences that require a route choice.
        decision = DecisionType.REPLAN
        default_reason = "current hypothesis or route did not satisfy the experiment"
    elif stage_ready:
        decision = DecisionType.STAGE_READY
        default_reason = "stage acceptance criteria are satisfied"
    else:
        decision = DecisionType.CONTINUE
        default_reason = "ordinary iteration may continue"

    selected_reason = reason or default_reason
    return attach_method_evidence_policy({
        "decision": decision.value,
        "reason": selected_reason,
        "human_gate_required": decision == DecisionType.HUMAN_GATE,
        "silent": decision in {DecisionType.CONTINUE, DecisionType.REPLAN},
        "architecture_reset": bool(architecture_reset),
    })


def _critic_roles(mode: str | CriticMode) -> list[str]:
    normalized = _enum_value(mode).upper()
    if normalized == CriticMode.NONE.value:
        return []
    if normalized == CriticMode.MAJOR_CHALLENGER.value:
        # Hard cap: one primary plus one challenger.
        return ["primary", "challenger"]
    raise ContractValidationError(f"unsupported critic_mode: {mode!r}")


def _normalise_string_set(value: Any, field: str = "representative_set") -> list[str]:
    """Return a deterministic, non-empty list of representative identifiers."""

    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        values = list(value)
    else:
        raise ContractValidationError(f"{field} must be a string or an array of strings")
    if not values or any(not isinstance(item, str) or not item.strip() for item in values):
        raise ContractValidationError(f"{field} must contain non-empty strings")
    # A set is represented as a sorted unique list so both lanes receive the
    # exact same value and packet IDs remain replayable.
    return sorted(set(item.strip() for item in values))


def _requested_comparison_mode(contract: Mapping[str, Any], result: Mapping[str, Any]) -> str:
    """Resolve the comparison spelling used by a contract/result pair."""

    candidate = result.get("comparison_mode")
    if candidate is None:
        candidate = contract.get("comparison_mode")
    if candidate is None:
        candidate = contract.get("critic_mode", CriticMode.NONE)
    normalized = _enum_value(candidate).upper()
    if normalized in {"MAJOR", "MAJOR_REPRESENTATION_REPLACEMENT", "MAJOR_REPLACEMENT"}:
        normalized = CriticMode.MAJOR_CHALLENGER.value
    if normalized not in {CriticMode.NONE.value, CriticMode.MAJOR_CHALLENGER.value}:
        raise ContractValidationError(f"unsupported comparison_mode: {candidate!r}")
    return normalized


def _has_major_representation_replacement(contract: Mapping[str, Any], result: Mapping[str, Any]) -> bool:
    """Recognize explicit major representation replacement markers.

    The JSON contracts intentionally allow extension fields.  Supporting the
    common marker spellings here keeps the state machine strict about the
    resulting mode while remaining compatible with older fixture vocabulary.
    """

    if _requested_comparison_mode(contract, result) == CriticMode.MAJOR_CHALLENGER.value:
        return True
    for source in (contract, result):
        for key in (
            "major_representation_replacement",
            "major_representation_change",
            "representation_replacement",
            "representation_replaced",
            "major_replacement",
        ):
            value = source.get(key)
            if isinstance(value, bool) and value:
                return True
            if isinstance(value, str) and value.upper() in {
                "MAJOR",
                "REPLACEMENT",
                "MAJOR_REPLACEMENT",
                "TRUE",
            }:
                return True
    for source in (contract, result):
        value = source.get("representation_change")
        if isinstance(value, Mapping):
            severity = str(value.get("severity", value.get("magnitude", ""))).upper()
            kind = str(value.get("type", value.get("kind", ""))).upper()
            if severity in {"MAJOR", "HIGH", "CRITICAL"} and (
                "REPLAC" in kind or "REPRESENT" in kind or not kind
            ):
                return True
        elif isinstance(value, str) and value.upper() in {
            "MAJOR",
            "MAJOR_REPLACEMENT",
            "REPRESENTATION_REPLACEMENT",
        }:
            return True
    return False


def _has_major_uncertainty(result: Mapping[str, Any]) -> bool:
    """Return whether a result explicitly requires a human route choice."""

    for key in ("major_uncertainty", "uncertainty_is_major", "uncertainty_major"):
        value = result.get(key)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, str) and value.upper() in {"TRUE", "MAJOR", "HIGH", "CRITICAL"}:
            return True
    for key in ("uncertainty_level", "uncertainty_severity", "uncertainty"):
        value = result.get(key)
        if isinstance(value, str) and value.upper() in {"MAJOR", "HIGH", "CRITICAL"}:
            return True
        if isinstance(value, Mapping):
            severity = str(value.get("severity", value.get("level", ""))).upper()
            if severity in {"MAJOR", "HIGH", "CRITICAL"}:
                return True
    # AMBIGUOUS is the result vocabulary's explicit representation of major
    # uncertainty.  BLOCKED still wins in planner_decision's priority order.
    return str(result.get("scientific_result", "")).upper() == "AMBIGUOUS"


def _no_user_visible_improvement(
    result: Mapping[str, Any], comparison: Mapping[str, Any], baseline: Mapping[str, Any] | None = None
) -> bool:
    """Infer the architecture signal while honoring explicit result flags."""

    if "no_user_visible_improvement" in result:
        value = result["no_user_visible_improvement"]
        if not isinstance(value, bool):
            raise ContractValidationError("no_user_visible_improvement must be boolean")
        return value
    for key in ("user_visible_improvement", "visible_improvement", "improved"):
        if key in result:
            value = result[key]
            if not isinstance(value, bool):
                raise ContractValidationError(f"{key} must be boolean")
            return not value
    if "user_visible_failure" in result:
        value = result["user_visible_failure"]
        if not isinstance(value, bool):
            raise ContractValidationError("user_visible_failure must be boolean")
        if value:
            return True
    status = str(result.get("status", "")).upper()
    science = str(result.get("scientific_result", "")).upper()
    if status in {"FAILED", "FAILURE", "ERROR"} or science == "FALSIFIED":
        return True
    # For fixtures that omit the explicit flag, infer improvement only for
    # the conventional lower-is-better metrics.  Unknown metrics do not
    # accidentally open an architecture route.
    baseline_metrics = baseline.get("metrics", {}) if isinstance(baseline, Mapping) else {}
    candidate_metrics = comparison.get("candidate_metrics", {})
    preferred = [
        key
        for key in set(baseline_metrics) | set(candidate_metrics)
        if any(token in str(key).lower() for token in ("score", "error", "loss", "defect", "artifact"))
    ]
    if preferred:
        numeric = [
            (baseline_metrics.get(key), candidate_metrics.get(key))
            for key in preferred
            if isinstance(baseline_metrics.get(key), (int, float))
            and not isinstance(baseline_metrics.get(key), bool)
            and isinstance(candidate_metrics.get(key), (int, float))
            and not isinstance(candidate_metrics.get(key), bool)
        ]
        if numeric:
            return not any(new < old for old, new in numeric)
    return False


def fresh_critic_packet(
    contract: Mapping[str, Any],
    result: Mapping[str, Any],
    comparison: Mapping[str, Any] | None = None,
    *,
    previous_packet_id: str | None = None,
    comparison_mode: str | CriticMode | None = None,
    representative_set: Sequence[str] | str | None = None,
    active_lanes: Sequence[str] | None = None,
    winner: str | None = None,
) -> dict[str, Any]:
    """Build a fresh, compact critic packet for the current result.

    ``NONE`` produces no critic tasks.  ``MAJOR_CHALLENGER`` produces exactly
    two task descriptors (primary and challenger), never an unbounded fan-out.
    The packet ID is content-addressed, so replaying the same event is safe and
    deterministic while a new iteration yields a new packet.
    """

    if not isinstance(contract, Mapping) or not isinstance(result, Mapping):
        raise ContractValidationError("critic packet inputs must be objects")
    stage_id = str(contract.get("stage_id", ""))
    iteration = int(result.get("iteration_index", 0))
    result_digest = sha256_json(result)
    mode_value = _enum_value(comparison_mode).upper() if comparison_mode is not None else _requested_comparison_mode(contract, result)
    if mode_value in {"MAJOR", "MAJOR_REPRESENTATION_REPLACEMENT", "MAJOR_REPLACEMENT"}:
        mode_value = CriticMode.MAJOR_CHALLENGER.value
    if mode_value not in {CriticMode.NONE.value, CriticMode.MAJOR_CHALLENGER.value}:
        raise ContractValidationError(f"unsupported comparison_mode: {mode_value!r}")
    if representative_set is None:
        representative_set = result.get("representative_set", contract.get("representative_set"))
    if mode_value == CriticMode.MAJOR_CHALLENGER.value:
        if representative_set is None:
            # Stable across iterations of the same stage/baseline, so both
            # lanes compare the same representatives instead of each inventing
            # its own sample.
            representative_set = [
                "representative-"
                + sha256_json({"stage_id": stage_id, "baseline_digest": result.get("baseline_digest")})[:12]
            ]
        reps = _normalise_string_set(representative_set)
        lanes = list(active_lanes or ["primary", "challenger"])
        if winner is not None:
            lanes = [winner]
        if any(lane not in {"primary", "challenger"} for lane in lanes):
            raise ContractValidationError("active_lanes must contain only primary and challenger")
        lanes = list(dict.fromkeys(lanes))[:2]
    else:
        reps = []
        lanes = []
    body = {
        "packet_version": "fresh_critic_packet.v1",
        "plan_id": contract.get("plan_id"),
        "stage_id": stage_id,
        "iteration_index": iteration,
        "mode": mode_value,
        "comparison_mode": mode_value,
        "representative_set": reps,
        "active_lanes": lanes,
        "winner": winner,
        "baseline_digest": result.get("baseline_digest"),
        "result_digest": result_digest,
        "previous_packet_id": previous_packet_id,
        "focus": {
            "hypothesis": contract.get("hypothesis"),
            "falsifier": contract.get("falsifier"),
            "user_visible_failure": bool(result.get("user_visible_failure", False)),
        },
    }
    packet_id = "critic-" + hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()[:16]
    tasks = []
    roles = _critic_roles(body["mode"])
    if body["active_lanes"]:
        roles = [role for role in roles if role in body["active_lanes"]]
    for role in roles[:2]:
        tasks.append(
            {
                "role": role,
                "lane": role,
                "comparison_mode": body["comparison_mode"],
                "representative_set": copy.deepcopy(body["representative_set"]),
                "scope": (
                    "independently assess evidence against the baseline and acceptance criteria"
                    if role == "primary"
                    else "attempt to falsify the primary interpretation using the same evidence"
                ),
                "allowed_paths": list(contract.get("allowed_paths", [])),
                "must_not_change_architecture": role == "primary",
            }
        )
    packet = {
        "packet_version": body["packet_version"],
        "packet_id": packet_id,
        "plan_id": body["plan_id"],
        "stage_id": stage_id,
        "iteration_index": iteration,
        "mode": body["mode"],
        "comparison_mode": body["comparison_mode"],
        "representative_set": copy.deepcopy(body["representative_set"]),
        "active_lanes": list(body["active_lanes"]),
        "winner": winner,
        "fresh": True,
        "supersedes": previous_packet_id,
        "baseline_digest": body["baseline_digest"],
        "result_digest": result_digest,
        "focus": body["focus"],
        "critic_tasks": tasks,
        "max_critic_tasks": 2,
    }
    return packet


def build_stage_context_pack(
    contract: Mapping[str, Any],
    state: Mapping[str, Any],
    *,
    latest_result: Mapping[str, Any] | None = None,
    critic_packet: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a compact stage context with at most nine resource classes."""

    if not isinstance(contract, Mapping) or not isinstance(state, Mapping):
        raise ContractValidationError("context pack inputs must be objects")
    resources: dict[str, Any] = {
        "user_plan": {
            "plan_id": contract.get("plan_id"),
            "user_visible_goal": contract.get("user_visible_goal"),
            "hypothesis": contract.get("hypothesis"),
        },
        "stage_contract": copy.deepcopy(dict(contract)),
        "baseline": copy.deepcopy(state.get("baseline")),
        "decision_history": {"items": copy.deepcopy(list(state.get("decision_history", [])))[-8:]},
        "stop_rules": {"items": copy.deepcopy(list(contract.get("stop_rules", [])))},
        "acceptance": {"items": copy.deepcopy(list(contract.get("acceptance", [])))},
        "safety": {
            "lane": "fake-codex",
            "allowed_paths": list(contract.get("allowed_paths", [])),
            "no_network": True,
            "finite_max_iterations": contract.get("max_iterations"),
        },
    }
    selected_result = latest_result if latest_result is not None else state.get("latest_result")
    if selected_result is not None:
        resources["latest_result"] = copy.deepcopy(dict(selected_result))
    selected_packet = critic_packet if critic_packet is not None else state.get("latest_critic_packet")
    if selected_packet is not None:
        resources["critic_packet"] = copy.deepcopy(dict(selected_packet))
    # The literal list and count are part of the contract and make the bound
    # easy for callers to assert without walking arbitrary transcript data.
    if len(resources) > len(RESOURCE_CLASSES) or set(resources) - set(RESOURCE_CLASSES):
        raise SupervisorError("stage context pack exceeds the nine resource-class bound")
    body = {
        "pack_version": "stage_context_pack.v1",
        "plan_id": contract.get("plan_id"),
        "stage_id": contract.get("stage_id"),
        "iteration_index": int(state.get("iteration_index", 0)),
        "resource_classes": list(resources),
        "resource_count": len(resources),
        "resources": resources,
    }
    return {
        **body,
        "digest": sha256_json(body),
    }


@dataclass
class _State:
    contract: dict[str, Any]
    status: str
    baseline: dict[str, Any]
    iteration_index: int = 0
    same_abstraction_count: int = 0
    last_abstraction_layer: str | None = None
    decision_history: list[dict[str, Any]] | None = None
    result_history: list[dict[str, Any]] | None = None
    latest_result: dict[str, Any] | None = None
    latest_critic_packet: dict[str, Any] | None = None
    pending_gate: dict[str, Any] | None = None
    review_history: list[dict[str, Any]] | None = None
    comparison_mode: str = CriticMode.NONE.value
    representative_set: list[str] | None = None
    active_lanes: list[str] | None = None
    selected_winner: str | None = None
    accepted_milestone: dict[str, Any] | None = None
    accepted_milestones: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.decision_history = list(self.decision_history or [])
        self.result_history = list(self.result_history or [])
        self.review_history = list(self.review_history or [])
        self.representative_set = list(self.representative_set or [])
        self.active_lanes = list(self.active_lanes or [])
        self.accepted_milestones = list(self.accepted_milestones or [])

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": copy.deepcopy(self.contract),
            "status": self.status,
            "baseline": copy.deepcopy(self.baseline),
            "iteration_index": self.iteration_index,
            "same_abstraction_count": self.same_abstraction_count,
            "last_abstraction_layer": self.last_abstraction_layer,
            "decision_history": copy.deepcopy(self.decision_history),
            "result_history": copy.deepcopy(self.result_history),
            "latest_result": copy.deepcopy(self.latest_result),
            "latest_critic_packet": copy.deepcopy(self.latest_critic_packet),
            "pending_gate": copy.deepcopy(self.pending_gate),
            "review_history": copy.deepcopy(self.review_history),
            "comparison_mode": self.comparison_mode,
            "representative_set": copy.deepcopy(self.representative_set),
            "active_lanes": copy.deepcopy(self.active_lanes),
            # ``winner`` is a short compatibility spelling used by workflow
            # consumers; the canonical state field is selected_winner.
            "selected_winner": self.selected_winner,
            "winner": self.selected_winner,
            "accepted_milestone": copy.deepcopy(self.accepted_milestone),
            "accepted_milestones": copy.deepcopy(self.accepted_milestones),
        }


class Supervisor:
    """One finite local stage supervisor.

    A ``Supervisor`` owns one active stage at a time.  The stage can be
    advanced only by explicitly creating a task, submitting its result,
    resolving a Human Gate, or reviewing a ready stage.  No method loops over
    iterations and no external process is started.
    """

    def __init__(self, *, architecture_threshold: int = 2, max_iterations: int | None = None) -> None:
        if not isinstance(architecture_threshold, int) or architecture_threshold < 2:
            raise ValueError("architecture_threshold must be an integer >= 2")
        self.architecture_threshold = architecture_threshold
        self.max_iterations_override = max_iterations
        self._state: _State | None = None

    @property
    def state(self) -> dict[str, Any] | None:
        return self._state.as_dict() if self._state else None

    @property
    def status(self) -> str | None:
        return self._state.status if self._state else None

    def start_stage(
        self,
        contract: Mapping[str, Any],
        baseline_metrics: Mapping[str, Any],
        baseline_artifact_refs: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Validate a contract and freeze its baseline exactly once."""

        if self._state is not None and self._state.status not in {StageStatus.ACCEPTED.value, StageStatus.REJECTED.value}:
            raise SupervisorError("a stage is already active")
        checked = validate_stage_contract(contract)
        if self.max_iterations_override is not None:
            if self.max_iterations_override < 1:
                raise ValueError("max_iterations override must be positive")
            checked["max_iterations"] = min(checked["max_iterations"], self.max_iterations_override)
        baseline = freeze_baseline(
            checked["stage_id"], baseline_metrics, baseline_artifact_refs, source="stage_start"
        )
        initial_mode = _requested_comparison_mode(checked, {})
        initial_representatives = checked.get("representative_set")
        if initial_mode == CriticMode.MAJOR_CHALLENGER.value:
            if initial_representatives is None:
                initial_representatives = [
                    "representative-"
                    + sha256_json({"stage_id": checked["stage_id"], "baseline_digest": baseline["digest"]})[:12]
                ]
            initial_representatives = _normalise_string_set(initial_representatives)
            initial_lanes = ["primary", "challenger"]
        else:
            initial_representatives = []
            initial_lanes = []
        self._state = _State(
            contract=checked,
            status=StageStatus.ACTIVE.value,
            baseline=baseline,
            comparison_mode=initial_mode,
            representative_set=initial_representatives,
            active_lanes=initial_lanes,
        )
        return {
            "event": "stage_started",
            "stage_id": checked["stage_id"],
            "plan_id": checked["plan_id"],
            "status": self._state.status,
            "baseline": copy.deepcopy(baseline),
            "baseline_frozen": True,
            "architecture_check": _architecture_checkpoint(
                "stage_start",
                "KEEP_COURSE",
                "stage start baseline frozen; keep course until evidence requires a replan",
                threshold=self.architecture_threshold,
            ),
            "comparison_mode": initial_mode,
            "representative_set": copy.deepcopy(initial_representatives),
            "active_lanes": list(initial_lanes),
        }

    def _require_state(self) -> _State:
        if self._state is None:
            raise SupervisorError("no stage has been started")
        return self._state

    def _require_status(self, *allowed: StageStatus) -> _State:
        state = self._require_state()
        allowed_values = {item.value for item in allowed}
        if state.status not in allowed_values:
            raise SupervisorError(f"stage status {state.status} does not allow this operation")
        return state

    def _activate_major_comparison(
        self,
        state: _State,
        representative_set: Sequence[str] | str | None = None,
    ) -> None:
        """Enable one bounded primary/challenger comparison for ``state``."""

        state.comparison_mode = CriticMode.MAJOR_CHALLENGER.value
        if representative_set is not None:
            requested = _normalise_string_set(representative_set)
            if state.representative_set and requested != state.representative_set:
                raise SupervisorError("a comparison cannot change its representative set after it starts")
            state.representative_set = requested
        elif not state.representative_set:
            state.representative_set = [
                "representative-"
                + sha256_json({"stage_id": state.contract["stage_id"], "baseline_digest": state.baseline["digest"]})[:12]
            ]
        if state.selected_winner:
            state.active_lanes = [state.selected_winner]
        else:
            state.active_lanes = ["primary", "challenger"]

    def start_representation_comparison(
        self,
        representative_set: Sequence[str] | str | None = None,
        *,
        representation_id: str | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        """Start a finite major-representation comparison.

        This is a local state operation only.  It creates no workers or
        external tasks; callers can use the returned lane list to construct at
        most one primary and one challenger task.
        """

        state = self._require_status(StageStatus.ACTIVE)
        if representation_id is not None:
            if representative_set is not None:
                raise ContractValidationError("provide representative_set or representation_id, not both")
            representative_set = [representation_id]
        if representative_set is None:
            representative_set = state.contract.get("representative_set")
        self._activate_major_comparison(state, representative_set)
        return {
            "event": "representation_comparison_started",
            "stage_id": state.contract["stage_id"],
            "status": state.status,
            "architecture_check": _architecture_checkpoint(
                "core_representation_replacement",
                "REPLAN",
                "core representation replacement requires a bounded primary/challenger replan",
                threshold=self.architecture_threshold,
            ),
            "comparison_mode": state.comparison_mode,
            "representative_set": copy.deepcopy(state.representative_set),
            "active_lanes": list(state.active_lanes),
            "winner": state.selected_winner,
            "rationale": rationale,
            "notification": None,
        }

    # A short alias is useful for callers that describe the same event as a
    # comparison rather than a representation replacement.
    def start_comparison(
        self,
        representative_set: Sequence[str] | str | None = None,
        *,
        representation_id: str | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        return self.start_representation_comparison(
            representative_set,
            representation_id=representation_id,
            rationale=rationale,
        )

    def select_winner(
        self,
        winner: str,
        *,
        actor: str = "local-human",
        rationale: str = "",
    ) -> dict[str, Any]:
        """Select one comparison lane and retire the other lane(s)."""

        state = self._require_status(StageStatus.ACTIVE)
        if state.comparison_mode != CriticMode.MAJOR_CHALLENGER.value:
            raise SupervisorError("no major challenger comparison is active")
        if not isinstance(winner, str) or winner not in {"primary", "challenger"}:
            raise ContractValidationError("winner must be primary or challenger")
        if winner not in (state.active_lanes or []):
            raise SupervisorError(f"comparison lane {winner!r} is not active")
        if not isinstance(actor, str) or not actor.strip():
            raise ContractValidationError("actor must be a non-empty string")
        if not isinstance(rationale, str):
            raise ContractValidationError("rationale must be a string")
        state.selected_winner = winner
        state.active_lanes = [winner]
        if state.latest_critic_packet is not None:
            # Retain the fresh packet's evidence/digest, but make the current
            # active-lane decision explicit for resumable callers.
            state.latest_critic_packet["winner"] = winner
            state.latest_critic_packet["active_lanes"] = [winner]
            state.latest_critic_packet["critic_tasks"] = [
                task
                for task in state.latest_critic_packet.get("critic_tasks", [])
                if task.get("role") == winner
            ]
        return {
            "event": "comparison_winner_selected",
            "stage_id": state.contract["stage_id"],
            "status": state.status,
            "architecture_check": _architecture_checkpoint(
                "stage_closeout",
                "KEEP_COURSE",
                "comparison winner selected; close out this stage without changing its goal",
                threshold=self.architecture_threshold,
            ),
            "comparison_mode": state.comparison_mode,
            "representative_set": copy.deepcopy(state.representative_set),
            "winner": winner,
            "active_lanes": [winner],
            "actor": actor,
            "rationale": rationale,
            "notification": None,
        }

    def select_comparison_winner(
        self,
        winner: str,
        *,
        actor: str = "local-human",
        rationale: str = "",
    ) -> dict[str, Any]:
        return self.select_winner(winner, actor=actor, rationale=rationale)

    # ``choose_winner`` is kept as a harmless ergonomic alias for a workflow
    # that uses choose/select terminology interchangeably.
    def choose_winner(
        self,
        winner: str,
        *,
        actor: str = "local-human",
        rationale: str = "",
    ) -> dict[str, Any]:
        return self.select_winner(winner, actor=actor, rationale=rationale)

    def create_scoped_task(
        self,
        objective: str | None = None,
        *,
        critic_role: str = "none",
        lane: str | None = None,
        iteration_index: int | None = None,
    ) -> dict[str, Any]:
        """Create one finite fake-Codex task scoped to the stage contract."""

        state = self._require_status(StageStatus.ACTIVE)
        next_iteration = state.iteration_index + 1 if iteration_index is None else iteration_index
        if next_iteration < 1 or next_iteration > state.contract["max_iterations"]:
            raise SupervisorError("task iteration exceeds finite max_iterations")
        role = str(critic_role).lower()
        if lane is not None:
            requested_lane = str(lane).lower()
            if role != "none" and role != requested_lane:
                raise ContractValidationError("critic_role and lane must identify the same lane")
            role = requested_lane
        if role not in {"none", "primary", "challenger"}:
            raise ContractValidationError("critic_role must be none, primary, or challenger")
        if role in {"primary", "challenger"}:
            self._activate_major_comparison(state)
            if role not in (state.active_lanes or []):
                raise SupervisorError(f"comparison lane {role!r} is no longer active")
        comparison_mode = state.comparison_mode
        representative_set = copy.deepcopy(state.representative_set)
        preflight_trigger = (
            "second_same_abstraction_repair"
            if next_iteration == 2
            and state.last_abstraction_layer == state.contract["abstraction_layer"]
            else "task_preflight"
        )
        architecture_preflight = _architecture_checkpoint(
            preflight_trigger,
            "KEEP_COURSE",
            (
                "preparing the second repair at the same abstraction; keep course until the result is measured"
                if preflight_trigger == "second_same_abstraction_repair"
                else "task preflight records the current architecture route"
            ),
            same_abstraction_count=state.same_abstraction_count,
            threshold=self.architecture_threshold,
        )
        base = {
            "schema_version": "codex_task.v1",
            "plan_id": state.contract["plan_id"],
            "stage_id": state.contract["stage_id"],
            "task_id": "task-" + sha256_json(
                {
                    "plan_id": state.contract["plan_id"],
                    "stage_id": state.contract["stage_id"],
                    "iteration_index": next_iteration,
                    "critic_role": role,
                    "comparison_mode": comparison_mode,
                    "representative_set": representative_set,
                }
            )[:16],
            "lane": "fake-codex",
            "objective": objective or state.contract["experiment"],
            "allowed_paths": list(state.contract["allowed_paths"]),
            "read_first": list(state.contract.get("read_first", [])),
            "context_refs": [
                f"stage://{state.contract['stage_id']}/baseline/{state.baseline['digest'][:12]}",
                f"stage://{state.contract['stage_id']}/context/{state.iteration_index}",
            ],
            "acceptance": list(state.contract["acceptance"]),
            "measurement_commands": list(state.contract.get("measurement_commands", [])),
            "tool_policy": {
                "network": "deny",
                "write_scope": list(state.contract["allowed_paths"]),
                "destructive_actions": "deny",
            },
            "protected_paths": list(state.contract.get("protected_paths", [])),
            "baseline_digest": state.baseline["digest"],
            "result_schema": "codex_result.schema.json",
            "stop_rules": list(state.contract["stop_rules"]),
            "iteration_index": next_iteration,
            "abstraction_layer": state.contract["abstraction_layer"],
            "critic_role": role,
            "comparison_mode": comparison_mode,
            "representative_set": representative_set,
            "architecture_check": architecture_preflight,
        }
        checked = validate_scoped_task(base)
        return checked

    def submit_result(self, task: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
        """Validate one result, compare it to baseline, and emit one decision."""

        state = self._require_status(StageStatus.ACTIVE)
        checked_task = validate_scoped_task(task)
        checked_result = validate_result_report(result, checked_task)
        result_baseline = checked_result.get("baseline_digest")
        if result_baseline is not None and result_baseline != state.baseline["digest"]:
            raise ContractValidationError("ResultReport baseline_digest does not match frozen stage baseline")
        expected_iteration = state.iteration_index + 1
        if checked_result["iteration_index"] != checked_task["iteration_index"]:
            raise ContractValidationError("ResultReport iteration_index does not match task")
        if checked_result["iteration_index"] != expected_iteration:
            raise SupervisorError(
                f"expected result for iteration {expected_iteration}, got {checked_result['iteration_index']}"
            )
        if checked_result["abstraction_layer"] != state.contract["abstraction_layer"]:
            # A planner can intentionally replan to another layer only after a
            # gate/review event; ordinary scoped tasks must stay at the stage's
            # declared layer.
            raise ContractValidationError("result abstraction_layer does not match stage contract")

        state.iteration_index = checked_result["iteration_index"]
        current_layer = checked_result["abstraction_layer"]
        comparison = compare_to_baseline(state.baseline, checked_result)
        # A major representation replacement explicitly switches the bounded
        # comparison mode.  The stage contract/goal itself remains untouched.
        if _has_major_representation_replacement(state.contract, checked_result):
            requested_representatives = checked_result.get(
                "representative_set",
                checked_result.get("representatives", state.contract.get("representative_set")),
            )
            self._activate_major_comparison(state, requested_representatives)

        no_visible_improvement = _no_user_visible_improvement(
            checked_result, comparison, state.baseline
        )
        if state.last_abstraction_layer == current_layer and no_visible_improvement:
            state.same_abstraction_count += 1
        elif current_layer != state.last_abstraction_layer:
            state.same_abstraction_count = 1 if no_visible_improvement else 0
            state.last_abstraction_layer = current_layer
        else:
            # A visible improvement breaks the *consecutive* no-improvement
            # sequence while retaining the current abstraction identity.
            state.same_abstraction_count = 0
        architecture = architecture_check(
            state.same_abstraction_count,
            no_visible_improvement,
            threshold=self.architecture_threshold,
            result_status=checked_result["status"],
            trigger=(
                "two_same_abstraction_no_improvement"
                if state.same_abstraction_count >= self.architecture_threshold and no_visible_improvement
                else "same_abstraction_check"
            ),
        )
        explicit_reason = checked_result.get("decision_reason")
        major_uncertainty = _has_major_uncertainty(checked_result)
        decision_bits = planner_decision(
            result_status=checked_result["status"],
            stage_ready=bool(checked_result.get("stage_ready", False)),
            human_gate_required=(
                bool(checked_result.get("human_gate_required", False)) or major_uncertainty
            ),
            architecture_reset=architecture["triggered"],
            scientific_result=checked_result.get("scientific_result"),
            reason=explicit_reason,
        )
        decision = {
            "schema_version": "planner_decision.v1",
            "decision_id": "decision-" + sha256_json(
                {
                    "stage_id": state.contract["stage_id"],
                    "iteration_index": state.iteration_index,
                    "task_id": checked_task["task_id"],
                    "decision": decision_bits["decision"],
                }
            )[:16],
            "plan_id": state.contract["plan_id"],
            "stage_id": state.contract["stage_id"],
            "iteration_index": state.iteration_index,
            "decision": decision_bits["decision"],
            "reason": decision_bits["reason"],
            "silent": decision_bits["silent"],
            "human_gate_required": decision_bits["human_gate_required"],
            "architecture_check": architecture,
            "architecture_reset": decision_bits["architecture_reset"],
            "task_id": checked_task["task_id"],
            "comparison_mode": state.comparison_mode,
            "representative_set": copy.deepcopy(state.representative_set),
            "active_lanes": list(state.active_lanes),
            "winner": state.selected_winner,
        }

        critic_input = {
            **checked_result,
            "baseline_digest": state.baseline["digest"],
            "comparison_mode": state.comparison_mode,
            "representative_set": copy.deepcopy(state.representative_set),
            "active_lanes": list(state.active_lanes),
        }
        packet = fresh_critic_packet(
            state.contract,
            critic_input,
            comparison,
            previous_packet_id=(state.latest_critic_packet or {}).get("packet_id"),
            comparison_mode=state.comparison_mode,
            representative_set=state.representative_set,
            active_lanes=state.active_lanes,
            winner=state.selected_winner,
        )
        state.latest_result = copy.deepcopy(checked_result)
        state.latest_critic_packet = copy.deepcopy(packet)
        state.result_history.append(copy.deepcopy(checked_result))
        state.decision_history.append(copy.deepcopy(decision))

        event: dict[str, Any] = {
            "event": "result_evaluated",
            "decision": decision,
            "comparison": comparison,
            "architecture_check": architecture,
            "critic_packet": copy.deepcopy(packet),
            "notification": None,
            "comparison_mode": state.comparison_mode,
            "representative_set": copy.deepcopy(state.representative_set),
            "active_lanes": list(state.active_lanes),
            "winner": state.selected_winner,
        }
        selected = DecisionType(decision["decision"])
        if selected == DecisionType.CONTINUE or selected == DecisionType.REPLAN:
            state.status = StageStatus.ACTIVE.value
            # ``silent`` is explicit so callers do not need to infer whether an
            # ordinary result should wake a user.
            event["notification"] = None
        elif selected == DecisionType.STAGE_READY:
            state.status = StageStatus.STAGE_READY.value
            review_artifacts = copy.deepcopy(checked_result.get("review_artifacts", []))
            # validate_result_report enforces this for stage_ready=true.  Keep
            # the check here as a defensive invariant if validation evolves.
            if not review_artifacts:
                raise ContractValidationError("STAGE_READY requires review_artifacts")
            event["notification"] = {
                "type": "STAGE_READY",
                "stage_id": state.contract["stage_id"],
                "iteration_index": state.iteration_index,
                "review_artifacts": review_artifacts,
                "reason": decision["reason"],
                "required_action": "accept_stage_or_reject_stage",
            }
        elif selected == DecisionType.HUMAN_GATE:
            state.status = StageStatus.HUMAN_GATE.value
            state.pending_gate = {
                "gate_id": "gate-" + sha256_json(decision)[:16],
                "stage_id": state.contract["stage_id"],
                "iteration_index": state.iteration_index,
                "reason": decision["reason"],
                "architecture_reset": bool(decision["architecture_reset"]),
                "status": "PENDING",
            }
            event["notification"] = {
                "type": "HUMAN_GATE",
                "gate_id": state.pending_gate["gate_id"],
                "stage_id": state.contract["stage_id"],
                "iteration_index": state.iteration_index,
                "question": "Approve the proposed route or choose a different research route?",
                "reason": decision["reason"],
                "required_action": "resolve_human_gate",
            }
        elif selected == DecisionType.BLOCKED:
            state.status = StageStatus.BLOCKED.value
            event["notification"] = {
                "type": "BLOCKED",
                "stage_id": state.contract["stage_id"],
                "iteration_index": state.iteration_index,
                "reason": decision["reason"],
            }
        event["status"] = state.status
        event["context_pack"] = build_stage_context_pack(
            state.contract, state.as_dict(), latest_result=checked_result, critic_packet=packet
        )
        return event

    def resolve_human_gate(
        self,
        *,
        approved: bool,
        actor: str = "local-human",
        rationale: str = "",
    ) -> dict[str, Any]:
        """Resolve a pending gate; approval resumes, rejection blocks the route."""

        state = self._require_status(StageStatus.HUMAN_GATE)
        if not isinstance(approved, bool):
            raise ContractValidationError("approved must be boolean")
        if not isinstance(actor, str) or not actor.strip():
            raise ContractValidationError("actor must be a non-empty string")
        if not isinstance(rationale, str):
            raise ContractValidationError("rationale must be a string")
        gate = copy.deepcopy(state.pending_gate)
        gate.update({"status": "APPROVED" if approved else "REJECTED", "actor": actor, "rationale": rationale})
        state.pending_gate = None
        state.status = StageStatus.ACTIVE.value if approved else StageStatus.BLOCKED.value
        return {
            "event": "human_gate_resolved",
            "stage_id": state.contract["stage_id"],
            "status": state.status,
            "gate": gate,
            "notification": None,
        }

    def review_stage(
        self,
        outcome: str,
        *,
        reviewer: str = "local-human",
        rationale: str = "",
    ) -> dict[str, Any]:
        """Accept or reject a stage-ready result."""

        state = self._require_status(StageStatus.STAGE_READY)
        normalized = str(outcome).upper()
        if normalized not in {"ACCEPT", "REJECT"}:
            raise ContractValidationError("stage review outcome must be ACCEPT or REJECT")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ContractValidationError("reviewer must be a non-empty string")
        if not isinstance(rationale, str):
            raise ContractValidationError("rationale must be a string")
        comparison = compare_to_baseline(state.baseline, state.latest_result or {})
        closeout_architecture = _architecture_checkpoint(
            "stage_closeout",
            "KEEP_COURSE",
            "stage closeout review records the architecture check before human accept/reject",
            same_abstraction_count=state.same_abstraction_count,
            threshold=self.architecture_threshold,
        )
        latest_result = state.latest_result or {}
        review = attach_method_evidence_policy({
            "schema_version": "stage_review.v1",
            "review_id": "review-" + sha256_json(
                {
                    "stage_id": state.contract["stage_id"],
                    "iteration_index": state.iteration_index,
                    "outcome": normalized,
                }
            )[:16],
            "plan_id": state.contract["plan_id"],
            "stage_id": state.contract["stage_id"],
            "iteration_index": state.iteration_index,
            "outcome": normalized,
            "reviewer": reviewer,
            "rationale": rationale or ("stage accepted" if normalized == "ACCEPT" else "stage rejected; continue investigation"),
            "baseline_comparison": comparison,
            "architecture_check": copy.deepcopy(closeout_architecture),
            "review_artifacts": copy.deepcopy(latest_result.get("review_artifacts", [])),
            "problems_discovered": copy.deepcopy(latest_result.get("problems_discovered", [])),
            "human_decision": True,
        })
        validate_against_schema(review, "stage_review")
        state.review_history.append(copy.deepcopy(review))
        if normalized == "ACCEPT":
            state.status = StageStatus.ACCEPTED.value
            milestone = {
                "milestone_id": "milestone-" + sha256_json(
                    {
                        "stage_id": state.contract["stage_id"],
                        "iteration_index": state.iteration_index,
                        "review_id": review["review_id"],
                    }
                )[:16],
                "plan_id": state.contract["plan_id"],
                "stage_id": state.contract["stage_id"],
                "iteration_index": state.iteration_index,
                "baseline_digest": state.baseline["digest"],
                "result_digest": sha256_json(latest_result),
                "review_id": review["review_id"],
                "review_artifacts": copy.deepcopy(latest_result.get("review_artifacts", [])),
                "accepted": True,
                "status": "ACCEPTED",
            }
            state.accepted_milestone = copy.deepcopy(milestone)
            state.accepted_milestones.append(copy.deepcopy(milestone))
        else:
            state.status = StageStatus.ACTIVE.value
            milestone = None
        review["accepted_milestone"] = copy.deepcopy(milestone)
        review["milestone"] = copy.deepcopy(milestone)
        # Include the milestone in the durable review record as well as the
        # event envelope; both are useful to a resumed local caller.
        state.review_history[-1] = copy.deepcopy(review)
        event = {
            "event": "stage_reviewed",
            "status": state.status,
            "review": review,
            "notification": None,
            "stage_id": state.contract["stage_id"],
            "architecture_check": closeout_architecture,
            "accepted_milestone": copy.deepcopy(milestone),
            # Keep a short alias for clients that model a review outcome as a
            # generic milestone event.
            "milestone": copy.deepcopy(milestone),
        }
        return event

    def accept_stage(self, *, reviewer: str = "local-human", rationale: str = "") -> dict[str, Any]:
        return self.review_stage("ACCEPT", reviewer=reviewer, rationale=rationale)

    def reject_stage(self, *, reviewer: str = "local-human", rationale: str = "") -> dict[str, Any]:
        return self.review_stage("REJECT", reviewer=reviewer, rationale=rationale)

    def context_pack(self) -> dict[str, Any]:
        state = self._require_state()
        return build_stage_context_pack(state.contract, state.as_dict())


__all__ = [
    "ComparisonMode",
    "CriticMode",
    "DecisionType",
    "RESOURCE_CLASSES",
    "StageStatus",
    "Supervisor",
    "SupervisorError",
    "architecture_check",
    "build_stage_context_pack",
    "fresh_critic_packet",
    "planner_decision",
]
