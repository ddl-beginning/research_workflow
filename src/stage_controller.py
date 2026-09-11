"""Thin, persistent Stage Controller built on the local supervisor PoC.

The original :mod:`src.supervisor` remains the Step 1 compatibility API.  This
module supplies the product-facing Stage lifecycle required by later steps.
It deliberately does not execute Codex, call ChatGPT, or implement a worker
loop.  Executor and consultation methods are request gates only; a caller must
perform the external work and explicitly submit its bounded result.

    The controller has exactly six Stage states.  Iteration-level planner values
(``CONTINUE``, ``REPLAN``, ``STAGE_READY``, ``HUMAN_GATE`` and ``BLOCKED``) are
recorded as events/decisions and never become extra Stage states.  State can be
kept in memory or persisted to an explicit local JSON path.  Persisted state
contains only contracts, bounded bookkeeping, digests, and an event log; it
does not contain prompts, browser state, cookies, tokens, or raw GPT replies.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import (
    ContractValidationError,
    _ensure_relative_path,
    canonical_json,
    sha256_json,
    validate_against_schema,
)


class StageControllerError(RuntimeError):
    """Raised when a Stage operation is not legal in the current state."""


class StageState(str, Enum):
    """The only persisted Stage lifecycle states."""

    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    STAGE_READY = "STAGE_READY"
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    STOPPED = "STOPPED"


# A readable alias for callers that use ``StageStatus`` terminology.  The
# legacy ``src.supervisor.StageStatus`` is intentionally not changed.
ControllerStageStatus = StageState
# Some clients use the shorter legacy spelling when importing the new
# controller module.  Keep the old ``src.supervisor.StageStatus`` untouched.
StageStatus = StageState


class LaneMode(str, Enum):
    PRIMARY = "PRIMARY"
    MAJOR_CHALLENGER = "MAJOR_CHALLENGER"


STAGE_STATES = tuple(item.value for item in StageState)
PLANNER_DECISIONS = ("CONTINUE", "REPLAN", "STAGE_READY", "HUMAN_GATE", "BLOCKED")
# ``iteration_index`` is the Stage-level progress counter.  Execution retries
# are deliberately tracked by a separate counter and are bounded per open
# Stage iteration.  Eight is large enough for the bounded provider probes used
# by the real execution harness while still making an accidental infinite
# retry impossible.  Contracts may choose a smaller budget explicitly.
DEFAULT_EXECUTION_ATTEMPTS_PER_ITERATION = 8
RETRY_SEMANTICS_VERSION = "stage_retry_semantics.v1"
_EXECUTION_FAILURE_KINDS = frozenset(
    {
        "PROVIDER_FAILURE",
        "ADAPTER_FAILURE",
        "RUNNER_FAILURE",
        "ENGINEERING_FAILURE",
        "CONTRACT_FAILURE",
        "INFRASTRUCTURE_FAILURE",
        "EXECUTION_FAILURE",
        "TIMEOUT",
        "UNAVAILABLE",
    }
)
_SUCCESS_STATUSES = frozenset({"SUCCEEDED", "SUCCESS", "COMPLETE", "COMPLETED"})
_FAILURE_STATUSES = frozenset({"FAILED", "FAILURE", "ERROR"})
_SENSITIVE_KEYS = {
    "cookie",
    "cookies",
    "token",
    "tokens",
    "authorization",
    "session",
    "session_storage",
    "storage_state",
    "raw_dom",
    "dom",
    "prompt",
    "raw_response",
}

# Persisted events are content-addressed over this exact body.  Keep the
# field set explicit so recovery cannot silently accept an event with an
# omitted, injected, or renamed field.
_EVENT_BODY_KEYS = (
    "event",
    "stage_id",
    "from_status",
    "to_status",
    "details",
    "revision",
)
_EVENT_KEYS = frozenset((*_EVENT_BODY_KEYS, "event_id"))


def _event_id(body: Mapping[str, Any]) -> str:
    """Return the stable identity used for one persisted event body."""

    return "event-" + sha256_json(body)[:16]


def _text(value: Any, field: str, *, required: bool = True, max_length: int = 4000) -> str:
    if not isinstance(value, str):
        raise ContractValidationError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise ContractValidationError(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise ContractValidationError(f"{field} is too long")
    if "\x00" in value:
        raise ContractValidationError(f"{field} contains a NUL character")
    return value


def _normalise_mode(value: Any, field: str = "lane_mode") -> str:
    if value is None:
        return LaneMode.PRIMARY.value
    if isinstance(value, LaneMode):
        value = value.value
    if not isinstance(value, str):
        raise ContractValidationError(f"{field} must be a string")
    normalized = value.strip().upper()
    aliases = {
        "NONE": LaneMode.PRIMARY.value,
        "PRIMARY_ONLY": LaneMode.PRIMARY.value,
        "MAJOR": LaneMode.MAJOR_CHALLENGER.value,
        "MAJOR_REPLACEMENT": LaneMode.MAJOR_CHALLENGER.value,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in {item.value for item in LaneMode}:
        raise ContractValidationError(f"{field} must be PRIMARY or MAJOR_CHALLENGER")
    return normalized


def _normalise_string_list(value: Any, field: str, *, minimum: int = 0) -> list[str]:
    if not isinstance(value, list):
        raise ContractValidationError(f"{field} must be an array")
    values = [_text(item, f"{field}[{index}]") for index, item in enumerate(value)]
    if len(values) < minimum:
        raise ContractValidationError(f"{field} must contain at least {minimum} item(s)")
    if len(set(values)) != len(values):
        raise ContractValidationError(f"{field} must not contain duplicates")
    return values


def validate_stage_contract_v1(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach a complete user-authored ``stage_contract.v1``.

    ``status`` must be ``PLANNED`` at registration.  The controller keeps the
    workflow status separately, so starting a Stage never rewrites the
    user-authored goals or contract body.  ``lane_mode`` defaults to PRIMARY;
    a challenger is only available when a caller explicitly requests
    ``MAJOR_CHALLENGER`` later.
    """

    if not isinstance(contract, Mapping):
        raise ContractValidationError("stage contract must be an object")
    detached = copy.deepcopy(dict(contract))
    validate_against_schema(detached, "stage_contract.v1")
    if detached["status"] != StageState.PLANNED.value:
        raise ContractValidationError("a new stage contract must have status=PLANNED")
    for field in (
        "schema_version", "project_id", "repository_root", "stage_id", "stage_name",
        "project_goal", "stage_goal", "user_visible_goal", "acceptance_description",
    ):
        _text(detached[field], field)
    protected_paths = _normalise_string_list(detached["protected_paths"], "protected_paths")
    allowed_paths = _normalise_string_list(detached["allowed_paths"], "allowed_paths", minimum=1)
    try:
        detached["protected_paths"] = [_ensure_relative_path(item, "protected_paths") for item in protected_paths]
        detached["allowed_paths"] = [_ensure_relative_path(item, "allowed_paths") for item in allowed_paths]
    except ContractValidationError as exc:
        # Stage scope is a relative, repository-bounded contract.  In
        # particular, absolute paths and ``..`` traversal must fail before a
        # Stage is registered; the executor result check remains the second
        # line of defence.
        raise ContractValidationError(str(exc)) from exc
    _normalise_string_list(detached["required_checks"], "required_checks", minimum=1)
    inputs = detached["inputs"]
    if not isinstance(inputs, (list, dict)):
        raise ContractValidationError("inputs must be an array or object")
    artifacts = detached["review_artifact_requirements"]
    if not isinstance(artifacts, (list, dict)):
        raise ContractValidationError("review_artifact_requirements must be an array or object")
    if not artifacts:
        raise ContractValidationError("review_artifact_requirements must not be empty")
    # Retry budget is independent from ``max_iterations``.  Keep the
    # user-authored contract body unchanged when the field is omitted so old
    # contract digests remain valid during state recovery.
    _contract_retry_budget(detached)
    if "lane_mode" in detached and "comparison_mode" in detached:
        if _normalise_mode(detached["lane_mode"], "lane_mode") != _normalise_mode(
            detached["comparison_mode"], "comparison_mode"
        ):
            raise ContractValidationError("lane_mode and comparison_mode disagree")
    if "critic_mode" in detached:
        critic_mode = str(detached["critic_mode"]).strip().upper()
        if critic_mode not in {"NONE", "PRIMARY", "MAJOR_CHALLENGER"}:
            raise ContractValidationError("critic_mode is not supported")
        critic_as_mode = (
            LaneMode.MAJOR_CHALLENGER.value if critic_mode == LaneMode.MAJOR_CHALLENGER.value
            else LaneMode.PRIMARY.value
        )
        if "lane_mode" in detached and _normalise_mode(detached["lane_mode"]) != critic_as_mode:
            raise ContractValidationError("critic_mode and lane_mode disagree")
    mode = detached.get("lane_mode", detached.get("comparison_mode"))
    if mode is None and detached.get("critic_mode") == LaneMode.MAJOR_CHALLENGER.value:
        mode = LaneMode.MAJOR_CHALLENGER.value
    detached["lane_mode"] = _normalise_mode(mode)
    detached["comparison_mode"] = detached["lane_mode"]
    detached["critic_mode"] = (
        "MAJOR_CHALLENGER" if detached["lane_mode"] == LaneMode.MAJOR_CHALLENGER.value else "NONE"
    )
    return detached


