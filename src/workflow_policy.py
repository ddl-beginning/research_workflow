"""Small, independent policy contracts for the GPT-first workflow.

The existing Stage, bridge, and human-artifact modules each own their own
runtime concerns.  This module is deliberately a small policy boundary that
can be used by a caller before those components are invoked.  It contains no
transport code, filesystem writes, worker loop, or implicit state transition.

The important default is fail-closed: a proposed :class:`ActionMap` is only
executable after deterministic validation *and* the explicit execution flag
has been enabled.  A GPT suggestion is data; it is never an execution trigger
or a reason to spend another consultation budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


class WorkflowPolicyError(ValueError):
    """Base error raised when a workflow policy contract is violated."""


class ActionMapError(WorkflowPolicyError):
    """Raised for an invalid or not-yet-authorized action map."""


class ConsultationBudgetError(WorkflowPolicyError):
    """Raised when a consultation would exceed its bounded budget."""


class ConsultationMode(str, Enum):
    """The only consultation modes available to the policy."""

    FRESH = "FRESH"
    NORMAL = "NORMAL"


class TransitionPolicyError(WorkflowPolicyError):
    """Raised for an illegal or unapproved lifecycle transition."""


class ArtifactPolicyError(WorkflowPolicyError):
    """Raised when a path is outside the fixed human-artifact contract."""


class NodeOwner(str, Enum):
    """The owners allowed to reason about or act on a workflow node.

    ``GPT`` supplies bounded recommendations.  ``CODEX`` (including its Luna
    execution lane) interprets and performs local work.  ``DETERMINISTIC``
    represents pure local checks and bookkeeping, while ``HUMAN`` owns
    explicit approvals and decisions at review gates.
    """

    GPT = "GPT"
    CODEX = "CODEX"
    DETERMINISTIC = "DETERMINISTIC"
    HUMAN = "HUMAN"

    # Readable aliases for callers that name the local execution lane
    # explicitly.  They do not add additional owners to iteration over this
    # enum.
    CODEX_LUNA = CODEX
    LUNA = CODEX
    LOCAL = DETERMINISTIC
    SYSTEM = DETERMINISTIC

    @classmethod
    def coerce(cls, value: "NodeOwner | str") -> "NodeOwner":
        """Normalize enum instances and case-insensitive wire values."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            raise WorkflowPolicyError("node owner must be a NodeOwner or string")
        normalized = value.strip().upper().replace("-", "_").replace(" ", "_")
        aliases = {
            "CHATGPT": cls.GPT,
            "CODEX_LUNA": cls.CODEX,
            "LOCAL_EXECUTOR": cls.CODEX,
            "RULES": cls.DETERMINISTIC,
            "USER": cls.HUMAN,
        }
        if normalized in aliases:
            return aliases[normalized]
        try:
            return cls[normalized]
        except KeyError as exc:
            raise WorkflowPolicyError(f"unknown node owner: {value!r}") from exc


@dataclass(frozen=True)
class ActionSpec:
    """Normalized description of one allowlisted action."""

    owner: NodeOwner
    requires_human: bool = False
    rationale: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", NodeOwner.coerce(self.owner))
        if not isinstance(self.requires_human, bool):
            raise ActionMapError("requires_human must be boolean")
        if not isinstance(self.rationale, str):
            raise ActionMapError("action rationale must be a string")
        if len(self.rationale) > 2000 or "\x00" in self.rationale:
            raise ActionMapError("action rationale is too long or contains NUL")


# The allowlist is intentionally small.  These names describe bounded local
# workflow operations and never include arbitrary shell, network, deletion,
# publishing, or deployment actions.
ACTION_ALLOWLIST = frozenset(
    {
        "inspect_evidence",
        "run_local_checks",
        "validate_contract",
        "prepare_action_map",
        "record_decision",
        "write_artifacts",
        "write_human_artifacts",
        "request_consultation",
        "request_fresh_consultation",
        "transition_stage",
        "enter_human_gate",
        "start_stage",
        "stop_stage",
    }
)


