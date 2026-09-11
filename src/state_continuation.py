"""Bounded continuation derived from the existing disk-backed StageController.

Dispatch adapters own receipt reconciliation and controller transitions. A return
without a committed Stage change stops this runner instead of replaying effects.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping
import os

from .contracts import sha256_json
from .stage_controller import StageController


class ContinuationError(RuntimeError):
    pass


def _reload(path: Path | None) -> StageController:
    if path is None or not Path(path).is_file():
        raise ContinuationError("DISK_AUTHORITY_REQUIRED")
    try:
        return StageController.from_state(path)
    except (ValueError, OSError, RuntimeError) as exc:
        raise ContinuationError("DISK_AUTHORITY_INVALID") from exc


def _route(current: StageController, stage_id: str | None) -> dict[str, Any]:
    stage = current.show_stage(stage_id)
    common = {
        "stage_id": stage["contract"]["stage_id"],
        "expected_revision": current.state["revision"],
        "result_digest": (stage.get("stage_result_binding") or {}).get("result_digest"),
    }
    status = stage["status"]
    if stage.get("pending_human_gate") is not None:
        route = dict(next_actor="Human", capability=None, position="human_gate", terminal=True)
    elif status in {"APPROVED", "BLOCKED", "STOPPED"}:
        route = dict(next_actor="none", capability=None,
                     position="closed" if status == "APPROVED" else status.lower(), terminal=True)
    elif status == "STAGE_READY":
        route = dict(next_actor="Codex", capability="integration", position="integration")
    elif status == "ACTIVE":
        if stage.get("execution_evidence_complete"):
            route = dict(next_actor="GPT", capability="review.technical", position="technical_review")
            route["consultation_request_ids"] = [
                r["request_id"] for r in stage.get("consultation_requests", [])]
        else:
            pending = [a for a in stage.get("execution_attempts", [])
                       if a.get("status") == "REQUESTED"
                       and a.get("iteration_index") == stage.get("open_iteration_index")]
            route = dict(next_actor="Codex", capability="stage.execution",
                         position="execution_recovery" if pending else "execution")
            if pending:
                route["request_id"] = pending[-1]["request_id"]
    else:
        raise ContinuationError(f"no legal transition from {status}")
    return {**common, **route}


def derive_next(controller: StageController, stage_id: str | None = None) -> dict[str, Any]:
    """Ignore the caller's possibly stale memory; disk is the sole authority."""
    return _route(_reload(controller.state_path), stage_id)


@contextmanager
def _exclusive(path: Path):
    # An OS advisory lock, not lifecycle state. Process death releases ownership.
    with Path(str(path) + ".continuation.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ContinuationError("CONTINUATION_ALREADY_RUNNING") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_bounded(controller: StageController, *, stage_id: str | None = None,
                dispatch: Callable[[dict[str, Any]], Mapping[str, Any]],
                max_steps: int = 32) -> list[dict[str, Any]]:
    if isinstance(max_steps, bool) or not isinstance(max_steps, int) or not 1 <= max_steps <= 32:
        raise ContinuationError("max_steps must be 1..32")
    current = _reload(controller.state_path)
    selected = current.show_stage(stage_id)["contract"]["stage_id"]
    evidence = []
    with _exclusive(Path(controller.state_path)):
        for _ in range(max_steps):
            current = _reload(controller.state_path)
            route = _route(current, selected)
            entry = {"source_revision": current.state["revision"], "route": route}
            evidence.append(entry)
            if route.get("terminal"):
                return evidence
            before = sha256_json(current.show_stage(selected))
            result = dispatch(dict(route))
            if not isinstance(result, Mapping):
                raise ContinuationError("dispatch must return an object")
            committed = _reload(controller.state_path)
            if sha256_json(committed.show_stage(selected)) == before:
                raise ContinuationError("NO_COMMITTED_PROGRESS: reconcile receipts before retry")
            entry["dispatch"] = {"status": result.get("status", "COMPLETED"),
                                 "capability": route["capability"]}
            entry["committed_revision"] = committed.state["revision"]
        final = _route(_reload(controller.state_path), selected)
        evidence.append({"route": final,
                         "stop_reason": "TERMINAL" if final.get("terminal") else "STEP_BUDGET"})
    return evidence