def _safe_state_copy(value: Any) -> Any:
    """Copy state data while excluding known raw transport/secret fields."""

    if isinstance(value, Mapping):
        return {
            str(key): _safe_state_copy(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_KEYS
        }
    if isinstance(value, list):
        return [_safe_state_copy(item) for item in value]
    return copy.deepcopy(value)


def _execution_provider_failure(value: Any) -> dict[str, Any] | None:
    """Return a bounded provider-failure marker, if one was supplied.

    Provider availability is an execution outcome, not a research decision.
    Keep the controller provider-neutral: adapters may report the same small
    marker without making the controller know their product/model names.
    Malformed markers are ignored so the existing failed-result semantics stay
    fail-closed (and continue to route a genuine evidence failure to REPLAN).
    """

    if not isinstance(value, Mapping):
        return None
    kind = value.get("kind", value.get("class"))
    if not isinstance(kind, str) or kind.strip().upper() != "PROVIDER_FAILURE":
        return None
    code = value.get("code")
    if not isinstance(code, str) or not code.strip() or len(code) > 128 or "\x00" in code:
        return None
    bounded: dict[str, Any] = {
        "kind": "PROVIDER_FAILURE",
        "code": code.strip(),
    }
    retryable = value.get("retryable")
    if isinstance(retryable, bool):
        bounded["retryable"] = retryable
    reason = value.get("reason")
    if isinstance(reason, str) and reason.strip() and "\x00" not in reason:
        bounded["reason"] = reason.strip()[:512]
    return bounded


def _normalise_execution_failure(value: Any) -> dict[str, Any] | None:
    """Normalize a bounded execution-infrastructure failure marker.

    Providers and adapters can use different vocabularies, but the Stage
    controller only needs a small, provider-neutral classification.  The
    marker is intentionally metadata-only; raw prompts, replies and transport
    state never enter the persisted receipt.
    """

    provider = _execution_provider_failure(value)
    if provider is not None:
        return provider
    if not isinstance(value, Mapping):
        return None
    kind_value = value.get("kind", value.get("class", value.get("type")))
    if not isinstance(kind_value, str):
        return None
    kind = kind_value.strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {
        "ADAPTER": "ADAPTER_FAILURE",
        "RUNNER": "RUNNER_FAILURE",
        "ENGINEERING": "ENGINEERING_FAILURE",
        "ENGINEERING_ERROR": "ENGINEERING_FAILURE",
        "INFRA": "INFRASTRUCTURE_FAILURE",
        "INFRASTRUCTURE": "INFRASTRUCTURE_FAILURE",
        "EXECUTION": "EXECUTION_FAILURE",
        "TIMEOUT_FAILURE": "TIMEOUT",
        "UNAVAILABLE_FAILURE": "UNAVAILABLE",
    }
    kind = aliases.get(kind, kind)
    if kind not in _EXECUTION_FAILURE_KINDS:
        return None
    code = value.get("code", value.get("failure_code", value.get("error_code")))
    if not isinstance(code, str):
        code = kind
    code = code.strip()
    if not code or len(code) > 128 or "\x00" in code:
        return None
    bounded: dict[str, Any] = {"kind": kind, "code": code}
    retryable = value.get("retryable")
    if isinstance(retryable, bool):
        bounded["retryable"] = retryable
    elif kind in _EXECUTION_FAILURE_KINDS - {"CONTRACT_FAILURE"}:
        bounded["retryable"] = True
    reason = value.get("reason", value.get("message"))
    if isinstance(reason, str) and reason.strip() and "\x00" not in reason:
        bounded["reason"] = reason.strip()[:512]
    # Keep only bounded provenance fields useful for later audit.  The caller
    # separately stores the full safe result digest/receipt.
    for field in ("provider_id", "provider", "model", "executable", "adapter", "runner"):
        candidate = value.get(field)
        if isinstance(candidate, str) and candidate.strip() and "\x00" not in candidate:
            bounded[field] = candidate.strip()[:512]
    return bounded


def _execution_failure_from_result(result: Mapping[str, Any], status: str) -> dict[str, Any] | None:
    """Recognize internal execution failures without relabeling real results."""

    for key in (
        "execution_failure",
        "attempt_failure",
        "runner_failure",
        "adapter_failure",
        "engineering_failure",
    ):
        marker = _normalise_execution_failure(result.get(key))
        if marker is not None:
            return marker
    # CodexPro's legacy timeout envelope has no explicit failure marker.  Its
    # bounded terminal diagnostics are sufficient to classify the attempt.
    if result.get("timed_out") is True:
        return {"kind": "PROVIDER_FAILURE", "code": "PROVIDER_TIMEOUT", "retryable": True}
    measurements = result.get("measurements")
    if isinstance(measurements, Mapping):
        for candidate in measurements.values():
            if isinstance(candidate, Mapping) and candidate.get("timed_out") is True:
                return {"kind": "PROVIDER_FAILURE", "code": "PROVIDER_TIMEOUT", "retryable": True}
    # A terminal ERROR without a contract result is an execution failure.  A
    # bare FAILED result remains the legacy substantive REPLAN path for
    # compatibility; callers can mark it explicitly with contract_satisfied or
    # result_valid when it is an infrastructure failure.
    if status == "ERROR":
        return {"kind": "EXECUTION_FAILURE", "code": "EXECUTION_RESULT_INVALID", "retryable": True}
    # A provider can report a terminal success status while its bounded
    # contract flags say that no usable turn/result was produced.  Treat the
    # explicit contract failure as an execution retry for every status; a
    # status token alone cannot manufacture a legal Stage result.
    if (
        result.get("contract_satisfied") is False
        or result.get("result_valid") is False
        or result.get("turn_completed") is False
        or result.get("result_produced") is False
    ):
        return {"kind": "CONTRACT_FAILURE", "code": "EXECUTION_CONTRACT_UNSATISFIED", "retryable": True}
    return None


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractValidationError(f"{field} must be a positive integer")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StageControllerError(f"{field} must be a non-negative integer")
    return value


def _contract_retry_budget(contract: Mapping[str, Any]) -> int:
    """Return the bounded per-iteration execution-attempt budget."""

    candidates: list[tuple[str, Any]] = []
    for field in ("retry_budget", "execution_attempt_budget", "max_execution_attempts"):
        if field in contract:
            candidates.append((field, contract[field]))
    if not candidates:
        return DEFAULT_EXECUTION_ATTEMPTS_PER_ITERATION
    first_name, first_value = candidates[0]
    budget = _positive_int(first_value, first_name)
    if budget > 100:
        raise ContractValidationError(f"{first_name} must be at most 100")
    for name, value in candidates[1:]:
        checked = _positive_int(value, name)
        if checked != budget:
            raise ContractValidationError("execution retry budget aliases disagree")
    return budget


def _normalise_check_status(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in {"PASS", "PASSED", "SUCCESS", "SUCCEEDED", "TRUE", "OK"}


def _check_names_and_statuses(value: Any) -> tuple[set[str], bool]:
    names: set[str] = set()
    all_pass = True
    if isinstance(value, Mapping):
        for name, status in value.items():
            names.add(str(name))
            all_pass = all_pass and _normalise_check_status(status)
        return names, all_pass
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, Mapping):
                name = item.get("name", item.get("id", item.get("check")))
                if name is not None:
                    names.add(str(name))
                status = item.get("status", item.get("result", item.get("passed", False)))
                all_pass = all_pass and _normalise_check_status(status)
            else:
                all_pass = False
        return names, all_pass
    return names, False


def _artifact_names(value: Any) -> set[str]:
    names: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            names.add(str(key))
            if isinstance(item, Mapping):
                for candidate in (item.get("name"), item.get("id"), item.get("type"), item.get("kind")):
                    if candidate:
                        names.add(str(candidate))
            elif isinstance(item, str):
                names.add(item)
        return names
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                names.add(item)
            elif isinstance(item, Mapping):
                for key in ("name", "id", "type", "kind", "role"):
                    if item.get(key):
                        names.add(str(item[key]))
        return names
    return names


def _required_artifact_names(requirements: Any) -> set[str]:
    if isinstance(requirements, Mapping):
        return {str(key) for key, required in requirements.items() if required is not False}
    return {str(item) for item in requirements if isinstance(item, str)}


class StageController:
    """A deterministic local controller for one or more planned Stages."""

    STATE_VERSION = "stage_controller_state.v1"

    def __init__(
        self,
        contract: Mapping[str, Any] | None = None,
        *,
        contracts: Sequence[Mapping[str, Any]] | None = None,
        state_path: str | Path | None = None,
    ) -> None:
        self.state_path = Path(state_path).expanduser() if state_path is not None else None
        self._revision = 0
        self._stages: dict[str, dict[str, Any]] = {}
        self._events: list[dict[str, Any]] = []
        self._active_stage_id: str | None = None
        self._current_stage_id: str | None = None
        self._legacy_retry_stage_ids: set[str] = set()
        if self.state_path is not None and self.state_path.exists():
            self._load_state(self.state_path)
        if contract is not None:
            self.register_stage(contract)
        for item in contracts or []:
            self.register_stage(item)

    @classmethod
    def from_state(cls, state_path: str | Path) -> "StageController":
        """Rehydrate a controller from an explicit local JSON snapshot."""

        return cls(state_path=state_path)

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> "StageController":
        controller = cls()
        controller._load_snapshot(snapshot)
        return controller

    @property
    def status(self) -> str | None:
        selected = self._select_stage_id(None, allow_missing=True)
        return self._stages[selected]["status"] if selected is not None else None

    @property
    def state(self) -> dict[str, Any]:
        """Return a stage-centric public snapshot plus the full stage index."""

        selected = self._select_stage_id(None, allow_missing=True)
        current = copy.deepcopy(self._stages[selected]) if selected is not None else None
        result: dict[str, Any] = {
            "schema_version": self.STATE_VERSION,
            "revision": self._revision,
            "current_stage_id": self._current_stage_id,
            "active_stage_id": self._active_stage_id,
            "stages": {
                stage_id: copy.deepcopy(record) for stage_id, record in self._stages.items()
            },
            "events": copy.deepcopy(self._events),
        }
        if current is not None:
            result.update(current)
        return result

    def snapshot(self) -> dict[str, Any]:
        return self._snapshot()

    def to_dict(self) -> dict[str, Any]:
        return self._snapshot()

    def _snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": self.STATE_VERSION,
            "revision": self._revision,
            "current_stage_id": self._current_stage_id,
            "active_stage_id": self._active_stage_id,
            "stages": {
                stage_id: _safe_state_copy(record) for stage_id, record in self._stages.items()
            },
            "events": _safe_state_copy(self._events),
        }

    def _persist(self) -> None:
        if self.state_path is None:
            return
        parent = self.state_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".stage-controller-", suffix=".json", dir=str(parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(self._snapshot(), handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def save_state(self, state_path: str | Path | None = None) -> Path:
        if state_path is not None:
            self.state_path = Path(state_path).expanduser()
        if self.state_path is None:
            raise StageControllerError("an explicit state_path is required to persist state")
        self._persist()
        return self.state_path

    def _load_state(self, state_path: Path) -> None:
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StageControllerError(f"cannot load stage controller state: {state_path}") from exc
        self._load_snapshot(payload)

    def _load_snapshot(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping) or payload.get("schema_version") != self.STATE_VERSION:
            raise StageControllerError("invalid stage controller state schema")
        stages = payload.get("stages")
        if not isinstance(stages, Mapping):
            raise StageControllerError("stage controller state must contain an object of stages")
        loaded: dict[str, dict[str, Any]] = {}
        legacy_retry_stage_ids: set[str] = set()
        for stage_id, raw_record in stages.items():
            if not isinstance(raw_record, Mapping):
                raise StageControllerError("persisted stage record must be an object")
            record = copy.deepcopy(dict(raw_record))
            if record.get("status") not in STAGE_STATES:
                raise StageControllerError("persisted stage has an invalid state")
            checked = validate_stage_contract_v1(record.get("contract", {}))
            if str(stage_id) != checked["stage_id"]:
                raise StageControllerError("persisted stage id does not match its contract")
            expected_digest = sha256_json(checked["baseline"])
            if record.get("baseline_digest") != expected_digest:
                raise StageControllerError("persisted baseline digest does not match contract")
            if canonical_json(record.get("baseline")) != canonical_json(checked["baseline"]):
                raise StageControllerError("persisted baseline does not match the immutable contract baseline")
            if record.get("contract_digest") != sha256_json(checked):
                raise StageControllerError("persisted contract digest does not match the immutable contract")
            if "events" not in record or not isinstance(record["events"], list):
                raise StageControllerError("persisted stage must contain an events array")
            has_retry_semantics = (
                record.get("retry_semantics_version") == RETRY_SEMANTICS_VERSION
                and isinstance(record.get("execution_attempts"), list)
                and isinstance(record.get("attempt_count"), int)
                and not isinstance(record.get("attempt_count"), bool)
                and "open_iteration_index" in record
            )
            if not has_retry_semantics:
                legacy_retry_stage_ids.add(checked["stage_id"])
            record["contract"] = checked
            record.setdefault("executor_requests", [])
            record.setdefault("consultation_requests", [])
            record.setdefault("consulted_evidence", [])
            record.setdefault("reviews", [])
            record.setdefault("iteration_index", 0)
            record.setdefault("open_iteration_index", None)
            record.setdefault("latest_result", None)
            record.setdefault("latest_stage_result", None)
            record.setdefault("stage_result_binding", None)
            record.setdefault("execution_evidence_complete", False)
            record.setdefault("execution_attempts", [])
            record.setdefault("attempt_count", len(record.get("executor_requests", [])))
            record.setdefault("retry_count", 0)
            record.setdefault("retry_budget", _contract_retry_budget(checked))
            record.setdefault("retry_semantics_version", RETRY_SEMANTICS_VERSION)
            record.setdefault("migration", None)
            record.setdefault("pending_human_gate", None)
            record.setdefault("stop_reason", None)
            record.setdefault("lane_mode", checked["lane_mode"])
            record.setdefault("lanes", ["PRIMARY"])
            record.setdefault("challenger_count", 0)
            if (
                isinstance(record.get("iteration_index"), bool)
                or not isinstance(record.get("iteration_index"), int)
                or record.get("iteration_index", 0) < 0
            ):
                raise StageControllerError("persisted Stage iteration_index is invalid")
            open_iteration = record.get("open_iteration_index")
            if open_iteration is not None and (
                isinstance(open_iteration, bool)
                or not isinstance(open_iteration, int)
                or open_iteration < 1
            ):
                raise StageControllerError("persisted open Stage iteration_index is invalid")
            if (
                isinstance(record.get("attempt_count"), bool)
                or not isinstance(record.get("attempt_count"), int)
                or record.get("attempt_count", 0) < 0
            ):
                raise StageControllerError("persisted execution attempt count is invalid")
            if not isinstance(record.get("execution_attempts"), list):
                raise StageControllerError("persisted execution attempts must be an array")
            if (
                not isinstance(record.get("retry_budget"), int)
                or isinstance(record.get("retry_budget"), bool)
                or not 1 <= record.get("retry_budget", 0) <= 100
            ):
                raise StageControllerError("persisted retry budget is invalid")
            loaded[checked["stage_id"]] = record

        revision = payload.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise StageControllerError("persisted revision must be a non-negative integer")
        if "events" not in payload or not isinstance(payload["events"], list):
            raise StageControllerError("stage controller state must contain an events array")
        persisted_events = payload["events"]
        if revision != len(persisted_events):
            raise StageControllerError("persisted revision does not match the event count")

        event_ids: set[str] = set()
        checked_events: list[dict[str, Any]] = []
        for expected_revision, raw_event in enumerate(persisted_events, start=1):
            if not isinstance(raw_event, Mapping) or set(raw_event) != _EVENT_KEYS:
                raise StageControllerError("persisted event has an invalid body shape")
            event = copy.deepcopy(dict(raw_event))
            event_revision = event.get("revision")
            if isinstance(event_revision, bool) or not isinstance(event_revision, int):
                raise StageControllerError("persisted event revision must be an integer")
            if event_revision != expected_revision:
                raise StageControllerError("persisted event revisions must be contiguous")
            event_stage_id = event.get("stage_id")
            if not isinstance(event_stage_id, str) or not event_stage_id.strip():
                raise StageControllerError("persisted event stage_id must be a non-empty string")
            if event_stage_id not in loaded:
                raise StageControllerError("persisted event references an unknown stage")
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                raise StageControllerError("persisted event_id must be a non-empty string")
            if event_id in event_ids:
                raise StageControllerError("persisted event_id values must be unique")
            body = {key: event[key] for key in _EVENT_BODY_KEYS}
            if event_id != _event_id(body):
                raise StageControllerError("persisted event_id does not match its body")
            event_ids.add(event_id)
            checked_events.append(event)

        for stage_id, record in loaded.items():
            expected_stage_events = [
                event for event in checked_events if event["stage_id"] == stage_id
            ]
            if canonical_json(record["events"]) != canonical_json(expected_stage_events):
                raise StageControllerError("persisted stage events do not mirror the global event log")

        active_id = payload.get("active_stage_id")
        if active_id is not None:
            if active_id not in loaded or loaded[active_id]["status"] != StageState.ACTIVE.value:
                raise StageControllerError("persisted active_stage_id is inconsistent")
        current_id = payload.get("current_stage_id")
        if current_id is not None and current_id not in loaded:
            raise StageControllerError("persisted current_stage_id is unknown")
        self._stages = loaded
        self._active_stage_id = active_id
        self._current_stage_id = current_id
        self._revision = revision
        self._events = checked_events
        self._legacy_retry_stage_ids = legacy_retry_stage_ids

    def _select_stage_id(self, stage_id: str | None, *, allow_missing: bool = False) -> str | None:
        if stage_id is not None:
            if not isinstance(stage_id, str) or not stage_id.strip():
                raise ContractValidationError("stage_id must be a non-empty string")
            if stage_id not in self._stages:
                raise StageControllerError(f"unknown stage: {stage_id}")
            return stage_id
        if self._active_stage_id is not None:
            return self._active_stage_id
        if self._current_stage_id is not None:
            return self._current_stage_id
        if len(self._stages) == 1:
            return next(iter(self._stages))
        if allow_missing:
            return None
        if not self._stages:
            raise StageControllerError("no stage contract has been registered")
        raise StageControllerError("stage_id is required when more than one Stage is registered")

    def _record(self, stage_id: str | None) -> dict[str, Any]:
        selected = self._select_stage_id(stage_id)
        assert selected is not None
        return self._stages[selected]

    def _require_active(self, stage_id: str | None) -> tuple[str, dict[str, Any]]:
        selected = self._select_stage_id(stage_id)
        assert selected is not None
        record = self._stages[selected]
        status = record["status"]
        if status != StageState.ACTIVE.value:
            raise StageControllerError(
                f"operation rejected: Stage {selected} is {status}; only ACTIVE allows this request"
            )
        if record.get("pending_human_gate") is not None:
            raise StageControllerError("operation rejected: a HUMAN_GATE decision is awaiting explicit resolution")
        return selected, record

    @staticmethod
    def _actor_and_rationale(actor: Any, rationale: Any) -> tuple[str, str]:
        return _text(actor, "actor"), _text(rationale, "rationale", required=False)

    def _append_event(
        self,
        event_type: str,
        stage_id: str,
        *,
        from_status: str | None,
        to_status: str | None,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = {
            "event": event_type,
            "stage_id": stage_id,
            "from_status": from_status,
            "to_status": to_status,
            "details": _safe_state_copy(dict(details or {})),
            "revision": self._revision + 1,
        }
        event = {
            **body,
            "event_id": _event_id(body),
        }
        self._revision += 1
        self._events.append(copy.deepcopy(event))
        self._stages[stage_id]["events"].append(copy.deepcopy(event))
        self._persist()
        return event

    def register_stage(self, contract: Mapping[str, Any]) -> dict[str, Any]:
        """Register a planned contract; registration never starts it."""

        checked = validate_stage_contract_v1(contract)
        stage_id = checked["stage_id"]
        if stage_id in self._stages:
            existing = self._stages[stage_id]["contract"]
            if canonical_json(existing) != canonical_json(checked):
                raise StageControllerError(f"stage {stage_id} is already registered with a different contract")
            return self.show_stage(stage_id)
        baseline_digest = sha256_json(checked["baseline"])
        self._stages[stage_id] = {
            "contract": checked,
            "contract_digest": sha256_json(checked),
            "status": StageState.PLANNED.value,
            "baseline": copy.deepcopy(checked["baseline"]),
            "baseline_digest": baseline_digest,
            "iteration_index": 0,
            "open_iteration_index": None,
            "latest_result": None,
            "latest_stage_result": None,
            "stage_result_binding": None,
            "execution_evidence_complete": False,
            "executor_requests": [],
            "execution_attempts": [],
            "attempt_count": 0,
            "retry_count": 0,
            "retry_budget": _contract_retry_budget(checked),
            "retry_semantics_version": RETRY_SEMANTICS_VERSION,
            "migration": None,
            "consultation_requests": [],
            "consulted_evidence": [],
            "reviews": [],
            "pending_human_gate": None,
            "stop_reason": None,
            "lane_mode": checked["lane_mode"],
            "lanes": ["PRIMARY"],
            "challenger_count": 0,
            "events": [],
        }
        self._current_stage_id = stage_id
        event = self._append_event(
            "stage_registered", stage_id, from_status=None, to_status=StageState.PLANNED.value,
            details={"baseline_digest": baseline_digest, "lane_mode": checked["lane_mode"]},
        )
        return {"event": event, "stage": self.show_stage(stage_id)}

    prepare_stage = register_stage

    def show_stage(self, stage_id: str | None = None) -> dict[str, Any]:
        return copy.deepcopy(self._record(stage_id))

    get_stage = show_stage

    def start_stage(
        self,
        stage_id: str | None = None,
        *,
        actor: str = "local-human",
        rationale: str = "",
    ) -> dict[str, Any]:
        """Explicitly transition one PLANNED Stage to ACTIVE."""

        actor, rationale = self._actor_and_rationale(actor, rationale)
        selected = self._select_stage_id(stage_id)
        assert selected is not None
        record = self._stages[selected]
        if record["status"] != StageState.PLANNED.value:
            raise StageControllerError(f"stage {selected} cannot start from {record['status']}")
        if any(
            item["status"] in {StageState.ACTIVE.value, StageState.STAGE_READY.value}
            for key, item in self._stages.items() if key != selected
        ):
            raise StageControllerError("another Stage is ACTIVE or STAGE_READY")
        record["status"] = StageState.ACTIVE.value
        record["pending_human_gate"] = None
        self._current_stage_id = selected
        self._active_stage_id = selected
        event = self._append_event(
            "start_stage", selected, from_status=StageState.PLANNED.value,
            to_status=StageState.ACTIVE.value, details={"actor": actor, "rationale": rationale},
        )
        return {"event": event, "stage": self.show_stage(selected)}

    def _validate_goal_immutability(self, payload: Mapping[str, Any], contract: Mapping[str, Any]) -> None:
        for field in ("project_goal", "stage_goal", "user_visible_goal"):
            if field in payload and payload[field] != contract[field]:
                raise ContractValidationError(f"{field} cannot be changed during Stage execution")

    def _validate_mode_and_lane(self, mode: Any, lane: Any) -> tuple[str, str]:
        normalized_mode = _normalise_mode(mode, "mode")
        if not isinstance(lane, str):
            raise ContractValidationError("lane must be PRIMARY or CHALLENGER")
        normalized_lane = lane.strip().upper()
        if normalized_lane not in {"PRIMARY", "CHALLENGER"}:
            raise ContractValidationError("lane must be PRIMARY or CHALLENGER")
        if normalized_mode == LaneMode.PRIMARY.value and normalized_lane != "PRIMARY":
            raise StageControllerError("CHALLENGER requires an explicit mode=MAJOR_CHALLENGER request")
        return normalized_mode, normalized_lane

    @staticmethod
    def _open_stage_iteration(record: Mapping[str, Any]) -> int:
        """Return the Stage iteration currently accepting execution attempts.

        ``iteration_index`` is the last committed Stage iteration for normal
        execution.  A technical review can explicitly open the next Stage
        iteration before its first attempt is requested, so the open
        coordinate is persisted separately.  Legacy snapshots do not have the
        coordinate; in that case the next iteration after the committed one is
        the safe default.
        """

        open_iteration = record.get("open_iteration_index")
        if (
            isinstance(open_iteration, int)
            and not isinstance(open_iteration, bool)
            and open_iteration >= 1
        ):
            return open_iteration
        committed = record.get("iteration_index", 0)
        if not isinstance(committed, int) or isinstance(committed, bool) or committed < 0:
            raise StageControllerError("persisted Stage iteration_index is invalid")
        return committed + 1

    @staticmethod
    def _find_executor_request(
        record: Mapping[str, Any], request_id: str,
    ) -> dict[str, Any] | None:
        """Return the most recent immutable executor request with ``request_id``."""

        for item in reversed(record.get("executor_requests", [])):
            if isinstance(item, Mapping) and item.get("request_id") == request_id:
                return dict(item)
        return None

    @staticmethod
    def _find_execution_attempt(
        record: Mapping[str, Any], request: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Resolve the receipt for one request without selecting another attempt."""

        request_id = request.get("request_id")
        attempt_index = request.get("attempt_index")
        for item in reversed(record.get("execution_attempts", [])):
            if not isinstance(item, Mapping) or item.get("request_id") != request_id:
                continue
            if isinstance(attempt_index, int) and item.get("attempt_index") != attempt_index:
                continue
            return dict(item)
        return None

    @staticmethod
    def _attempt_is_inflight(attempt: Mapping[str, Any]) -> bool:
        """Use the same fail-closed terminal test as result reconciliation."""

        return (
            attempt.get("status") in {None, "REQUESTED"}
            and attempt.get("outcome") in {None, "REQUESTED"}
        )

    @staticmethod
    def _semantic_replan_event(event: Mapping[str, Any]) -> bool:
        """Recognize structured semantic replans, never executor result text."""

        event_type = event.get("event")
        details = event.get("details")
        if not isinstance(details, Mapping):
            return False
        decision = details.get("decision", details.get("outcome"))
        normalized = decision.strip().upper() if isinstance(decision, str) else None
        if event_type == "planner_decision":
            return normalized in {"REPLAN", "REPLAN_NEXT_ITERATION"}
        if event_type == "technical_review":
            return (
                details.get("requires_next_iteration") is True
                and normalized in {
                    "NEXT_ITERATION",
                    "TECHNICAL_MODIFICATION_REQUIRED",
                    "REPLAN_NEXT_ITERATION",
                    "REPLAN",
                }
            )
        return any(
            details.get(field) is True
            for field in (
                "semantic_replan",
                "semantic_change",
                "semantic_change_required",
                "requirement_change",
                "requirement_changed",
                "design_change",
                "design_changed",
                "requirement_design_change",
                "objective_change",
            )
        )

    def _has_unresolved_semantic_replan(self, record: Mapping[str, Any]) -> bool:
        """Return whether a planner REPLAN still lacks a real next iteration."""

        events = [item for item in record.get("events", []) if isinstance(item, Mapping)]
        current_iteration = record.get("open_iteration_index")
        if not isinstance(current_iteration, int) or current_iteration < 1:
            current_iteration = record.get("iteration_index", 0)
        last_technical_revision = 0
        for event in events:
            if event.get("event") != "technical_review":
                continue
            details = event.get("details")
            target = details.get("to_iteration_index") if isinstance(details, Mapping) else None
            if (
                isinstance(details, Mapping)
                and details.get("requires_next_iteration") is True
                and isinstance(target, int)
                and target <= current_iteration
            ):
                revision = event.get("revision")
                if isinstance(revision, int):
                    last_technical_revision = max(last_technical_revision, revision)
        for event in events:
            revision = event.get("revision")
            if isinstance(revision, int) and revision <= last_technical_revision:
                continue
            if event.get("event") == "planner_decision" and self._semantic_replan_event(event):
                return True
            if (
                event.get("event") != "executor_result"
                and self._semantic_replan_event(event)
            ):
                return True
        return False

    @staticmethod
    def _retry_failure_marker(
        receipt: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Validate that a terminal receipt is an engineering-retry candidate."""

        result = receipt.get("result")
        if not isinstance(result, Mapping):
            return None
        status_value = result.get("status", receipt.get("status"))
        status = status_value.strip().upper() if isinstance(status_value, str) else ""
        if status not in _FAILURE_STATUSES:
            return None
        if result.get("retryable") is False or StageController._receipt_has_nonretryable_failure(receipt):
            return None
        if result.get("human_gate_required") is True or result.get("pending_human_gate") is not None:
            return None
        if any(
            result.get(field) is True
            for field in (
                "semantic_replan",
                "semantic_change",
                "semantic_change_required",
                "requirement_change",
                "requirement_changed",
                "design_change",
                "design_changed",
                "requirement_design_change",
                "objective_change",
            )
        ):
            return None

        marker_keys = (
            "execution_failure",
            "attempt_failure",
            "runner_failure",
            "adapter_failure",
            "engineering_failure",
        )
        present_markers = [key for key in marker_keys if key in result]
        if present_markers:
            marker: dict[str, Any] | None = None
            for key in present_markers:
                marker = _normalise_execution_failure(result.get(key))
                if marker is not None:
                    break
            if marker is None or marker.get("retryable") is False:
                return None
            return marker

        # ``ERROR`` without a marker is normalized by
        # ``_execution_failure_from_result`` before this receipt is written.
        # Accept that bounded receipt through the same engineering retry seam.
        receipt_failure = receipt.get("execution_failure")
        normalized_receipt_failure = _normalise_execution_failure(receipt_failure)
        if normalized_receipt_failure is not None:
            if normalized_receipt_failure.get("retryable") is False:
                return None
            return normalized_receipt_failure

        # A bare FAILED/FAILURE receipt is the legacy substantive executor
        # outcome.  It is eligible only if its executor outcome was the
        # historical REPLAN; that outcome is deliberately preserved as-is.
        decision = receipt.get("decision", receipt.get("outcome"))
        if isinstance(decision, str) and decision.strip().upper() == "REPLAN":
            return {
                "kind": "ENGINEERING_FAILURE",
                "code": "BARE_FAILED_RESULT",
                "retryable": True,
            }
        return None

    @staticmethod
    def _build_executor_request(
        record: Mapping[str, Any],
        stage_id: str,
        *,
        iteration_index: int,
        attempt_index: int,
        mode: str,
        lane: str,
        objective: str,
        retry_of_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Build one immutable executor request without mutating controller state."""

        retry_budget = _contract_retry_budget(record["contract"])
        request_body: dict[str, Any] = {
            "stage_id": stage_id,
            "iteration_index": iteration_index,
            "stage_iteration_index": iteration_index,
            "attempt_index": attempt_index,
            "attempt_number": attempt_index,
            "mode": mode,
            "lane": lane,
            "objective": objective,
            "baseline_digest": record["baseline_digest"],
            "retry_budget": retry_budget,
        }
        if retry_of_request_id is not None:
            request_body["retry_of_request_id"] = retry_of_request_id
        request = {
            "request_id": "executor-" + sha256_json(request_body)[:16],
            "request_type": "executor",
            **request_body,
            "allowed_paths": list(record["contract"]["allowed_paths"]),
            "protected_paths": list(record["contract"]["protected_paths"]),
        }
        request["execution_attempt"] = attempt_index
        return request

    def _engineering_retry_pending(self, record: Mapping[str, Any]) -> bool:
        """Detect a committed engineering retry that still awaits its result."""

        for request in record.get("executor_requests", []):
            if not isinstance(request, Mapping) or not request.get("retry_of_request_id"):
                continue
            attempt = self._find_execution_attempt(record, request)
            if attempt is not None and self._attempt_is_inflight(attempt):
                return True
        return False

    @staticmethod
    def _receipt_has_nonretryable_failure(receipt: Mapping[str, Any]) -> bool:
        """Return whether a terminal execution receipt explicitly forbids retry."""

        result = receipt.get("result")
        if not isinstance(result, Mapping):
            result = {}
        if result.get("retryable") is False:
            return True
        for key in (
            "execution_failure",
            "attempt_failure",
            "runner_failure",
            "adapter_failure",
            "engineering_failure",
        ):
            marker = _normalise_execution_failure(result.get(key))
            if marker is not None and marker.get("retryable") is False:
                return True
        marker = _normalise_execution_failure(receipt.get("execution_failure"))
        return marker is not None and marker.get("retryable") is False

    @staticmethod
    def _receipt_result_status(receipt: Mapping[str, Any]) -> str:
        result = receipt.get("result")
        value = result.get("status") if isinstance(result, Mapping) else None
        if value is None:
            value = receipt.get("status")
        return value.strip().upper() if isinstance(value, str) else ""

    def _receipt_request(
        self,
        record: Mapping[str, Any],
        receipt: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        request = receipt.get("request")
        if isinstance(request, Mapping):
            return request
        request_id = receipt.get("request_id")
        if isinstance(request_id, str):
            return self._find_executor_request(record, request_id)
        return None

    def _direct_executor_requires_engineering_retry(
        self,
        record: Mapping[str, Any],
        *,
        mode: str,
        lane: str,
        objective: str | None = None,
    ) -> bool:
        """Prevent a normal request from bypassing a committed retry gate."""

        if self._has_unresolved_semantic_replan(record):
            return True
        if self._engineering_retry_pending(record):
            return True

        open_iteration = record.get("open_iteration_index")
        current_iteration = (
            open_iteration
            if isinstance(open_iteration, int) and not isinstance(open_iteration, bool)
            else record.get("iteration_index", 0)
        )
        if not isinstance(current_iteration, int) or isinstance(current_iteration, bool):
            return True

        current_failures: list[tuple[Mapping[str, Any], Mapping[str, Any] | None]] = []
        for raw_attempt in reversed(record.get("execution_attempts", [])):
            if not isinstance(raw_attempt, Mapping):
                continue
            attempt_iteration = raw_attempt.get("iteration_index")
            request = self._receipt_request(record, raw_attempt)
            if not isinstance(attempt_iteration, int) and isinstance(request, Mapping):
                attempt_iteration = request.get("iteration_index")
            if attempt_iteration != current_iteration:
                continue
            if self._attempt_is_inflight(raw_attempt):
                # A second request in the same lane would duplicate an
                # in-flight dispatch.  The explicitly requested
                # PRIMARY/CHALLENGER lanes may still run in parallel.
                attempt_lane = request.get("lane", "PRIMARY") if isinstance(request, Mapping) else "PRIMARY"
                if isinstance(attempt_lane, str) and attempt_lane.strip().upper() == lane:
                    return True
                continue
            status = self._receipt_result_status(raw_attempt)
            if status in _FAILURE_STATUSES:
                current_failures.append((raw_attempt, request))

        for receipt, request in current_failures:
            # Every subsequent engineering attempt must carry the canonical
            # predecessor identity, including ERROR/provider/runner failures.
            if isinstance(request, Mapping) and request.get("lane", "PRIMARY") == lane:
                return True
            if self._retry_failure_marker(receipt) is None:
                return True
            if objective is not None and isinstance(request, Mapping):
                request_objective = request.get("objective")
                if request_objective is not None and request_objective != objective:
                    return True
            # A substantive FAILED receipt closes its coordinate.  Reopening
            # it must go through request_engineering_retry even when a
            # semantic marker made the receipt ineligible for that seam.
            if self._receipt_result_status(receipt) == "FAILED":
                return True

        # Legacy snapshots can carry a latest failed result without a fully
        # reconstructed attempt receipt.  Keep the closed-coordinate guard
        # fail-closed, while allowing the first request of a newly opened
        # semantic iteration whose failures belong to an older iteration.
        if open_iteration is None and isinstance(record.get("latest_result"), Mapping):
            latest_status = record["latest_result"].get("status")
            if isinstance(latest_status, str) and latest_status.strip().upper() in _FAILURE_STATUSES:
                return True
        return False

    def request_executor(
        self,
        stage_id: str | None = None,
        *,
        objective: str | None = None,
        mode: str | LaneMode = LaneMode.PRIMARY.value,
        comparison_mode: str | LaneMode | None = None,
        critic_mode: str | LaneMode | None = None,
        lane: str = "PRIMARY",
        iteration_index: int | None = None,
    ) -> dict[str, Any]:
        """Authorize one bounded provider-neutral execution request.

        The returned request is an execution contract only; a selected
        provider/adapter must perform the work and return evidence for this
        controller to validate.  No provider identity is stored in the Stage
        lifecycle and this method never starts an executor.
        """

        selected, record = self._require_active(stage_id)
        supplied_modes = [item for item in (comparison_mode, critic_mode) if item is not None]
        if supplied_modes:
            if len({str(item).strip().upper() for item in supplied_modes}) != 1:
                raise ContractValidationError("comparison_mode and critic_mode disagree")
            if mode != LaneMode.PRIMARY.value:
                if _normalise_mode(mode, "mode") != _normalise_mode(supplied_modes[0], "comparison_mode"):
                    raise ContractValidationError("mode and comparison_mode disagree")
            mode = supplied_modes[0]
        normalized_mode, normalized_lane = self._validate_mode_and_lane(mode, lane)
        # ``iteration_index`` identifies the currently open Stage iteration.
        # It is intentionally stable across execution retries.  The old
        # implementation derived a fresh iteration for every request, which
        # made a provider retry look like technical progress.
        current_iteration = self._open_stage_iteration(record)
        next_iteration = current_iteration if iteration_index is None else iteration_index
        if not isinstance(next_iteration, int) or isinstance(next_iteration, bool) or next_iteration < 1:
            raise ContractValidationError("iteration_index must be a positive integer")
        if next_iteration != current_iteration:
            raise StageControllerError(
                "executor request must target the currently open Stage iteration"
            )
        maximum = record["contract"].get("max_iterations")
        if maximum is not None and next_iteration > maximum:
            raise StageControllerError("executor request exceeds max_iterations")
        objective_text = objective if objective is not None else record["contract"]["stage_goal"]
        if objective is None:
            for previous in reversed(record.get("executor_requests", [])):
                if previous.get("iteration_index") == next_iteration and previous.get("lane", "PRIMARY") == normalized_lane:
                    objective_text = previous["objective"]
                    break
        objective_text = _text(objective_text, "objective")

        # Do not mutate lane bookkeeping until every request validation above
        # has succeeded.  A rejected request must be observationally inert.
        if self._direct_executor_requires_engineering_retry(
            record,
            mode=normalized_mode,
            lane=normalized_lane,
            objective=objective_text,
        ):
            raise StageControllerError(
                "an engineering retry transition is required before requesting another executor attempt"
            )

        retry_budget = _contract_retry_budget(record["contract"])
        requests_in_iteration = sum(
            1
            for item in record.get("executor_requests", [])
            if isinstance(item, Mapping) and item.get("iteration_index") == next_iteration
        )
        if requests_in_iteration >= retry_budget:
            record["open_iteration_index"] = None
            record["status"] = StageState.BLOCKED.value
            if self._active_stage_id == selected:
                self._active_stage_id = None
            self._append_event(
                "engineering_retry_blocked",
                selected,
                from_status=StageState.ACTIVE.value,
                to_status=StageState.BLOCKED.value,
                details={
                    "decision": "BLOCKED",
                    "reason": "EXECUTION_RETRY_BUDGET_EXHAUSTED",
                    "iteration_index": next_iteration,
                    "attempt_count": record.get(
                        "attempt_count", len(record.get("execution_attempts", []))
                    ),
                    "retry_budget": retry_budget,
                    "stage_iteration_advanced": False,
                },
            )
            raise StageControllerError(
                f"execution retry budget exhausted for Stage iteration {next_iteration}"
            )
        # ``attempt_index`` is scoped to the open Stage iteration so each
        # technical iteration starts at attempt 1.  ``attempt_count`` below
        # remains the lifetime count across the Stage and is useful for an
        # audit summary; the retry budget is enforced with the per-iteration
        # request count above.
        attempt_index = requests_in_iteration + 1
        if normalized_lane == "CHALLENGER":
            if record["challenger_count"] >= 1:
                raise StageControllerError("at most one challenger lane is allowed for a Stage")
        request = self._build_executor_request(
            record,
            selected,
            iteration_index=next_iteration,
            attempt_index=attempt_index,
            mode=normalized_mode,
            lane=normalized_lane,
            objective=objective_text,
        )
        if normalized_lane == "CHALLENGER":
            record["challenger_count"] += 1
            if "CHALLENGER" not in record["lanes"]:
                record["lanes"].append("CHALLENGER")
        # ``attempt_index`` is part of the identity, so two retries with the
        # same objective/iteration cannot collide on request_id.  Keep the
        # aliases in the wire request for adapters that use either spelling.
        # Keep the open Stage coordinate stable while provider/adapter
        # failures are retried.  It is cleared only when a result commits the
        # Stage iteration or an explicit technical review opens another one.
        record["open_iteration_index"] = next_iteration
        record["executor_requests"].append(copy.deepcopy(request))
        record["attempt_count"] = len(record["executor_requests"])
        record["retry_count"] = max(
            0,
            sum(
                1
                for item in record["executor_requests"]
                if isinstance(item, Mapping) and item.get("iteration_index") == next_iteration
            )
            - 1,
        )
        record.setdefault("execution_attempts", []).append(
            {
                "attempt_index": attempt_index,
                "iteration_index": next_iteration,
                "request_id": request["request_id"],
                "status": "REQUESTED",
                "outcome": "REQUESTED",
                "request": _safe_state_copy(request),
            }
        )
        event = self._append_event(
            "executor_requested", selected, from_status=record["status"], to_status=record["status"],
            details={
                "request_id": request["request_id"], "iteration_index": next_iteration,
                "attempt_index": attempt_index, "retry_budget": retry_budget,
                "mode": normalized_mode, "lane": normalized_lane,
            },
        )
        return {"event": event, "request": request, "stage": self.show_stage(selected)}

    def request_engineering_retry(
        self,
        *,
        previous_request_id: str,
        stage_id: str | None = None,
        objective: str | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        """Commit one bounded same-iteration engineering retry.

        This seam is intentionally separate from ``request_executor``.  A
        substantive executor ``FAILED`` result historically records
        ``REPLAN`` and closes its open coordinate, while an execution/provider
        failure keeps that coordinate open.  The explicit retry transition
        reopens the failed request's existing Stage iteration without changing
        the historical result, event, or semantic iteration counter.
        """

        selected, record = self._require_active(stage_id)
        previous_request_id = _text(previous_request_id, "previous_request_id", max_length=256)
        rationale = _text(rationale, "rationale", required=False, max_length=2000)
        previous_request = self._find_executor_request(record, previous_request_id)
        if previous_request is None:
            raise StageControllerError("engineering retry references an unknown executor request")
        previous_attempt = self._find_execution_attempt(record, previous_request)
        if previous_attempt is None:
            raise StageControllerError("engineering retry requires a recorded executor failure")
        if self._attempt_is_inflight(previous_attempt):
            raise StageControllerError("engineering retry requires the previous executor attempt to be terminal")
        failure_marker = self._retry_failure_marker(previous_attempt)
        if failure_marker is None:
            raise StageControllerError(
                "previous executor result is not an eligible retryable engineering failure"
            )

        previous_iteration = previous_request.get("iteration_index")
        if (
            isinstance(previous_iteration, bool)
            or not isinstance(previous_iteration, int)
            or previous_iteration < 1
        ):
            raise StageControllerError("previous executor request has an invalid Stage iteration")
        open_iteration = record.get("open_iteration_index")
        current_iteration = open_iteration if isinstance(open_iteration, int) else record.get("iteration_index")
        if (
            isinstance(current_iteration, bool)
            or not isinstance(current_iteration, int)
            or current_iteration < 1
            or previous_iteration != current_iteration
        ):
            raise StageControllerError(
                "engineering retry cannot reopen a previous or superseded Stage iteration"
            )
        committed_iteration = record.get("iteration_index", 0)
        if (
            isinstance(committed_iteration, bool)
            or not isinstance(committed_iteration, int)
            or committed_iteration < 0
            or previous_iteration < committed_iteration
        ):
            raise StageControllerError("engineering retry cannot move the Stage iteration backwards")

        # A technical review/planner REPLAN is a semantic transition.  The
        # executor's historical REPLAN receipt is intentionally not one of
        # these events; only the structured review/planner event is authoritative.
        if self._has_unresolved_semantic_replan(record):
            raise StageControllerError(
                "engineering retry is blocked by an unresolved semantic REPLAN"
            )
        failure_revision = 0
        for event in record.get("events", []):
            if not isinstance(event, Mapping) or event.get("event") != "executor_result":
                continue
            details = event.get("details")
            if not isinstance(details, Mapping) or details.get("request_id") != previous_request_id:
                continue
            if details.get("attempt_index") == previous_request.get("attempt_index"):
                revision = event.get("revision")
                if isinstance(revision, int):
                    failure_revision = max(failure_revision, revision)
        for event in record.get("events", []):
            if not isinstance(event, Mapping):
                continue
            revision = event.get("revision")
            if not isinstance(revision, int) or revision <= failure_revision:
                continue
            if self._semantic_replan_event(event):
                raise StageControllerError(
                    "engineering retry is blocked by a later semantic transition"
                )

        expected_objective = previous_request.get("objective")
        if expected_objective is None:
            expected_objective = record["contract"]["stage_goal"]
        expected_objective = _text(expected_objective, "objective")
        objective_text = expected_objective if objective is None else _text(objective, "objective")
        if objective_text != expected_objective:
            raise ContractValidationError("engineering retry objective must remain unchanged")

        # A retry request is keyed only by the failed request id.  Reusing it
        # after a reload is a read-only operation and must not append another
        # request, attempt, event, or revision.  Validate the supplied
        # objective before returning the existing request so a changed
        # objective cannot be hidden behind idempotent reuse.
        for existing_request in reversed(record.get("executor_requests", [])):
            if not isinstance(existing_request, Mapping):
                continue
            if existing_request.get("retry_of_request_id") != previous_request_id:
                continue
            existing_objective = existing_request.get("objective", expected_objective)
            if existing_objective != objective_text:
                raise ContractValidationError("engineering retry objective must remain unchanged")
            return {
                "request": copy.deepcopy(dict(existing_request)),
                "idempotent_reuse": True,
                "dispatch_required": False,
                "stage": self.show_stage(selected),
            }

        if record.get("execution_evidence_complete") is True:
            raise StageControllerError(
                "engineering retry cannot replace an already committed Stage result"
            )
        latest_result = record.get("latest_result")
        previous_digest = previous_attempt.get("result_digest")
        if (
            isinstance(latest_result, Mapping)
            and isinstance(previous_digest, str)
            and sha256_json(_safe_state_copy(dict(latest_result))) != previous_digest
        ):
            raise StageControllerError(
                "engineering retry target is no longer the latest execution failure"
            )

        # Do not permit another request while any other execution attempt is
        # still in flight.  The normal request_executor path retains its
        # historical PRIMARY/CHALLENGER parallel-lane behavior.
        for attempt in record.get("execution_attempts", []):
            if not isinstance(attempt, Mapping):
                continue
            if attempt.get("request_id") == previous_request_id:
                continue
            if self._attempt_is_inflight(attempt):
                raise StageControllerError("engineering retry requires no in-flight executor attempt")

        retry_budget = _contract_retry_budget(record["contract"])
        requests_in_iteration = sum(
            1
            for item in record.get("executor_requests", [])
            if isinstance(item, Mapping) and item.get("iteration_index") == previous_iteration
        )
        if requests_in_iteration >= retry_budget:
            record["open_iteration_index"] = None
            record["status"] = StageState.BLOCKED.value
            if self._active_stage_id == selected:
                self._active_stage_id = None
            self._append_event(
                "engineering_retry_blocked",
                selected,
                from_status=StageState.ACTIVE.value,
                to_status=StageState.BLOCKED.value,
                details={
                    "decision": "BLOCKED",
                    "reason": "EXECUTION_RETRY_BUDGET_EXHAUSTED",
                    "previous_request_id": previous_request_id,
                    "iteration_index": previous_iteration,
                    "attempt_count": record.get("attempt_count", len(record.get("execution_attempts", []))),
                    "retry_budget": retry_budget,
                    "stage_iteration_advanced": False,
                },
            )
            raise StageControllerError(
                f"execution retry budget exhausted for Stage iteration {previous_iteration}"
            )

        attempt_index = requests_in_iteration + 1
        mode = _normalise_mode(previous_request.get("mode"), "mode")
        lane = previous_request.get("lane", "PRIMARY")
        if not isinstance(lane, str) or lane.strip().upper() not in {"PRIMARY", "CHALLENGER"}:
            raise ContractValidationError("previous executor request has an invalid lane")
        lane = lane.strip().upper()
        request = self._build_executor_request(
            record,
            selected,
            iteration_index=previous_iteration,
            attempt_index=attempt_index,
            mode=mode,
            lane=lane,
            objective=objective_text,
            retry_of_request_id=previous_request_id,
        )
        record["open_iteration_index"] = previous_iteration
        record["executor_requests"].append(copy.deepcopy(request))
        record["attempt_count"] = len(record["executor_requests"])
        record["retry_count"] = max(
            0,
            sum(
                1
                for item in record["executor_requests"]
                if isinstance(item, Mapping) and item.get("iteration_index") == previous_iteration
            )
            - 1,
        )
        record.setdefault("execution_attempts", []).append(
            {
                "attempt_index": attempt_index,
                "iteration_index": previous_iteration,
                "request_id": request["request_id"],
                "status": "REQUESTED",
                "outcome": "REQUESTED",
                "request": _safe_state_copy(request),
            }
        )
        event = self._append_event(
            "engineering_retry_requested",
            selected,
            from_status=StageState.ACTIVE.value,
            to_status=StageState.ACTIVE.value,
            details={
                "decision": "ENGINEERING_RETRY",
                "previous_request_id": previous_request_id,
                "request_id": request["request_id"],
                "iteration_index": previous_iteration,
                "attempt_index": attempt_index,
                "retry_budget": retry_budget,
                "objective": objective_text,
                "rationale": rationale,
                "failure_kind": failure_marker.get("kind"),
                "failure_code": failure_marker.get("code"),
                "stage_iteration_advanced": False,
            },
        )
        return {
            "event": event,
            "request": request,
            "idempotent_reuse": False,
            "dispatch_required": True,
            "stage": self.show_stage(selected),
        }

    def create_executor_task(self, stage_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self.request_executor(stage_id, **kwargs)

    request_executor_task = create_executor_task
    executor_request = request_executor

    def request_consultation(
        self,
        stage_id: str | None = None,
        *,
        mode: str = "NORMAL",
        evidence_digest: str | None = None,
        reason: str = "",
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Authorize one consultation request; never calls a bridge."""

        selected, record = self._require_active(stage_id)
        if not isinstance(mode, str) or mode.strip().upper() not in {"NORMAL", "FRESH"}:
            raise ContractValidationError("consultation mode must be NORMAL or FRESH")
        normalized_mode = mode.strip().upper()
        digest = _text(evidence_digest, "evidence_digest", max_length=256)
        reason = _text(reason, "reason", required=False)
        key = f"{normalized_mode}:{digest}"
        if key in record["consulted_evidence"]:
            raise StageControllerError("the same evidence digest cannot be consulted twice in one mode")
        if conversation_id is not None:
            conversation_id = _text(conversation_id, "conversation_id", max_length=256)
        body = {
            "stage_id": selected,
            "mode": normalized_mode,
            "evidence_digest": digest,
            "reason": reason,
            "conversation_id": conversation_id,
        }
        request = {
            "request_id": "consultation-" + sha256_json(body)[:16],
            "request_type": "consultation",
            **body,
            "baseline_digest": record["baseline_digest"],
            "stage_goal": record["contract"]["stage_goal"],
            "user_visible_goal": record["contract"]["user_visible_goal"],
        }
        record["consulted_evidence"].append(key)
        record["consultation_requests"].append(copy.deepcopy(request))
        event = self._append_event(
            "consultation_requested", selected, from_status=record["status"], to_status=record["status"],
            details={
                "request_id": request["request_id"], "mode": normalized_mode,
                "evidence_digest": digest, "conversation_id": conversation_id,
            },
        )
        return {"event": event, "request": request, "stage": self.show_stage(selected)}

    consult_gpt = request_consultation
    request_gpt_consultation = request_consultation

    def _resolve_execution_attempt(
        self,
        record: Mapping[str, Any],
        result: Mapping[str, Any],
        request_id: str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Resolve a result to its immutable request and mutable receipt."""

        requests = [item for item in record.get("executor_requests", []) if isinstance(item, Mapping)]
        candidate_id = request_id
        if candidate_id is None:
            for key in ("request_id", "attempt_request_id", "execution_request_id"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    candidate_id = value.strip()
                    break
        selected_request: dict[str, Any] | None = None
        if candidate_id is not None:
            for item in reversed(requests):
                if item.get("request_id") == candidate_id:
                    selected_request = dict(item)
                    break
            if selected_request is None:
                raise StageControllerError("executor result references an unknown request")
        elif requests:
            # A provider result predating explicit request binding may omit the
            # request id.  Resolve only to the most recent attempt that has not
            # received a terminal receipt; this remains deterministic and does
            # not invent a new Stage iteration.
            receipts = record.get("execution_attempts", [])
            terminal_ids = {
                item.get("request_id")
                for item in receipts
                if isinstance(item, Mapping)
                and item.get("status") not in {None, "REQUESTED"}
                and item.get("outcome") not in {None, "REQUESTED"}
            }
            for item in reversed(requests):
                if item.get("request_id") not in terminal_ids:
                    selected_request = dict(item)
                    break
            if selected_request is None:
                # Keep the old API's useful single-result behavior.  A caller
                # that has multiple completed requests must bind explicitly.
                if len(requests) == 1:
                    selected_request = dict(requests[0])
                else:
                    raise StageControllerError("executor result requires request_id after multiple attempts")

        selected_receipt: dict[str, Any] | None = None
        if selected_request is not None:
            selected_id = selected_request.get("request_id")
            selected_attempt = selected_request.get("attempt_index")
            receipts = record.get("execution_attempts", [])
            if isinstance(receipts, list):
                for item in reversed(receipts):
                    if not isinstance(item, Mapping):
                        continue
                    if item.get("request_id") == selected_id and (
                        selected_attempt is None or item.get("attempt_index") == selected_attempt
                    ):
                        selected_receipt = item  # type: ignore[assignment]
                        break
        return selected_request, selected_receipt

    @staticmethod
    def _attempt_provenance(result: Mapping[str, Any]) -> dict[str, Any]:
        """Extract bounded provider/adapter provenance for an attempt receipt."""

        provenance: dict[str, Any] = {}
        for field in (
            "provider_id", "provider", "model", "executable", "adapter", "runner",
            "provider_request_id", "receipt", "receipt_path", "evidence_refs", "measurements", "provenance",
        ):
            if field in result:
                provenance[field] = _safe_state_copy(result[field])
        # ``record_executor_failure`` nests adapter/provider details under a
        # provenance object so the failure marker remains provider-neutral.
        # Copy the bounded conventional fields to the receipt root as well;
        # this makes provider/model/executable provenance directly queryable
        # without discarding the original nested shape.
        nested = result.get("provenance")
        if isinstance(nested, Mapping):
            for field in (
                "provider_id", "provider", "model", "executable", "adapter", "runner",
                "provider_request_id", "receipt", "receipt_path",
            ):
                if field in nested and field not in provenance:
                    provenance[field] = _safe_state_copy(nested[field])
        # Native Codex receipts carry model/executable details inside their
        # bounded measurement namespace.  Promote conventional provenance
        # fields so a reconciled historical artifact remains easy to audit.
        measurements = result.get("measurements")
        if isinstance(measurements, Mapping):
            for measurement in measurements.values():
                if not isinstance(measurement, Mapping):
                    continue
                for field in (
                    "provider_id", "provider", "model", "executable", "provider_request_id",
                ):
                    if field in measurement and field not in provenance:
                        provenance[field] = _safe_state_copy(measurement[field])
        return provenance

    def _update_attempt_receipt(
        self,
        record: dict[str, Any],
        result: Mapping[str, Any],
        *,
        request: Mapping[str, Any] | None,
        receipt: dict[str, Any] | None,
        status: str,
        outcome: str,
        decision: str | None,
        failure: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist one bounded attempt receipt while preserving the request."""

        if not isinstance(record.get("execution_attempts"), list):
            record["execution_attempts"] = []
        if receipt is None:
            attempt_index = (
                request.get("attempt_index")
                if isinstance(request, Mapping) and isinstance(request.get("attempt_index"), int)
                else int(record.get("attempt_count", 0)) or 1
            )
            receipt = {
                "attempt_index": attempt_index,
                "iteration_index": (
                    request.get("iteration_index")
                    if isinstance(request, Mapping)
                    else record.get("iteration_index", 0) + 1
                ),
                "request_id": request.get("request_id") if isinstance(request, Mapping) else None,
                "request": _safe_state_copy(request) if isinstance(request, Mapping) else None,
            }
            record["execution_attempts"].append(receipt)
        # Keep both names on persisted receipts.  ``iteration_index`` is the
        # historical wire spelling; ``stage_iteration_index`` makes the
        # separation from the execution attempt coordinate explicit for new
        # consumers and migrations.
        if "stage_iteration_index" not in receipt:
            receipt["stage_iteration_index"] = receipt.get("iteration_index")
        if "attempt_number" not in receipt:
            receipt["attempt_number"] = receipt.get("attempt_index")
        # ``attempt_count`` is the lifetime number of real attempts.  Do not
        # compare it with the iteration-scoped attempt_index when a later
        # Stage iteration starts again at attempt 1.
        record["attempt_count"] = max(
            int(record.get("attempt_count", 0) or 0),
            len(record.get("execution_attempts", [])),
        )
        receipt["status"] = status
        receipt["outcome"] = outcome
        if decision is not None:
            receipt["decision"] = decision
        safe_result = _safe_state_copy(dict(result))
        receipt["result_digest"] = sha256_json(safe_result)
        receipt["result"] = safe_result
        provenance = self._attempt_provenance(result)
        if provenance:
            receipt["provenance"] = provenance
        if failure is not None:
            receipt["execution_failure"] = _safe_state_copy(dict(failure))
        return receipt

    def _record_invalid_attempt(
        self,
        selected: str,
        record: dict[str, Any],
        result: Mapping[str, Any],
        *,
        request: Mapping[str, Any],
        receipt: dict[str, Any] | None,
        request_id: str | None,
        failure_code: str,
    ) -> dict[str, Any]:
        """Persist a malformed result as a terminal attempt receipt.

        Validation failures happen after the provider boundary has consumed an
        execution request.  Persisting the bounded invalid receipt before
        raising keeps the attempt auditable and prevents a caller from
        replaying the same request as if it were still pending.  The Stage
        iteration remains open so a fresh bounded retry can be requested.
        """

        failure = {
            "kind": "CONTRACT_FAILURE",
            "code": failure_code,
            "retryable": False,
        }
        attempt = self._update_attempt_receipt(
            record,
            result,
            request=request,
            receipt=receipt,
            status="INVALID",
            outcome="EXECUTION_ATTEMPT_INVALID",
            decision="EXECUTION_ATTEMPT_INVALID",
            failure=failure,
        )
        event = self._append_event(
            "executor_result",
            selected,
            from_status=record["status"],
            to_status=record["status"],
            details={
                "request_id": request_id,
                "decision": "EXECUTION_ATTEMPT_INVALID",
                "failure_code": failure_code,
                "iteration_index": request.get("iteration_index"),
                "attempt_index": attempt.get("attempt_index"),
                "retry_budget": record.get("retry_budget", _contract_retry_budget(record["contract"])),
                "stage_iteration_advanced": False,
                "receipt_digest": attempt.get("result_digest"),
            },
        )
        return {"attempt": attempt, "event": event}

    def _bind_stage_result(
        self,
        record: dict[str, Any],
        result: Mapping[str, Any],
        *,
        stage_iteration: int,
        request: Mapping[str, Any] | None,
        legacy_result_iteration: int | None = None,
    ) -> dict[str, Any]:
        """Bind a valid result to a Stage iteration without rewriting its body."""

        safe_result = _safe_state_copy(dict(result))
        result_digest = sha256_json(safe_result)
        attempt_index = request.get("attempt_index") if isinstance(request, Mapping) else None
        request_id = request.get("request_id") if isinstance(request, Mapping) else None
        if attempt_index is None and isinstance(request_id, str):
            # Legacy request bodies predate the explicit attempt field.  The
            # reconciled receipt is authoritative for the scoped attempt
            # number, while the immutable historical request remains intact.
            for attempt in reversed(record.get("execution_attempts", [])):
                if (
                    isinstance(attempt, Mapping)
                    and attempt.get("request_id") == request_id
                    and isinstance(attempt.get("attempt_index"), int)
                ):
                    attempt_index = attempt["attempt_index"]
                    break
        binding: dict[str, Any] = {
            "schema_version": "stage_result_binding.v1",
            "stage_id": record["contract"]["stage_id"],
            "iteration_index": stage_iteration,
            "attempt_index": attempt_index,
            "request_id": request_id,
            "result_digest": result_digest,
            "evidence_complete": True,
        }
        source_iteration = result.get("iteration_index")
        if isinstance(source_iteration, int):
            binding["source_result_iteration_index"] = source_iteration
        if legacy_result_iteration is not None:
            binding["legacy_result_iteration_index"] = legacy_result_iteration
            binding["reconciled"] = True
        record["latest_stage_result"] = {
            **binding,
            "result": safe_result,
        }
        record["stage_result_binding"] = copy.deepcopy(binding)
        record["execution_evidence_complete"] = True
        return binding

    def repair_evidence_binding(
        self,
        stage_id: str | None = None,
        *,
        evidence_refs: Sequence[str],
        rationale: str = "",
    ) -> dict[str, Any]:
        """Repair only the external evidence references for the latest result.

        The execution result and its immutable digest remain unchanged.  This
        is deliberately separate from ``request_executor``: a context-pack
        validation failure must not consume another execution attempt.
        """
        selected = self._select_stage_id(stage_id)
        assert selected is not None
        record = self._stages[selected]
        if record.get("status") != StageState.ACTIVE.value:
            raise StageControllerError("evidence repair requires an ACTIVE Stage")
        latest = record.get("latest_result")
        binding = record.get("stage_result_binding")
        if not isinstance(latest, Mapping) or not isinstance(binding, Mapping):
            raise StageControllerError("no bound execution result is available for evidence repair")
        if binding.get("evidence_complete") is not True:
            raise StageControllerError("execution evidence binding is incomplete")
        refs = []
        for value in evidence_refs:
            if not isinstance(value, str) or not value.strip() or value.strip().startswith(("/", "\\")) or ":" in value.split("/")[0]:
                raise StageControllerError("evidence references must be workspace-relative")
            normalized = value.strip().replace("\\", "/")
            if normalized.startswith("../") or "/../" in normalized or normalized == "..":
                raise StageControllerError("evidence reference escapes the workspace")
            refs.append(normalized)
        if not refs:
            raise StageControllerError("at least one repaired evidence reference is required")
        original_digest = binding.get("result_digest")
        record["evidence_reference_repair"] = {
            "schema_version": "evidence_reference_repair.v1",
            "stage_id": selected,
            "original_result_digest": original_digest,
            "repaired_evidence_refs": list(dict.fromkeys(refs)),
            "rationale": _text(rationale, "rationale", required=False),
        }
        return {"event": self._append_event(
            "evidence_binding_repaired", selected,
            from_status=record["status"], to_status=record["status"],
            details={"original_result_digest": original_digest, "repaired_evidence_refs": list(dict.fromkeys(refs))},
        ), "stage": self.show_stage(selected)}

    def _record_attempt_failure(
        self,
        selected: str,
        record: dict[str, Any],
        result: Mapping[str, Any],
        *,
        request: Mapping[str, Any] | None,
        receipt: dict[str, Any] | None,
        request_id: str | None,
        status: str,
        failure: Mapping[str, Any],
    ) -> dict[str, Any]:
        kind = str(failure.get("kind", "EXECUTION_FAILURE"))
        decision = "EXECUTION_PROVIDER_FAILURE" if kind == "PROVIDER_FAILURE" else "EXECUTION_ATTEMPT_FAILURE"
        attempt_receipt = self._update_attempt_receipt(
            record,
            result,
            request=request,
            receipt=receipt,
            status=status,
            outcome=decision,
            decision=decision,
            failure=failure,
        )
        record["latest_result"] = _safe_state_copy(dict(result))
        event = self._append_event(
            "executor_result",
            selected,
            from_status=record["status"],
            to_status=record["status"],
            details={
                "request_id": request_id,
                "decision": decision,
                "failure_code": failure.get("code"),
                "failure_kind": kind,
                "iteration_index": (
                    request.get("iteration_index") if isinstance(request, Mapping)
                    else self._open_stage_iteration(record)
                ),
                "attempt_index": attempt_receipt.get("attempt_index"),
                "retry_budget": record.get("retry_budget", _contract_retry_budget(record["contract"])),
                "stage_iteration_advanced": False,
                "receipt_digest": attempt_receipt.get("result_digest"),
            },
        )
        return {
            "event": event,
            "decision": decision,
            "execution_failure": _safe_state_copy(dict(failure)),
            "stage": self.show_stage(selected),
        }

    def record_executor_result(
        self,
        result: Mapping[str, Any],
        *,
        stage_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Record one execution attempt and, when valid, advance its Stage.

        A request is an execution attempt.  Its ``iteration_index`` is stable
        for every retry in the same open Stage iteration.  Only a valid
        successful Stage result advances ``record['iteration_index']``;
        provider/adapter/runner/contract failures remain receipts on the
        current iteration.
        """

        selected, record = self._require_active(stage_id)
        if not isinstance(result, Mapping):
            raise ContractValidationError("executor result must be an object")
        detached_result = copy.deepcopy(dict(result))
        self._validate_goal_immutability(detached_result, record["contract"])
        result_stage_id = detached_result.get("stage_id")
        if result_stage_id is not None and result_stage_id != selected:
            raise ContractValidationError("executor result stage_id does not match the active Stage")
        if request_id is not None:
            request_id = _text(request_id, "request_id", max_length=256)
        request, attempt_receipt = self._resolve_execution_attempt(record, detached_result, request_id)
        if request is None:
            # Keep the historical no-request validation behavior for callers
            # that submit a result before any execution request exists.  A
            # receipt is only created for a real immutable request; accepting
            # an unbound payload here would fabricate an execution attempt.
            raise ContractValidationError("executor result references no execution request")
        request_id = request.get("request_id") if isinstance(request, Mapping) else request_id
        if isinstance(attempt_receipt, Mapping) and (
            attempt_receipt.get("status") not in {None, "REQUESTED", "HISTORICAL"}
            or attempt_receipt.get("outcome") not in {None, "REQUESTED", "HISTORICAL_REQUEST"}
        ):
            raise StageControllerError("executor result has already been recorded for this attempt")
        expected_iteration = (
            request.get("iteration_index") if isinstance(request, Mapping)
            else self._open_stage_iteration(record)
        )
        if not isinstance(expected_iteration, int) or expected_iteration < 1:
            raise StageControllerError("execution attempt has an invalid Stage iteration")
        supplied_iteration = detached_result.get("iteration_index")
        if supplied_iteration is not None and supplied_iteration != expected_iteration:
            # A mismatched result is still a real attempt receipt when it was
            # bound to a known request.  The result itself is never rewritten.
            self._record_invalid_attempt(
                selected,
                record,
                detached_result,
                request=request,
                receipt=attempt_receipt,
                request_id=request_id,
                failure_code="ITERATION_BINDING_MISMATCH",
            )
            raise ContractValidationError("executor result iteration_index does not match its request")
        if expected_iteration != self._open_stage_iteration(record):
            raise StageControllerError("executor result targets a Stage iteration that is no longer open")

        status = str(detached_result.get("status", "SUCCEEDED")).strip().upper()
        allowed_statuses = _SUCCESS_STATUSES | _FAILURE_STATUSES | {"BLOCKED"}
        if status not in allowed_statuses:
            self._record_invalid_attempt(
                selected,
                record,
                detached_result,
                request=request,
                receipt=attempt_receipt,
                request_id=request_id,
                failure_code="EXECUTION_STATUS_INVALID",
            )
            raise ContractValidationError("unknown executor result status; refusing to infer a decision")

        failure = _execution_failure_from_result(detached_result, status)
        # A legacy bare BLOCKED result is an explicit Stage stop.  BLOCKED
        # carrying timeout/provider/runner evidence is a retryable attempt and
        # must remain within the current iteration.
        if failure is not None:
            return self._record_attempt_failure(
                selected,
                record,
                detached_result,
                request=request,
                receipt=attempt_receipt,
                request_id=request_id,
                status=status,
                failure=failure,
            )
        if status == "BLOCKED":
            attempt = self._update_attempt_receipt(
                record,
                detached_result,
                request=request,
                receipt=attempt_receipt,
                status=status,
                outcome="BLOCKED",
                decision="BLOCKED",
            )
            record["latest_result"] = _safe_state_copy(dict(detached_result))
            record["open_iteration_index"] = None
            record["status"] = StageState.BLOCKED.value
            self._active_stage_id = None
            event = self._append_event(
                "executor_result",
                selected,
                from_status=StageState.ACTIVE.value,
                to_status=StageState.BLOCKED.value,
                details={
                    "request_id": request_id,
                    "decision": "BLOCKED",
                    "iteration_index": expected_iteration,
                    "attempt_index": attempt.get("attempt_index"),
                    "stage_iteration_advanced": False,
                },
            )
            return {"event": event, "decision": "BLOCKED", "stage": self.show_stage(selected)}

        # Preserve the historical substantive-failure decision for callers
        # that submit a bare FAILED result, while still keeping its attempt
        # receipt.  Explicit contract/turn/provider failures took the path
        # above and did not advance the Stage.
        decision = "REPLAN" if status in _FAILURE_STATUSES else (
            "STAGE_READY" if bool(detached_result.get("stage_ready", False)) else "CONTINUE"
        )
        attempt = self._update_attempt_receipt(
            record,
            detached_result,
            request=request,
            receipt=attempt_receipt,
            status=status,
            outcome=decision,
            decision=decision,
        )
        if decision == "STAGE_READY":
            try:
                candidate, _, _ = self._ready_evidence(record, detached_result, None, None)
            except (StageControllerError, ContractValidationError) as exc:
                # The provider turn happened, so retain its failed contract
                # receipt even though no Stage progress is committed.
                attempt["status"] = "INVALID"
                attempt["outcome"] = "EXECUTION_ATTEMPT_INVALID"
                attempt["decision"] = "EXECUTION_ATTEMPT_INVALID"
                attempt["execution_failure"] = {
                    "kind": "CONTRACT_FAILURE",
                    "code": "STAGE_RESULT_INCOMPLETE",
                    "retryable": True,
                }
                # Keep the public latest-result slot unchanged when readiness
                # validation fails before any result has been accepted.  The
                # full attempted payload and digest remain in the attempt
                # receipt for audit/retry purposes.
                if record.get("latest_result") is not None:
                    record["latest_result"] = _safe_state_copy(dict(detached_result))
                self._append_event(
                    "executor_result",
                    selected,
                    from_status=record["status"],
                    to_status=record["status"],
                    details={
                        "request_id": request_id,
                        "decision": "EXECUTION_ATTEMPT_INVALID",
                        "failure_code": "STAGE_RESULT_INCOMPLETE",
                        "iteration_index": expected_iteration,
                        "attempt_index": attempt.get("attempt_index"),
                        "stage_iteration_advanced": False,
                        "receipt_digest": attempt.get("result_digest"),
                    },
                )
                raise exc
            record["iteration_index"] = expected_iteration
            record["open_iteration_index"] = None
            record["latest_result"] = candidate
            self._bind_stage_result(
                record,
                candidate,
                stage_iteration=expected_iteration,
                request=request,
            )
            return self.mark_stage_ready(
                stage_id=selected,
                result=candidate,
                _already_recorded=True,
                request_id=request_id,
            )

        record["iteration_index"] = expected_iteration
        record["open_iteration_index"] = None
        record["latest_result"] = _safe_state_copy(dict(detached_result))
        if status in _SUCCESS_STATUSES:
            self._bind_stage_result(
                record,
                detached_result,
                stage_iteration=expected_iteration,
                request=request,
            )
        else:
            record["execution_evidence_complete"] = False
        event = self._append_event(
            "executor_result",
            selected,
            from_status=record["status"],
            to_status=record["status"],
            details={
                "request_id": request_id,
                "decision": decision,
                "iteration_index": expected_iteration,
                "attempt_index": attempt.get("attempt_index"),
                "stage_iteration_advanced": True,
                "receipt_digest": attempt.get("result_digest"),
            },
        )
        return {"event": event, "decision": decision, "stage": self.show_stage(selected)}

    submit_result = record_executor_result
    submit_executor_result = record_executor_result

    def record_executor_failure(
        self,
        failure: Mapping[str, Any] | str,
        *,
        stage_id: str | None = None,
        request_id: str | None = None,
        provider: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record an adapter/runner failure as a bounded execution attempt.

        This method is used when an adapter raises before it can construct a
        normal ``codex_result``.  The request and failure provenance are still
        recorded, and the Stage remains on its current iteration.
        """

        selected, record = self._require_active(stage_id)
        if isinstance(failure, str):
            failure_value: Any = {
                "kind": "EXECUTION_FAILURE",
                "code": failure,
                "retryable": True,
            }
        else:
            failure_value = failure
        normalized = _normalise_execution_failure(failure_value)
        if normalized is None:
            raise ContractValidationError("execution failure must contain a supported bounded kind and code")
        if provider is not None:
            if not isinstance(provider, Mapping):
                raise ContractValidationError("provider provenance must be an object")
            normalized = {**normalized, "provenance": _safe_state_copy(dict(provider))}
        request, receipt = self._resolve_execution_attempt(record, {}, request_id)
        if request is None:
            # A failure without an immutable request cannot be represented as
            # a legitimate attempt.  Refuse it instead of incrementing a
            # synthetic attempt counter or consuming retry budget.
            raise ContractValidationError("execution failure references no execution request")
        if isinstance(receipt, Mapping) and (
            receipt.get("status") not in {None, "REQUESTED", "HISTORICAL"}
            or receipt.get("outcome") not in {None, "REQUESTED", "HISTORICAL_REQUEST"}
        ):
            raise StageControllerError("executor result has already been recorded for this attempt")
        expected_iteration = (
            request.get("iteration_index") if isinstance(request, Mapping)
            else self._open_stage_iteration(record)
        )
        result: dict[str, Any] = {
            "stage_id": selected,
            "iteration_index": expected_iteration,
            "status": "ERROR",
            "summary": "Execution adapter or runner failed before producing a contract result.",
            "execution_failure": normalized,
        }
        result["provenance"] = _safe_state_copy(dict(provider)) if isinstance(provider, Mapping) else {}
        return self._record_attempt_failure(
            selected,
            record,
            result,
            request=request,
            receipt=receipt,
            request_id=request.get("request_id") if isinstance(request, Mapping) else request_id,
            status="ERROR",
            failure=normalized,
        )

    submit_executor_failure = record_executor_failure
    record_attempt_failure = record_executor_failure

    def record_technical_review(
        self,
        review: Mapping[str, Any],
        *,
        stage_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a GPT Technical Review and advance only on explicit demand.

        A review must carry ``requires_next_iteration=true`` and one of the
        bounded technical-modification decisions.  Ordinary ``CONTINUE`` or
        ``REPLAN`` text cannot advance a Stage by itself.
        """

        selected, record = self._require_active(stage_id)
        if not isinstance(review, Mapping):
            raise ContractValidationError("technical review must be an object")
        checked = _safe_state_copy(dict(review))
        decision = checked.get("decision", checked.get("outcome"))
        if not isinstance(decision, str):
            raise ContractValidationError("technical review decision is required")
        normalized = decision.strip().upper()
        allowed = {
            "NEXT_ITERATION",
            "TECHNICAL_MODIFICATION_REQUIRED",
            "REPLAN_NEXT_ITERATION",
            "REPLAN",
        }
        if normalized not in allowed:
            raise ContractValidationError("technical review does not request a next Stage iteration")
        if checked.get("requires_next_iteration") is not True:
            raise ContractValidationError(
                "technical review must explicitly set requires_next_iteration=true"
            )
        used_iteration = max(record["iteration_index"], record.get("open_iteration_index") or 0)
        target = used_iteration + 1
        maximum = record["contract"].get("max_iterations")
        if maximum is not None and target > maximum:
            raise StageControllerError("technical review exceeds max_iterations")
        review_id = checked.get("review_id", checked.get("consultation_id"))
        if review_id is not None:
            review_id = _text(review_id, "review_id", max_length=256)
        rationale = checked.get("rationale", checked.get("reason", ""))
        rationale = _text(rationale, "rationale", required=False, max_length=2000)
        entry = {
            "review_type": "GPT_TECHNICAL_REVIEW",
            "decision": normalized,
            "requires_next_iteration": True,
            "from_iteration_index": used_iteration,
            "to_iteration_index": target,
            "review_id": review_id,
            "rationale": rationale,
        }
        record["reviews"].append(copy.deepcopy(entry))
        previous = record["iteration_index"]
        record["iteration_index"] = target
        record["open_iteration_index"] = target
        record["latest_stage_result"] = None
        record["stage_result_binding"] = None
        record["execution_evidence_complete"] = False
        record["retry_count"] = 0
        event = self._append_event(
            "technical_review",
            selected,
            from_status=record["status"],
            to_status=record["status"],
            details={
                **entry,
                "stage_iteration_advanced": True,
                "iteration_index": target,
            },
        )
        return {"event": event, "decision": normalized, "stage": self.show_stage(selected)}

    apply_technical_review = record_technical_review
    record_gpt_technical_review = record_technical_review

    def advance_stage_iteration(
        self,
        *,
        stage_id: str | None = None,
        review: Mapping[str, Any] | None = None,
        rationale: str = "",
        review_id: str | None = None,
    ) -> dict[str, Any]:
        """Explicit convenience wrapper for a GPT-requested next iteration."""

        payload: dict[str, Any] = dict(review or {})
        payload.setdefault("decision", "TECHNICAL_MODIFICATION_REQUIRED")
        payload["requires_next_iteration"] = True
        if rationale:
            payload["rationale"] = rationale
        if review_id is not None:
            payload["review_id"] = review_id
        return self.record_technical_review(payload, stage_id=stage_id)

    next_stage_iteration = advance_stage_iteration

    def reconcile_retry_semantics(
        self,
        stage_id: str | None = None,
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        """Migrate legacy request histories into explicit attempt receipts.

        The migration is append-only with respect to historical requests and
        events.  It may correct the derived current Stage counter when an old
        adapter artificially rebound a provider request from iteration 1 to 2;
        the original result remains byte-for-byte represented in
        ``latest_result`` and its source index is recorded in the new binding.
        """

        selected = self._select_stage_id(stage_id)
        assert selected is not None
        record = self._stages[selected]
        requests = [item for item in record.get("executor_requests", []) if isinstance(item, Mapping)]
        old_iteration = record.get("iteration_index", 0)
        if not isinstance(old_iteration, int) or old_iteration < 0:
            raise StageControllerError("persisted Stage iteration_index is invalid")
        # ``_load_snapshot`` supplies compatibility defaults for legacy
        # records, so the field value alone cannot tell whether migration has
        # actually happened.  Retain that provenance privately until this
        # explicit reconciliation has rebuilt receipts.
        had_semantics = (
            selected not in self._legacy_retry_stage_ids
            and record.get("retry_semantics_version") == RETRY_SEMANTICS_VERSION
        )
        existing = record.get("execution_attempts")
        if not isinstance(existing, list):
            existing = []
            record["execution_attempts"] = existing

        # Build one receipt per historical request, preserving duplicate legacy
        # request ids and every request body exactly as loaded.
        by_request: dict[str, list[dict[str, Any]]] = {}
        for item in existing:
            if isinstance(item, Mapping) and isinstance(item.get("request_id"), str):
                by_request.setdefault(item["request_id"], []).append(item)  # type: ignore[arg-type]
        event_results: dict[str, list[Mapping[str, Any]]] = {}
        for event in record.get("events", []):
            if not isinstance(event, Mapping) or event.get("event") != "executor_result":
                continue
            details = event.get("details")
            if isinstance(details, Mapping) and isinstance(details.get("request_id"), str):
                event_results.setdefault(details["request_id"], []).append(details)
        rebuilt: list[dict[str, Any]] = []
        used_existing: set[int] = set()
        iteration_attempts: dict[int, int] = {}
        for request in requests:
            request_iteration = request.get("iteration_index", 1)
            if not isinstance(request_iteration, int) or request_iteration < 1:
                request_iteration = 1
            iteration_attempts[request_iteration] = iteration_attempts.get(request_iteration, 0) + 1
            attempt_index = iteration_attempts[request_iteration]
            request_id_value = request.get("request_id")
            matched: dict[str, Any] | None = None
            for existing_index, item in enumerate(existing):
                if existing_index in used_existing or not isinstance(item, Mapping):
                    continue
                if item.get("request_id") == request_id_value and item.get("attempt_index") == attempt_index:
                    matched = item  # type: ignore[assignment]
                    used_existing.add(existing_index)
                    break
            if matched is None:
                matched = {
                    "attempt_index": attempt_index,
                    "iteration_index": request_iteration,
                    "stage_iteration_index": request_iteration,
                    "attempt_number": attempt_index,
                    "request_id": request_id_value,
                    "status": "HISTORICAL",
                    "outcome": "HISTORICAL_REQUEST",
                    "request": _safe_state_copy(dict(request)),
                    "historical": True,
                }
                details_list = event_results.get(str(request_id_value), [])
                if details_list:
                    detail = details_list.pop(0)
                    matched["outcome"] = detail.get("decision", "HISTORICAL_RESULT")
                    matched["decision"] = detail.get("decision")
                    matched["failure_code"] = detail.get("failure_code")
                    matched["historical_event_revision"] = detail.get("revision")
            else:
                matched = copy.deepcopy(dict(matched))
                matched.setdefault("historical", True)
                matched.setdefault("request", _safe_state_copy(dict(request)))
                matched.setdefault("attempt_index", attempt_index)
                matched.setdefault("iteration_index", request_iteration)
                matched.setdefault("stage_iteration_index", request_iteration)
                matched.setdefault("attempt_number", attempt_index)
            # Legacy event details recorded the stable decision/code pair but
            # predated the explicit execution_failure object.  Reconstruct a
            # bounded marker in the new receipt while leaving the event body
            # untouched.  A substantive REPLAN remains a valid Stage result
            # and therefore is intentionally not classified as infrastructure
            # failure here.
            decision_value = matched.get("decision", matched.get("outcome"))
            failure_code_value = matched.get("failure_code")
            if (
                "execution_failure" not in matched
                and isinstance(failure_code_value, str)
                and failure_code_value.strip()
                and decision_value in {"EXECUTION_PROVIDER_FAILURE", "EXECUTION_ATTEMPT_FAILURE"}
            ):
                matched["execution_failure"] = {
                    "kind": (
                        "PROVIDER_FAILURE"
                        if decision_value == "EXECUTION_PROVIDER_FAILURE"
                        else "EXECUTION_FAILURE"
                    ),
                    "code": failure_code_value.strip()[:128],
                    "retryable": True,
                }
            rebuilt.append(matched)
        # Preserve any receipt whose immutable request was compacted from the
        # legacy request log.  Such an orphan is retained for audit only and
        # is never selected for a new result binding.
        for existing_index, item in enumerate(existing):
            if existing_index in used_existing or not isinstance(item, Mapping):
                continue
            orphan = copy.deepcopy(dict(item))
            orphan.setdefault("historical", True)
            orphan["orphaned"] = True
            rebuilt.append(orphan)
        record["execution_attempts"] = rebuilt
        record["attempt_count"] = max(len(requests), int(record.get("attempt_count", 0) or 0))
        record["retry_semantics_version"] = RETRY_SEMANTICS_VERSION
        record["retry_budget"] = _contract_retry_budget(record["contract"])
        # A legacy controller may have advanced the Stage because it rebound a
        # successful provider result to a new iteration.  Derive completed
        # Stage iterations only from matching successful request/result pairs.
        # Retain a legacy committed counter unless the latest artifact proves
        # that the old controller rebound a provider result to the wrong
        # iteration.  Quietly lowering an otherwise consistent counter would
        # fabricate a migration decision of its own.
        legal_completed = old_iteration
        latest = record.get("latest_result")
        latest_request: Mapping[str, Any] | None = None
        latest_request_id = latest.get("task_id") if isinstance(latest, Mapping) else None
        if isinstance(latest, Mapping):
            binding = latest.get("adapter_iteration_binding")
            if isinstance(binding, Mapping):
                candidate_id = binding.get("provider_request_id")
                if isinstance(candidate_id, str):
                    for request in reversed(requests):
                        if request.get("request_id") == candidate_id:
                            latest_request = request
                            break
            if latest_request is None and isinstance(latest_request_id, str):
                for request in reversed(requests):
                    if request.get("request_id") == latest_request_id:
                        latest_request = request
                        break
            source_iteration = latest.get("iteration_index")
            request_iteration = latest_request.get("iteration_index") if latest_request else None
            existing_binding = record.get("stage_result_binding")
            already_bound = (
                isinstance(existing_binding, Mapping)
                and existing_binding.get("evidence_complete") is True
                and existing_binding.get("request_id") == (
                    latest_request.get("request_id") if isinstance(latest_request, Mapping) else None
                )
                and existing_binding.get("iteration_index") == request_iteration
            )
            artificial_rebind = (
                isinstance(binding, Mapping)
                and binding.get("provider_request_iteration_index") != binding.get("controller_result_iteration_index")
            )
            if (
                str(latest.get("status", "")).upper() in _SUCCESS_STATUSES
                and isinstance(request_iteration, int)
                and isinstance(source_iteration, int)
                and source_iteration == request_iteration
                and not artificial_rebind
            ):
                legal_completed = request_iteration
                if not already_bound and record.get("latest_stage_result") is None:
                    # A matching legacy success has a legal request/result
                    # pair.  Materialize its bounded binding while retaining
                    # the original result body as the stage artifact.
                    self._bind_stage_result(
                        record,
                        latest,
                        stage_iteration=request_iteration,
                        request=latest_request,
                    )
            elif already_bound and isinstance(request_iteration, int):
                # ``latest_result`` is intentionally preserved verbatim and
                # may still carry the legacy provider index.  A completed
                # stage_result_binding is the explicit reconciliation record
                # that proves this artifact was already legally bound.
                legal_completed = request_iteration
                # Fill an omitted attempt number from the rebuilt receipt for
                # legacy requests without changing that request body.
                if existing_binding.get("attempt_index") is None:
                    for attempt in reversed(rebuilt):
                        if (
                            isinstance(attempt, Mapping)
                            and attempt.get("request_id") == latest_request.get("request_id")
                            and isinstance(attempt.get("attempt_index"), int)
                        ):
                            existing_binding["attempt_index"] = attempt["attempt_index"]
                            break
                if isinstance(record.get("latest_stage_result"), Mapping):
                    record["latest_stage_result"]["attempt_index"] = existing_binding.get("attempt_index")
            if (
                isinstance(latest_request, Mapping)
                and isinstance(request_iteration, int)
                and not already_bound
            ):
                if artificial_rebind or source_iteration != request_iteration:
                    # Keep the raw result untouched; expose the discrepancy in
                    # migration metadata and wait for explicit binding.
                    legal_completed = 0
                    record["stage_result_binding"] = {
                        "schema_version": "stage_result_binding.v1",
                        "stage_id": selected,
                        "iteration_index": request_iteration,
                        "attempt_index": latest_request.get("attempt_index"),
                        "request_id": latest_request.get("request_id"),
                        "result_digest": sha256_json(_safe_state_copy(dict(latest))),
                        "source_result_iteration_index": source_iteration,
                        "provider_request_iteration_index": request_iteration,
                        "evidence_complete": False,
                        "reconciled": True,
                        "awaiting_explicit_bind": True,
                    }
                    record["latest_stage_result"] = None
                    record["execution_evidence_complete"] = False
        if legal_completed != record["iteration_index"]:
            record["migration"] = {
                "schema_version": "stage_retry_migration.v1",
                "from_iteration_index": old_iteration,
                "to_iteration_index": legal_completed,
                "attempt_count": len(requests),
                "historical_requests_preserved": True,
                "historical_events_preserved": True,
                "reason": "legacy_iteration_rebind_reconciled",
            }
            record["iteration_index"] = legal_completed
            record["open_iteration_index"] = None
            record["retry_count"] = max(
                0,
                sum(
                    1
                    for item in requests
                    if item.get("iteration_index") == legal_completed + 1
                )
                - 1,
            )
            # Keep the next legal execution coordinate explicit while the
            # reconciled historical result awaits a fresh binding.
            if record.get("status") == StageState.ACTIVE.value:
                record["open_iteration_index"] = (
                    record.get("stage_result_binding", {}).get("iteration_index")
                    if isinstance(record.get("stage_result_binding"), Mapping)
                    and isinstance(record.get("stage_result_binding", {}).get("iteration_index"), int)
                    else legal_completed + 1
                )
        else:
            record.setdefault("migration", {
                "schema_version": "stage_retry_migration.v1",
                "from_iteration_index": old_iteration,
                "to_iteration_index": legal_completed,
                "attempt_count": len(requests),
                "historical_requests_preserved": True,
                "historical_events_preserved": True,
                "reason": "legacy_attempt_receipts_rebuilt",
            })
        if record.get("status") == StageState.ACTIVE.value:
            # Keep the open coordinate explicit after migration.  A pending
            # legacy binding remains on the provider request's iteration;
            # otherwise the next attempt starts at the legally reconciled
            # Stage iteration plus one.  A completed binding closes the
            # iteration until a new technical review opens another one.
            if record.get("execution_evidence_complete") is True and record.get("latest_stage_result") is not None:
                record["open_iteration_index"] = None
            else:
                binding = record.get("stage_result_binding")
                pending_iteration = (
                    binding.get("iteration_index")
                    if isinstance(binding, Mapping)
                    and binding.get("awaiting_explicit_bind") is True
                    and isinstance(binding.get("iteration_index"), int)
                    else legal_completed + 1
                )
                record["open_iteration_index"] = pending_iteration
        if persist and not had_semantics:
            event = self._append_event(
                "retry_semantics_reconciled",
                selected,
                from_status=record["status"],
                to_status=record["status"],
                details={
                    "from_schema": "stage_controller_state.v1",
                    "retry_semantics_version": RETRY_SEMANTICS_VERSION,
                    "legacy_iteration_index": old_iteration,
                    "reconciled_iteration_index": record["iteration_index"],
                    "attempt_count": len(requests),
                    "historical_requests_preserved": True,
                    "historical_events_preserved": True,
                },
            )
        else:
            event = None
        if persist:
            self._persist()
            self._legacy_retry_stage_ids.discard(selected)
        return {
            "event": event,
            "reconciled": True,
            "stage": self.show_stage(selected),
        }

    migrate_retry_semantics = reconcile_retry_semantics
    migrate_state = reconcile_retry_semantics

    def bind_existing_execution_result(
        self,
        result: Mapping[str, Any],
        *,
        request_id: str,
        stage_id: str | None = None,
    ) -> dict[str, Any]:
        """Bind an already recorded provider artifact without rewriting it.

        This is the reconciliation seam for a completed turn produced before
        the retry semantics migration.  The provider request's original
        iteration remains authoritative; a legacy result index is recorded as
        provenance only.
        """

        selected, record = self._require_active(stage_id)
        request_id = _text(request_id, "request_id", max_length=256)
        if not isinstance(result, Mapping):
            raise ContractValidationError("executor result must be an object")
        request, receipt = self._resolve_execution_attempt(record, result, request_id)
        if request is None:
            raise StageControllerError("executor result references an unknown request")
        request_iteration = request.get("iteration_index")
        if not isinstance(request_iteration, int) or request_iteration != self._open_stage_iteration(record):
            raise StageControllerError("existing execution result is not for the currently open Stage iteration")
        status = str(result.get("status", "")).strip().upper()
        if status not in _SUCCESS_STATUSES:
            raise ContractValidationError("only a successful execution artifact can be bound")
        failure = _execution_failure_from_result(result, status)
        if failure is not None:
            raise ContractValidationError("an execution failure cannot be bound as a Stage result")
        source_iteration = result.get("iteration_index")
        legacy_iteration = source_iteration if isinstance(source_iteration, int) and source_iteration != request_iteration else None
        binding = self._bind_stage_result(
            record,
            result,
            stage_iteration=request_iteration,
            request=request,
            legacy_result_iteration=legacy_iteration,
        )
        self._update_attempt_receipt(
            record,
            result,
            request=request,
            receipt=receipt,
            status=status,
            outcome="CONTINUE",
            decision="CONTINUE",
        )
        record["latest_result"] = _safe_state_copy(dict(result))
        record["iteration_index"] = request_iteration
        record["open_iteration_index"] = None
        record["execution_evidence_complete"] = True
        record["migration"] = {
            **(record.get("migration") if isinstance(record.get("migration"), Mapping) else {}),
            "bound_request_id": request_id,
            "bound_stage_iteration_index": request_iteration,
            "source_result_iteration_index": source_iteration,
            "historical_artifact_preserved": True,
        }
        event = self._append_event(
            "stage_result_bound",
            selected,
            from_status=record["status"],
            to_status=record["status"],
            details={
                "request_id": request_id,
                "attempt_index": request.get("attempt_index"),
                "iteration_index": request_iteration,
                "source_result_iteration_index": source_iteration,
                "stage_iteration_advanced": True,
                "result_digest": binding["result_digest"],
                "historical_artifact_preserved": True,
            },
        )
        return {"event": event, "decision": "CONTINUE", "stage": self.show_stage(selected)}

    bind_stage_result = bind_existing_execution_result

    def _ready_evidence(
        self,
        record: Mapping[str, Any],
        result: Mapping[str, Any] | None,
        required_checks: Any,
        review_artifacts: Any,
    ) -> tuple[dict[str, Any], Any, Any]:
        candidate = dict(result or record.get("latest_result") or {})
        self._validate_goal_immutability(candidate, record["contract"])
        status = str(candidate.get("status", "SUCCEEDED")).strip().upper()
        if status in {"BLOCKED", "FAILED", "FAILURE", "ERROR"}:
            raise StageControllerError("a failed or blocked result cannot become STAGE_READY")
        checks = required_checks
        if checks is None:
            checks = candidate.get("required_checks", candidate.get("checks"))
        if checks is None:
            raise ContractValidationError("STAGE_READY requires required_checks evidence")
        check_names, all_pass = _check_names_and_statuses(checks)
        required_names = set(record["contract"]["required_checks"])
        if not all_pass or not required_names.issubset(check_names):
            raise ContractValidationError("STAGE_READY requires every required check to pass")
        artifacts = review_artifacts
        if artifacts is None:
            artifacts = candidate.get("review_artifacts")
        artifact_names = _artifact_names(artifacts)
        required_artifacts = _required_artifact_names(record["contract"]["review_artifact_requirements"])
        if not artifacts or not required_artifacts.issubset(artifact_names):
            raise ContractValidationError("STAGE_READY requires all declared review artifacts")
        baseline_digest = candidate.get("baseline_digest")
        if baseline_digest is not None and baseline_digest != record["baseline_digest"]:
            raise ContractValidationError("result baseline_digest does not match the Stage baseline")
        candidate.setdefault("stage_id", record["contract"]["stage_id"])
        candidate.setdefault("baseline_digest", record["baseline_digest"])
        candidate["required_checks"] = _safe_state_copy(checks)
        candidate["review_artifacts"] = _safe_state_copy(artifacts)
        candidate["stage_ready"] = True
        return candidate, checks, artifacts

    def mark_stage_ready(
        self,
        result: Mapping[str, Any] | None = None,
        *,
        stage_id: str | None = None,
        required_checks: Any = None,
        review_artifacts: Any = None,
        request_id: str | None = None,
        _already_recorded: bool = False,
    ) -> dict[str, Any]:
        """Promote an ACTIVE Stage only after checks and artifacts are present."""

        selected, record = self._require_active(stage_id)
        candidate, checks, artifacts = self._ready_evidence(
            record, result, required_checks, review_artifacts
        )
        supplied_iteration = candidate.get("iteration_index")
        if supplied_iteration is not None:
            if not isinstance(supplied_iteration, int) or isinstance(supplied_iteration, bool) or supplied_iteration < 1:
                raise ContractValidationError("stage result iteration_index must be a positive integer")
            if supplied_iteration not in {record["iteration_index"], self._open_stage_iteration(record)}:
                raise ContractValidationError("stage result iteration_index is not the current Stage iteration")
        stage_iteration_advanced = not _already_recorded and supplied_iteration != record["iteration_index"]
        target_iteration = record["iteration_index"]
        if stage_iteration_advanced:
            target_iteration = self._open_stage_iteration(record) if supplied_iteration is None else supplied_iteration
            maximum = record["contract"].get("max_iterations")
            if maximum is not None and target_iteration > maximum:
                raise StageControllerError("stage ready result exceeds max_iterations")
        record["iteration_index"] = target_iteration
        record["open_iteration_index"] = None
        record["latest_result"] = candidate
        if record.get("latest_stage_result") is None:
            self._bind_stage_result(
                record,
                candidate,
                stage_iteration=record["iteration_index"],
                request=None,
            )
        record["execution_evidence_complete"] = True
        record["status"] = StageState.STAGE_READY.value
        record["pending_human_gate"] = None
        self._active_stage_id = None
        event = self._append_event(
            "stage_ready", selected, from_status=StageState.ACTIVE.value,
            to_status=StageState.STAGE_READY.value,
            details={
                "request_id": request_id,
                "iteration_index": record["iteration_index"],
                "attempt_index": (
                    record.get("latest_stage_result", {}).get("attempt_index")
                    if isinstance(record.get("latest_stage_result"), Mapping) else None
                ),
                "stage_iteration_advanced": stage_iteration_advanced,
                "required_check_count": len(_check_names_and_statuses(checks)[0]),
                "review_artifact_count": len(_artifact_names(artifacts)),
            },
        )
        return {"event": event, "decision": "STAGE_READY", "stage": self.show_stage(selected)}

    def apply_decision(
        self,
        decision: str,
        *,
        stage_id: str | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        """Record a bounded planner decision without inventing a new Stage state."""

        selected, record = self._require_active(stage_id)
        normalized = str(decision).strip().upper()
        if normalized not in PLANNER_DECISIONS:
            raise ContractValidationError("unknown planner decision; refusing to default to CONTINUE")
        rationale = _text(rationale, "rationale", required=False)
        if normalized == "STAGE_READY":
            return self.mark_stage_ready(stage_id=selected)
        if normalized == "BLOCKED":
            record["status"] = StageState.BLOCKED.value
            self._active_stage_id = None
        elif normalized == "HUMAN_GATE":
            record["pending_human_gate"] = {"decision": "HUMAN_GATE", "rationale": rationale}
        event = self._append_event(
            "planner_decision", selected, from_status=StageState.ACTIVE.value,
            to_status=record["status"], details={"decision": normalized, "rationale": rationale},
        )
        return {"event": event, "decision": normalized, "stage": self.show_stage(selected)}

    def resolve_human_gate(
        self,
        approved: bool,
        *,
        stage_id: str | None = None,
        actor: str = "local-human",
        rationale: str = "",
    ) -> dict[str, Any]:
        selected = self._select_stage_id(stage_id)
        assert selected is not None
        record = self._stages[selected]
        if record["status"] != StageState.ACTIVE.value or record.get("pending_human_gate") is None:
            raise StageControllerError("no pending HUMAN_GATE decision for this Stage")
        if not isinstance(approved, bool):
            raise ContractValidationError("approved must be boolean")
        actor, rationale = self._actor_and_rationale(actor, rationale)
        gate = copy.deepcopy(record["pending_human_gate"])
        gate.update({"approved": approved, "actor": actor, "rationale": rationale})
        record["pending_human_gate"] = None
        if not approved:
            record["status"] = StageState.BLOCKED.value
            self._active_stage_id = None
        event = self._append_event(
            "human_gate_resolved", selected,
            from_status=StageState.ACTIVE.value, to_status=record["status"], details=gate,
        )
        return {"event": event, "stage": self.show_stage(selected)}

    def approve_stage(
        self,
        stage_id: str | None = None,
        *,
        actor: str = "local-human",
        rationale: str = "",
    ) -> dict[str, Any]:
        """Explicitly transition STAGE_READY to APPROVED."""

        actor, rationale = self._actor_and_rationale(actor, rationale)
        selected = self._select_stage_id(stage_id)
        assert selected is not None
        record = self._stages[selected]
        if record["status"] != StageState.STAGE_READY.value:
            raise StageControllerError(f"stage {selected} cannot be approved from {record['status']}")
        record["status"] = StageState.APPROVED.value
        record["reviews"].append({"outcome": "APPROVED", "actor": actor, "rationale": rationale})
        event = self._append_event(
            "approve_stage", selected, from_status=StageState.STAGE_READY.value,
            to_status=StageState.APPROVED.value, details={"actor": actor, "rationale": rationale},
        )
        return {"event": event, "stage": self.show_stage(selected)}

    def reject_stage(
        self,
        stage_id: str | None = None,
        *,
        actor: str = "local-human",
        rationale: str = "",
    ) -> dict[str, Any]:
        """Explicitly return STAGE_READY to ACTIVE, retaining feedback."""

        actor, rationale = self._actor_and_rationale(actor, rationale)
        selected = self._select_stage_id(stage_id)
        assert selected is not None
        record = self._stages[selected]
        if record["status"] != StageState.STAGE_READY.value:
            raise StageControllerError(f"stage {selected} cannot be rejected from {record['status']}")
        record["status"] = StageState.ACTIVE.value
        record["reviews"].append({"outcome": "REJECTED", "actor": actor, "rationale": rationale})
        self._active_stage_id = selected
        event = self._append_event(
            "reject_stage", selected, from_status=StageState.STAGE_READY.value,
            to_status=StageState.ACTIVE.value, details={"actor": actor, "rationale": rationale},
        )
        return {"event": event, "stage": self.show_stage(selected)}

    def stop_stage(
        self,
        stage_id: str | None = None,
        *,
        actor: str = "local-human",
        rationale: str = "",
    ) -> dict[str, Any]:
        """Immediately close the Stage to all new executor/consultation requests."""

        actor, rationale = self._actor_and_rationale(actor, rationale)
        selected = self._select_stage_id(stage_id)
        assert selected is not None
        record = self._stages[selected]
        if record["status"] not in {StageState.ACTIVE.value, StageState.STAGE_READY.value}:
            raise StageControllerError(f"stage {selected} cannot be stopped from {record['status']}")
        previous = record["status"]
        record["status"] = StageState.STOPPED.value
        record["stop_reason"] = {"actor": actor, "rationale": rationale}
        if self._active_stage_id == selected:
            self._active_stage_id = None
        event = self._append_event(
            "stop_stage", selected, from_status=previous, to_status=StageState.STOPPED.value,
            details={"actor": actor, "rationale": rationale},
        )
        return {"event": event, "stage": self.show_stage(selected)}


__all__ = [
    "ControllerStageStatus",
    "DEFAULT_EXECUTION_ATTEMPTS_PER_ITERATION",
    "LaneMode",
    "PLANNER_DECISIONS",
    "RETRY_SEMANTICS_VERSION",
    "STAGE_STATES",
    "StageController",
    "StageControllerError",
    "StageStatus",
    "StageState",
    "validate_stage_contract_v1",
]