def _normalize_action_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ActionMapError("action name must be a string")
    name = value.strip().lower().replace("-", "_").replace(" ", "_")
    if not name or len(name) > 128 or "\x00" in name:
        raise ActionMapError("action name is empty, too long, or contains NUL")
    return name


def _normalize_action_spec(value: Any) -> ActionSpec:
    if isinstance(value, ActionSpec):
        return value
    if isinstance(value, (NodeOwner, str)):
        return ActionSpec(NodeOwner.coerce(value))
    if isinstance(value, Mapping):
        unknown = set(value) - {"owner", "requires_human", "rationale"}
        if unknown:
            raise ActionMapError(f"unknown ActionSpec field(s): {sorted(map(str, unknown))!r}")
        if "owner" not in value:
            raise ActionMapError("ActionSpec requires owner")
        return ActionSpec(
            owner=NodeOwner.coerce(value["owner"]),
            requires_human=value.get("requires_human", False),
            rationale=value.get("rationale", ""),
        )
    raise ActionMapError("action specification must name an owner or be an object")


@dataclass
class ActionMap:
    """A bounded, advisory-to-executable action mapping.

    ``execute`` intentionally defaults to ``False``.  ``validate()`` checks
    the action names, owners, and cross-field rules but does not execute
    anything.  ``dispatch()`` remains blocked until validation has succeeded,
    ``execute`` is true, and every action requiring a human gate has received
    explicit approval.

    ``actions`` may be a mapping of action name to :class:`NodeOwner`,
    :class:`ActionSpec`, or a small object with ``owner``,
    ``requires_human``, and ``rationale`` fields.  A sequence of names is
    accepted as a convenience and assigns those actions to ``CODEX``.
    """

    actions: Mapping[str, Any] | Sequence[str] = field(default_factory=dict)
    execute: bool = False
    owner: NodeOwner | str = NodeOwner.GPT
    human_approved: bool = False
    action: str | None = None
    # ``source_owner`` is a compatibility spelling for callers that distinguish
    # the author of the map from the owner field used on the wire.
    source_owner: NodeOwner | str | None = None
    validated: bool = field(default=False, init=False)
    _normalized_actions: dict[str, ActionSpec] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.execute, bool):
            raise ActionMapError("execute must be boolean")
        if not isinstance(self.human_approved, bool):
            raise ActionMapError("human_approved must be boolean")
        if self.source_owner is not None:
            self.owner = self.source_owner
        self.owner = NodeOwner.coerce(self.owner)
        if isinstance(self.actions, Mapping):
            raw_actions = dict(self.actions)
        elif isinstance(self.actions, Sequence) and not isinstance(self.actions, (str, bytes, bytearray)):
            raw_actions: dict[str, Any] = {}
            for item in self.actions:
                key = str(item)
                if key in raw_actions:
                    raise ActionMapError(f"duplicate action: {key!r}")
                raw_actions[key] = NodeOwner.CODEX
        else:
            raise ActionMapError("actions must be a mapping or sequence of action names")
        if self.action is not None:
            if not isinstance(self.action, str) or not self.action.strip():
                raise ActionMapError("action must be a non-empty string when supplied")
            if raw_actions and self.action not in raw_actions:
                raise ActionMapError("action conflicts with the supplied actions mapping")
            raw_actions.setdefault(self.action, NodeOwner.CODEX)
        self.actions = raw_actions

    @property
    def action_specs(self) -> Mapping[str, ActionSpec]:
        """Return a detached view of normalized action specifications."""

        return dict(self._normalized_actions)

    def validate(self) -> "ActionMap":
        """Validate this map and return it; no execution is implicit."""

        if not self.actions:
            raise ActionMapError("ActionMap must contain at least one action")
        normalized: dict[str, ActionSpec] = {}
        for raw_name, raw_spec in self.actions.items():
            name = _normalize_action_name(raw_name)
            if name not in ACTION_ALLOWLIST:
                raise ActionMapError(f"action is not allowlisted: {raw_name!r}")
            if name in normalized:
                raise ActionMapError(f"duplicate action after normalization: {raw_name!r}")
            spec = _normalize_action_spec(raw_spec)
            # GPT can recommend an operation but cannot own a local
            # consultation or execution action.
            if spec.owner is NodeOwner.GPT and name in {
                "request_consultation",
                "request_fresh_consultation",
                "write_artifacts",
                "write_human_artifacts",
                "transition_stage",
                "start_stage",
                "stop_stage",
            }:
                raise ActionMapError(f"GPT cannot directly own local action: {name}")
            normalized[name] = spec
        if self.execute and not self.validated:
            # An explicit execute request is retained, but remains unusable
            # until this validation completes.  This makes callers able to
            # construct a proposed map in one step while preserving the gate.
            pass
        self._normalized_actions = normalized
        self.validated = True
        return self

    def approve(self, *, actor: str = "human", rationale: str = "") -> "ActionMap":
        """Record the explicit human approval required by gated actions."""

        if not self.validated:
            raise ActionMapError("ActionMap must be validated before human approval")
        if not isinstance(actor, str) or not actor.strip():
            raise ActionMapError("human approval actor must be non-empty")
        if not isinstance(rationale, str) or len(rationale) > 2000 or "\x00" in rationale:
            raise ActionMapError("human approval rationale is invalid")
        self.human_approved = True
        self.execute = True
        return self

    authorize = approve

    def enable_execution(self) -> "ActionMap":
        """Enable execution after validation when no human gate is pending."""

        if not self.validated:
            raise ActionMapError("ActionMap must be validated before execution can be enabled")
        if any(spec.requires_human for spec in self._normalized_actions.values()) and not self.human_approved:
            raise ActionMapError("human approval is required before this ActionMap can execute")
        self.execute = True
        return self

    enable = enable_execution

    def can_execute(self) -> bool:
        """Return whether dispatch is currently permitted."""

        if not self.validated or not self.execute:
            return False
        return self.human_approved or not any(
            spec.requires_human for spec in self._normalized_actions.values()
        )

    @property
    def executable(self) -> bool:
        """Property spelling for callers that prefer a boolean check."""

        return self.can_execute()

    def dispatch(self, runner: Callable[[str, ActionSpec], Any]) -> list[Any]:
        """Dispatch each validated action through an injected local runner.

        The injected runner is intentionally the only execution mechanism; no
        subprocess, network, or filesystem operation is performed here.
        """

        if not callable(runner):
            raise ActionMapError("runner must be callable")
        if not self.can_execute():
            raise ActionMapError("ActionMap is not validated and explicitly authorized for execution")
        return [runner(name, spec) for name, spec in self._normalized_actions.items()]

    run = dispatch

    def to_dict(self) -> dict[str, Any]:
        """Return a bounded, JSON-friendly contract snapshot."""

        return {
            "owner": self.owner.value,
            "execute": self.execute,
            "validated": self.validated,
            "human_approved": self.human_approved,
            "actions": {
                name: {
                    "owner": spec.owner.value,
                    "requires_human": spec.requires_human,
                    "rationale": spec.rationale,
                }
                for name, spec in self._normalized_actions.items()
            },
        }


