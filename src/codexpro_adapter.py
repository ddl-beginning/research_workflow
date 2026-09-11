"""Minimal, offline CodexPro handoff-to-Stage result adapter.

This is deliberately an interface proof, not a CodexPro client.  A caller
must supply one already-observed terminal ``wait_for_handoff``-style evidence
object plus the immutable Stage identity/scope.  The adapter never starts an
agent, calls MCP, changes files, advances a Stage, or converts GPT output into
permission to execute.

The reversible PoC proves only the narrow complement supported by the
architecture consultation: bounded CodexPro execution evidence can be shaped
into the existing ``codex_result.v1`` vocabulary while preserving plan-hash,
iteration, baseline, and allowed/protected path checks.  StageController and
Human Gate remain the system of record.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import ContractValidationError, path_is_allowed
from .executor import ExecutionRequest, ExecutionResult, ExecutorDescriptor, validate_execution_result


_TERMINAL_STATES = frozenset({"completed", "failed", "timed_out"})
_TEST_STATUSES = frozenset({"PASS", "FAIL", "SKIP", "ERROR"})
_EVIDENCE_REFS = (
    ".ai-bridge/handoff-run-state.json",
    ".ai-bridge/agent-status.md",
    ".ai-bridge/implementation-diff.patch",
    ".ai-bridge/execution-log.jsonl",
)


def _text(value: Any, field: str, *, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum or "\x00" in value:
        raise ContractValidationError(f"{field} is invalid or too long")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ContractValidationError(f"{field} must be a positive integer")
    return value


def _normalise_tests(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ContractValidationError("tests must be an array")
    out: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ContractValidationError(f"tests[{index}] must be an object")
        name = _text(item.get("name"), f"tests[{index}].name", maximum=300)
        status = _text(item.get("status"), f"tests[{index}].status", maximum=16).upper()
        if status not in _TEST_STATUSES:
            raise ContractValidationError(f"tests[{index}].status is invalid")
        out.append({"name": name, "status": status})
    return out


def _normalise_changed_files(
    value: Any,
    *,
    allowed_paths: Sequence[str],
    protected_paths: Sequence[str],
    workspace_root: str | None,
) -> list[str]:
    if not isinstance(value, list):
        raise ContractValidationError("changed_files must be an explicit array")
    out: list[str] = []
    for index, item in enumerate(value):
        rel = _text(item, f"changed_files[{index}]", maximum=1000).replace("\\", "/")
        if not path_is_allowed(
            rel,
            allowed_paths,
            protected_paths=protected_paths,
            workspace_root=workspace_root,
        ):
            raise ContractValidationError(
                f"changed_files[{index}] is outside allowed_paths/protected_paths"
            )
        if rel not in out:
            out.append(rel)
    return out


def normalize_codexpro_handoff_result(
    evidence: Mapping[str, Any],
    *,
    plan_id: str,
    stage_id: str,
    task_id: str,
    iteration_index: int,
    abstraction_layer: str,
    baseline_digest: str | None,
    expected_plan_hash: str,
    allowed_paths: Sequence[str],
    protected_paths: Sequence[str] = (),
    workspace_root: str | None = None,
    attempt_index: int = 1,
    retry_budget: int | None = None,
) -> dict[str, Any]:
    """Normalize one terminal CodexPro handoff observation.

    ``evidence`` is intentionally shaped like the bounded structured fields
    returned by ``wait_for_handoff`` plus explicit ``changed_files`` and
    ``tests`` supplied by the local supervisor integration.  It is not a
    transport client and ignores raw status/diff/log excerpts.
    """

    if not isinstance(evidence, Mapping):
        raise ContractValidationError("CodexPro handoff evidence must be an object")
    state = _text(evidence.get("state", evidence.get("run_state")), "state", maximum=32).lower()
    if state not in _TERMINAL_STATES:
        raise ContractValidationError("CodexPro handoff evidence must be terminal")
    plan_hash = _text(evidence.get("plan_hash"), "plan_hash", maximum=256)
    expected_hash = _text(expected_plan_hash, "expected_plan_hash", maximum=256)
    if plan_hash != expected_hash:
        raise ContractValidationError("CodexPro plan_hash does not match the awaited plan")
    iteration = _positive_int(evidence.get("iteration"), "iteration")
    expected_iteration = _positive_int(iteration_index, "iteration_index")
    if iteration != expected_iteration:
        raise ContractValidationError("CodexPro iteration does not match the Stage task")

    exit_code = evidence.get("exit_code")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        raise ContractValidationError("exit_code must be an integer or null")
    timed_out = evidence.get("timed_out", state == "timed_out")
    if not isinstance(timed_out, bool):
        raise ContractValidationError("timed_out must be boolean")
    if state == "completed" and exit_code == 0 and not timed_out:
        status = "SUCCEEDED"
    elif state == "timed_out" or timed_out:
        status = "BLOCKED"
    else:
        status = "FAILED"

    checked_plan_id = _text(plan_id, "plan_id", maximum=256)
    checked_stage_id = _text(stage_id, "stage_id", maximum=256)
    checked_task_id = _text(task_id, "task_id", maximum=256)
    checked_layer = _text(abstraction_layer, "abstraction_layer", maximum=256)
    checked_baseline = None if baseline_digest is None else _text(baseline_digest, "baseline_digest", maximum=256)
    changed_files = _normalise_changed_files(
        evidence.get("changed_files"),
        allowed_paths=allowed_paths,
        protected_paths=protected_paths,
        workspace_root=workspace_root,
    )
    tests = _normalise_tests(evidence.get("tests"))

    result: dict[str, Any] = {
        "schema_version": "codex_result.v1",
        "plan_id": checked_plan_id,
        "stage_id": checked_stage_id,
        "task_id": checked_task_id,
        "iteration_index": expected_iteration,
        "stage_iteration_index": expected_iteration,
        "attempt_index": _positive_int(attempt_index, "attempt_index"),
        "attempt_number": _positive_int(attempt_index, "attempt_index"),
        "status": status,
        "summary": f"CodexPro handoff reached terminal state {state}.",
        "changed_files": copy.deepcopy(changed_files),
        "tests": tests,
        "measurements": {
            "codexpro_handoff": {
                "state": state,
                "plan_hash": plan_hash,
                "exit_code": exit_code,
                "timed_out": timed_out,
            }
        },
        "evidence_refs": list(_EVIDENCE_REFS),
        "review_artifacts": [],
        "problems_discovered": [],
        "abstraction_layer": checked_layer,
        # A CodexPro terminal run is evidence only.  It can never self-promote
        # to Stage_READY or synthesize a Human Gate transition.
        "stage_ready": False,
        "user_visible_failure": status != "SUCCEEDED",
        "human_gate_required": False,
        "decision_reason": "CodexPro terminal evidence was normalized; StageController must decide the next workflow action.",
        "stop_reason": "codexpro_handoff_terminal",
    }
    if retry_budget is not None:
        result["retry_budget"] = _positive_int(retry_budget, "retry_budget")
    if checked_baseline is not None:
        result["baseline_digest"] = checked_baseline
    return result


class CodexProHandoffExecutor:
    """Offline provider implementation for the provider-neutral executor seam.

    The adapter deliberately receives one already-observed terminal evidence
    object.  It does not discover CodexPro, start an agent, invoke MCP, or
    mutate a controller.  A real integration can replace this injected
    evidence source while retaining the same ``ExecutionRequest`` /
    ``ExecutionResult`` boundary.
    """

    def __init__(
        self,
        evidence: Mapping[str, Any],
        *,
        available: bool = True,
        reliability: float = 0.75,
        cost: float = 1.0,
    ) -> None:
        if not isinstance(evidence, Mapping):
            raise ContractValidationError("CodexPro handoff evidence must be an object")
        self._evidence = copy.deepcopy(dict(evidence))
        self._descriptor = ExecutorDescriptor(
            provider_id="codexpro-handoff",
            capabilities=("coding", "handoff", "execution_evidence"),
            execution_modes=("PRIMARY", "MAJOR_CHALLENGER"),
            available=available,
            reliability=reliability,
            cost=cost,
            metadata={"transport": "injected_terminal_evidence", "mutates_stage": False},
        )

    @property
    def descriptor(self) -> ExecutorDescriptor:
        return self._descriptor

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        """Normalize evidence for ``request``; never changes Stage state."""

        if not isinstance(request, ExecutionRequest):
            raise ContractValidationError("CodexPro executor requires an ExecutionRequest")
        if not request.plan_id:
            raise ContractValidationError("CodexPro executor request requires plan_id")
        if not request.task_id:
            raise ContractValidationError("CodexPro executor request requires task_id")
        expected_plan_hash = request.metadata.get("expected_plan_hash")
        if not isinstance(expected_plan_hash, str) or not expected_plan_hash.strip():
            raise ContractValidationError("CodexPro executor request requires metadata.expected_plan_hash")
        abstraction_layer = request.metadata.get("abstraction_layer", "codexpro-handoff")
        result = normalize_codexpro_handoff_result(
            self._evidence,
            plan_id=request.plan_id,
            stage_id=request.stage_id,
            task_id=request.task_id,
            iteration_index=request.iteration_index,
            abstraction_layer=abstraction_layer,
            baseline_digest=request.baseline_digest,
            expected_plan_hash=expected_plan_hash,
            allowed_paths=request.allowed_paths,
            protected_paths=request.protected_paths,
            workspace_root=request.workspace_root,
            attempt_index=request.attempt_index,
            retry_budget=request.retry_budget,
        )
        checked = validate_execution_result(result)
        return ExecutionResult(
            provider_id=self.descriptor.provider_id,
            result=checked,
            evidence={
                "provider": self.descriptor.provider_id,
                "state": result["measurements"]["codexpro_handoff"]["state"],
                "plan_hash": expected_plan_hash,
                "iteration": request.iteration_index,
                "attempt_index": request.attempt_index,
            },
        )


__all__ = ["CodexProHandoffExecutor", "normalize_codexpro_handoff_result"]