@dataclass
class ConsultationBudget:
    """Fixed low-frequency consultation budget.

    The defaults are the contract: at most one ``FRESH`` request, one
    ``NORMAL`` request, two total requests, no retries, and no request may be
    triggered by a GPT recommendation.  Lower limits may be supplied for a
    narrower run; raising a limit above the contract is rejected.
    """

    fresh_limit: int = 1
    normal_limit: int = 1
    total_limit: int = 2
    retry_limit: int = 0
    gpt_triggered_limit: int = 0
    fresh_count: int = 0
    normal_count: int = 0
    total_count: int = 0
    retry_count: int = 0
    gpt_triggered_count: int = 0

    MAX_FRESH = 1
    MAX_NORMAL = 1
    MAX_TOTAL = 2
    MAX_RETRY = 0
    MAX_GPT_TRIGGERED = 0

    def __post_init__(self) -> None:
        for field_name, value, maximum in (
            ("fresh_limit", self.fresh_limit, self.MAX_FRESH),
            ("normal_limit", self.normal_limit, self.MAX_NORMAL),
            ("total_limit", self.total_limit, self.MAX_TOTAL),
            ("retry_limit", self.retry_limit, self.MAX_RETRY),
            ("gpt_triggered_limit", self.gpt_triggered_limit, self.MAX_GPT_TRIGGERED),
        ):
            self._validate_counter(field_name, value, maximum=maximum)
        for field_name in (
            "fresh_count",
            "normal_count",
            "total_count",
            "retry_count",
            "gpt_triggered_count",
        ):
            self._validate_counter(field_name, getattr(self, field_name))
        if self.fresh_count > self.fresh_limit:
            raise ConsultationBudgetError("fresh_count exceeds fresh_limit")
        if self.normal_count > self.normal_limit:
            raise ConsultationBudgetError("normal_count exceeds normal_limit")
        if self.total_count > self.total_limit:
            raise ConsultationBudgetError("total_count exceeds total_limit")
        if self.retry_count > self.retry_limit:
            raise ConsultationBudgetError("retry_count exceeds retry_limit")
        if self.gpt_triggered_count > self.gpt_triggered_limit:
            raise ConsultationBudgetError("gpt_triggered_count exceeds gpt_triggered_limit")
        if self.total_count < self.fresh_count + self.normal_count:
            raise ConsultationBudgetError("total_count cannot be below mode request count")

    @staticmethod
    def _validate_counter(field_name: str, value: Any, *, maximum: int | None = None) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConsultationBudgetError(f"{field_name} must be a non-negative integer")
        if maximum is not None and value > maximum:
            raise ConsultationBudgetError(f"{field_name} cannot exceed the fixed policy maximum {maximum}")

    @property
    def fresh(self) -> int:
        """Configured FRESH limit (the fixed contract value is 1)."""

        return self.fresh_limit

    @property
    def fresh_requests(self) -> int:
        return self.fresh_count

    @property
    def normal(self) -> int:
        return self.normal_limit

    @property
    def normal_requests(self) -> int:
        return self.normal_count

    @property
    def total(self) -> int:
        return self.total_limit

    @property
    def requests(self) -> int:
        return self.total_count

    @property
    def retry(self) -> int:
        return self.retry_limit

    @property
    def retries(self) -> int:
        return self.retry_count

    @property
    def gpt_triggered(self) -> int:
        return self.gpt_triggered_limit

    @property
    def gpt_triggered_requests(self) -> int:
        return self.gpt_triggered_count

    @staticmethod
    def _mode(mode: Any) -> str:
        candidate = mode.value if isinstance(mode, Enum) else mode
        if not isinstance(candidate, str):
            raise ConsultationBudgetError("consultation mode must be FRESH or NORMAL")
        normalized = candidate.strip().upper()
        if normalized not in {"FRESH", "NORMAL"}:
            raise ConsultationBudgetError("consultation mode must be FRESH or NORMAL")
        return normalized

    def can_request(
        self,
        mode: Any,
        *,
        retry: bool = False,
        triggered_by_gpt: bool = False,
        gpt_triggered: bool | None = None,
        trigger: NodeOwner | str | None = None,
    ) -> bool:
        """Check a request without consuming budget or mutating state."""

        try:
            normalized = self._mode(mode)
            if not isinstance(retry, bool) or not isinstance(triggered_by_gpt, bool):
                return False
            if trigger is not None:
                if NodeOwner.coerce(trigger) is NodeOwner.GPT:
                    triggered_by_gpt = True
            if gpt_triggered is not None:
                if not isinstance(gpt_triggered, bool):
                    return False
                if triggered_by_gpt and triggered_by_gpt != gpt_triggered:
                    return False
                triggered_by_gpt = gpt_triggered
            if retry and self.retry_count >= self.retry_limit:
                return False
            if triggered_by_gpt and self.gpt_triggered_count >= self.gpt_triggered_limit:
                return False
            if self.total_count >= self.total_limit:
                return False
            if normalized == "FRESH" and self.fresh_count >= self.fresh_limit:
                return False
            if normalized == "NORMAL" and self.normal_count >= self.normal_limit:
                return False
            return True
        except ConsultationBudgetError:
            return False

    def request(
        self,
        mode: Any,
        *,
        retry: bool = False,
        triggered_by_gpt: bool = False,
        gpt_triggered: bool | None = None,
        trigger: NodeOwner | str | None = None,
    ) -> dict[str, Any]:
        """Consume one request if all fixed budget gates pass."""

        normalized = self._mode(mode)
        trigger_is_gpt = False
        if trigger is not None:
            try:
                trigger_is_gpt = NodeOwner.coerce(trigger) is NodeOwner.GPT
            except WorkflowPolicyError as exc:
                raise ConsultationBudgetError("trigger must be a known NodeOwner") from exc
        if not self.can_request(
            normalized,
            retry=retry,
            triggered_by_gpt=triggered_by_gpt,
            gpt_triggered=gpt_triggered,
            trigger=trigger,
        ):
            reason = "consultation budget exceeded"
            if retry:
                reason = "retry is disabled by policy"
            elif triggered_by_gpt or gpt_triggered or trigger_is_gpt:
                reason = "GPT-triggered consultation is disabled by policy"
            raise ConsultationBudgetError(reason)
        if normalized == "FRESH":
            self.fresh_count += 1
        else:
            self.normal_count += 1
        self.total_count += 1
        if retry:
            self.retry_count += 1
        if triggered_by_gpt or gpt_triggered or trigger_is_gpt:
            self.gpt_triggered_count += 1
        return {
            "mode": normalized,
            "retry": retry,
            "triggered_by_gpt": bool(triggered_by_gpt or gpt_triggered),
            "fresh_count": self.fresh_count,
            "normal_count": self.normal_count,
            "total_count": self.total_count,
        }

    consume = request
    reserve = request
    record_request = request

    def to_dict(self) -> dict[str, int]:
        return {
            "fresh_limit": self.fresh_limit,
            "normal_limit": self.normal_limit,
            "total_limit": self.total_limit,
            "retry_limit": self.retry_limit,
            "gpt_triggered_limit": self.gpt_triggered_limit,
            "fresh_count": self.fresh_count,
            "normal_count": self.normal_count,
            "total_count": self.total_count,
            "retry_count": self.retry_count,
            "gpt_triggered_count": self.gpt_triggered_count,
        }


class TransitionState(str, Enum):
    """Named states understood by :class:`TransitionPolicy`."""

    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    STAGE_READY = "STAGE_READY"
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    STOPPED = "STOPPED"
    HUMAN_GATE = "HUMAN_GATE"


WorkflowState = TransitionState


class TransitionPolicy:
    """Finite transition and human-gate policy for one workflow."""

    ALLOWED_TRANSITIONS = frozenset(
        {
            ("PLANNED", "ACTIVE"),
            ("ACTIVE", "STAGE_READY"),
            ("ACTIVE", "BLOCKED"),
            ("ACTIVE", "HUMAN_GATE"),
            ("ACTIVE", "STOPPED"),
            ("STAGE_READY", "APPROVED"),
            ("STAGE_READY", "ACTIVE"),
            ("STAGE_READY", "STOPPED"),
            ("HUMAN_GATE", "ACTIVE"),
            ("HUMAN_GATE", "BLOCKED"),
            ("HUMAN_GATE", "STOPPED"),
            ("BLOCKED", "STOPPED"),
        }
    )

    # These are the transitions where a model or deterministic planner must
    # stop and obtain an explicit human decision.
    HUMAN_GATES = frozenset(
        {
            ("PLANNED", "ACTIVE"),
            ("STAGE_READY", "APPROVED"),
            ("STAGE_READY", "ACTIVE"),
            ("STAGE_READY", "STOPPED"),
            ("HUMAN_GATE", "ACTIVE"),
            ("HUMAN_GATE", "BLOCKED"),
            ("HUMAN_GATE", "STOPPED"),
            ("ACTIVE", "STOPPED"),
        }
    )

    @staticmethod
    def _state(value: Any) -> str:
        candidate = value.value if isinstance(value, Enum) else value
        if not isinstance(candidate, str):
            raise TransitionPolicyError("workflow state must be a string or TransitionState")
        normalized = candidate.strip().upper().replace("-", "_").replace(" ", "_")
        if normalized not in {item.value for item in TransitionState}:
            raise TransitionPolicyError(f"unknown workflow state: {value!r}")
        return normalized

    def requires_human(self, current: Any, target: Any) -> bool:
        pair = (self._state(current), self._state(target))
        if pair not in self.ALLOWED_TRANSITIONS:
            raise TransitionPolicyError(f"illegal workflow transition: {pair[0]} -> {pair[1]}")
        return pair in self.HUMAN_GATES

    def can_transition(
        self,
        current: Any,
        target: Any,
        *,
        human_approved: bool = False,
        approved: bool | None = None,
    ) -> bool:
        try:
            pair = (self._state(current), self._state(target))
            if pair not in self.ALLOWED_TRANSITIONS:
                return False
            if approved is not None:
                if not isinstance(approved, bool):
                    return False
                human_approved = approved
            return not (pair in self.HUMAN_GATES and human_approved is not True)
        except TransitionPolicyError:
            return False

    def validate_transition(
        self,
        current: Any,
        target: Any,
        *,
        human_approved: bool = False,
        approved: bool | None = None,
        actor: str | None = None,
    ) -> tuple[str, str]:
        """Validate one transition and return normalized state names."""

        source = self._state(current)
        destination = self._state(target)
        pair = (source, destination)
        if pair not in self.ALLOWED_TRANSITIONS:
            raise TransitionPolicyError(f"illegal workflow transition: {source} -> {destination}")
        if approved is not None:
            if not isinstance(approved, bool):
                raise TransitionPolicyError("approved must be boolean")
            human_approved = approved
        if pair in self.HUMAN_GATES and human_approved is not True:
            raise TransitionPolicyError(f"human approval required for transition: {source} -> {destination}")
        if pair in self.HUMAN_GATES and actor is not None:
            if not isinstance(actor, str) or not actor.strip():
                raise TransitionPolicyError("human-gated transition requires a non-empty actor")
        return pair

    check = validate_transition
    assert_transition = validate_transition
    validate = validate_transition

    @property
    def human_gates(self) -> frozenset[tuple[str, str]]:
        return self.HUMAN_GATES

    @property
    def allowed_transitions(self) -> frozenset[tuple[str, str]]:
        return self.ALLOWED_TRANSITIONS


CANONICAL_HUMAN_ARTIFACTS = (
    "STAGE_EXECUTION_PLAN.md",
    "RESEARCH_DECISION_LOG.md",
    "HUMAN_REVIEW.md",
)

FRESH_LIMIT = 1
NORMAL_LIMIT = 1
TOTAL_CONSULTATION_LIMIT = 2
RETRY_LIMIT = 0
GPT_TRIGGERED_LIMIT = 0


class ArtifactPolicy:
    """Fixed root-level policy for the three human-review artifacts."""

    FILES = CANONICAL_HUMAN_ARTIFACTS
    FIXED_FILES = CANONICAL_HUMAN_ARTIFACTS
    CANONICAL_FILES = CANONICAL_HUMAN_ARTIFACTS

    @property
    def fixed_files(self) -> tuple[str, ...]:
        return self.FILES

    @staticmethod
    def _name(value: Any) -> str:
        if isinstance(value, Path):
            if value.is_absolute():
                raise ArtifactPolicyError("artifact path must be relative to the workflow root")
            raw = value.as_posix()
        elif isinstance(value, str):
            raw = value.replace("\\", "/")
        else:
            raise ArtifactPolicyError("artifact name must be a string or Path")
        if not raw or "\x00" in raw or raw.startswith("/"):
            raise ArtifactPolicyError("artifact name is empty, absolute, or contains NUL")
        parts = raw.split("/")
        if any(part in {"", ".", ".."} for part in parts) or len(parts) != 1:
            raise ArtifactPolicyError("artifact path must be one fixed root-level filename")
        return raw

    def allows(self, value: Any) -> bool:
        try:
            return self._name(value) in self.FILES
        except ArtifactPolicyError:
            return False

    accepts = allows
    is_allowed = allows

    def validate(self, values: Iterable[Any] | Mapping[str, Any]) -> tuple[str, ...]:
        """Validate a complete fixed artifact set and return canonical order."""

        raw_values = list(values.keys()) if isinstance(values, Mapping) else list(values)
        names = [self._name(value) for value in raw_values]
        if len(names) != len(set(names)):
            raise ArtifactPolicyError("artifact names must be unique")
        unknown = sorted(set(names) - set(self.FILES))
        if unknown:
            raise ArtifactPolicyError(f"artifact is outside fixed policy: {unknown!r}")
        missing = [name for name in self.FILES if name not in names]
        if missing:
            raise ArtifactPolicyError(f"fixed artifact set is incomplete: {missing!r}")
        return self.FILES

    validate_names = validate

    def paths(self, root: str | Path) -> dict[str, Path]:
        """Return fixed root-level paths without creating or writing files."""

        root_path = Path(root).expanduser().resolve()
        return {name: root_path / name for name in self.FILES}


@dataclass
class WorkflowPolicy:
    """Convenience bundle for callers that want one policy object."""

    action_map: ActionMap = field(default_factory=ActionMap)
    consultation_budget: ConsultationBudget = field(default_factory=ConsultationBudget)
    transition: TransitionPolicy = field(default_factory=TransitionPolicy)
    artifacts: ArtifactPolicy = field(default_factory=ArtifactPolicy)


def run_policy_self_test() -> dict[str, str]:
    """Run the small deterministic policy falsification checks.

    This helper has no external side effects and is intentionally equivalent
    to the focused tests in ``tests/test_workflow_policy.py``.
    """

    actions = ActionMap({"run_local_checks": NodeOwner.CODEX}, execute=True)
    if actions.can_execute():
        raise AssertionError("unvalidated ActionMap became executable")
    actions.validate()
    if not actions.can_execute():
        raise AssertionError("validated explicit ActionMap should be executable")

    budget = ConsultationBudget()
    budget.request("FRESH")
    try:
        budget.request("NORMAL", triggered_by_gpt=True)
    except ConsultationBudgetError:
        pass
    else:
        raise AssertionError("GPT-triggered follow-up escaped the zero budget")

    transitions = TransitionPolicy()
    try:
        transitions.validate_transition("APPROVED", "ACTIVE")
    except TransitionPolicyError:
        pass
    else:
        raise AssertionError("illegal transition was accepted")

    ArtifactPolicy().validate(CANONICAL_HUMAN_ARTIFACTS)
    return {
        "action_map": "PASS",
        "consultation_budget": "PASS",
        "transition_policy": "PASS",
        "artifact_policy": "PASS",
    }


__all__ = [
    "ACTION_ALLOWLIST",
    "ActionMap",
    "ActionMapError",
    "ActionSpec",
    "ArtifactPolicy",
    "ArtifactPolicyError",
    "CANONICAL_HUMAN_ARTIFACTS",
    "ConsultationBudget",
    "ConsultationBudgetError",
    "ConsultationMode",
    "FRESH_LIMIT",
    "GPT_TRIGGERED_LIMIT",
    "NORMAL_LIMIT",
    "NodeOwner",
    "RETRY_LIMIT",
    "TOTAL_CONSULTATION_LIMIT",
    "TransitionPolicy",
    "TransitionPolicyError",
    "TransitionState",
    "WorkflowPolicy",
    "WorkflowPolicyError",
    "WorkflowState",
    "run_policy_self_test",
]
