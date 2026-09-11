"""Thin, provider-neutral workflow runtime seam.

This module is intentionally smaller than the Core V1 workflow.  It owns the
project-level intake/checkpoint boundary and delegates execution to an
already-configured runner.  It does not implement StageController transitions,
ActionMap validation, GPT transport, or an executor/provider fallback policy.

The persisted files are project-local and bounded:

* ``.research/PROJECT_BRIEF.json`` is owned and atomically written by
  :mod:`src.project_intake`;
* ``.research/workflow-state.json`` is this runtime's atomic checkpoint.

Both paths are resolved beneath the configured workspace.  No executable,
bridge, profile, or secret is inferred from a machine-specific path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from .contracts import canonical_json, ContractValidationError
from .research_routing import parse_research_routing
from .research_prompt_policy import routing_contract_prompt_text
from .project_intake import (
    BriefState,
    ProjectIntakeError,
    ProjectRequirementsIntake,
    validate_project_brief,
)
from .project_state import ProjectStateError, resolve_project_root
from .design_review import (
    DESIGN_REVIEW_STATES,
    DesignReviewError,
    design_review_trigger_required,
    design_summary_digest,
    normalize_design_summary,
)
from .human_artifacts import HumanArtifactError, sanitize_bounded_evidence, write_human_artifacts
from .project_blueprint import (
    BLUEPRINT_MANIFEST_RELATIVE_PATH,
    BLUEPRINT_MANIFEST_SCHEMA_VERSION,
    PROJECT_BLUEPRINT_MARKER,
    PROJECT_BLUEPRINT_STATUS,
)
from .project_discovery import (
    DISCOVERY_REPORT_MARKER,
    DISCOVERY_REPORT_RELATIVE_PATH,
    DISCOVERY_REPORT_SCHEMA_VERSION,
    DISCOVERY_STATUS,
)
from .stage_planning import (
    BOOTSTRAP_STATE_RELATIVE_PATH,
    EXECUTION_HANDOFF_RELATIVE_PATH,
    HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH,
    STAGE_STATE_RELATIVE_PATH,
    build_human_accept_receipt,
    bootstrap_state_digest,
    validate_execution_handoff,
    validate_human_accept_receipt,
)
from .stage_controller import StageController, StageControllerError, StageState, validate_stage_contract_v1
from .workflow_orchestrator import (
    WORKFLOW_TRANSITION_SCHEMA_VERSION,
    WorkflowOrchestratorError,
    validate_workflow_transition,
)
from .design_package import validate_design_package, DesignPackageError


WORKFLOW_CHECKPOINT_SCHEMA_VERSION = "workflow_checkpoint.v1"
WORKFLOW_RUNTIME_RESULT_SCHEMA_VERSION = "workflow_runtime_result.v1"
DEFAULT_CHECKPOINT_RELATIVE = Path(".research") / "workflow-state.json"
WORKFLOW_PHASES = frozenset(
    {
        "INTAKE",
        "WAITING_USER_APPROVAL",
        "READY",
        "AWAITING_DESIGN_REVIEW",
        "DESIGN_ACCEPTED",
        "PLANNED",
        "RUNNING",
        "COMPLETE",
        "BLOCKED",
    }
)
WORKFLOW_NEXT_ACTIONS = frozenset(
    {
        "ASK_REQUIREMENT",
        "REQUEST_BRIEF_APPROVAL",
        "REQUEST_DESIGN_REVIEW",
        "RUN_WORKFLOW",
        "CONTINUE_WORKFLOW",
        "COMPLETE",
        "BLOCKED",
    }
)
WORKFLOW_ANSWER_MODES = frozenset(
    {"ANSWER", "UPDATE", "APPROVE", "DESIGN_SUBMIT", "DESIGN_ACCEPT", "DESIGN_FEEDBACK", "DESIGN_REVIEW"}
)
WORKFLOW_START_MODES = frozenset({"USER_CONFIRMED_BRIEF", "CODEX_REQUIREMENTS_INTERVIEW"})
# ``PLANNED`` is an explicit future Step 15 seam.  It is accepted only when
# the injected runner attests that a Stage Contract and its planned evidence
# were validated; a raw provider status can never produce this transition.
RUNNER_OUTCOMES = frozenset({"CONTINUE_WORKFLOW", "PLANNED", "COMPLETE", "BLOCKED"})

# These are deterministic, workflow-owned inputs derived from the approved
# brief.  A brief revision invalidates them, but it must not delete them: the
# quarantine record is useful evidence and keeps recovery reversible.  Keep
# this list explicit; never quarantine arbitrary files under ``.research``.
_DERIVED_ARTIFACT_RELATIVE_PATHS = (
    Path(".research") / "PROJECT_CONTEXT.md",
    Path(".research") / "ARTIFACT_RETENTION_MANIFEST.json",
    Path(".research") / "LOCAL_PROJECT_PROFILE.md",
    Path(".research") / "AVAILABLE_ASSETS.json",
)
_DERIVED_STALE_RELATIVE_PATH = Path(".research") / "stale"
_DERIVED_INVALIDATION_FILENAME = "INVALIDATION.json"
_DERIVED_INVALIDATION_SCHEMA_VERSION = "workflow_derived_invalidation.v1"
_DERIVED_ARTIFACT_MAX_BYTES = 512_000
_TRANSACTION_STATE_MAX_BYTES = 1_000_000

_TEXT_FIELDS = {
    "problem": "problem_statement",
    "problem_statement": "problem_statement",
    "goal": "goal",
    "desired_outcome": "desired_outcome",
    "project_goal": "goal",
    "observable_outcome": "desired_outcome",
    "input": "input",
    "inputs": "input",
    "input_refs": "input",
    "expected_output": "expected_output",
    "expected_outputs": "expected_output",
    "output": "expected_output",
    "outputs": "expected_output",
    "success_criteria": "success_criteria",
    "acceptance_criteria": "acceptance_criteria",
    "constraints": "constraints",
    "non_goals": "non_goals",
    "scope": "scope",
    "preferences": "preferences",
    "available_assets": "available_assets",
    "acceptance": "acceptance_criteria",
    "human_preferences": "preferences",
}
_BRIEF_FIELDS = (
    "problem",
    "problem_statement",
    "goal",
    "desired_outcome",
    "project_goal",
    "observable_outcome",
    "input",
    "inputs",
    "expected_output",
    "expected_outputs",
    "output",
    "outputs",
    "success_criteria",
    "acceptance_criteria",
    "preferences",
    "scope",
    "non_goals",
    "constraints",
    "available_assets",
    "acceptance",
    "human_preferences",
)
_FORBIDDEN_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "cookie",
    "cookies",
    "dom",
    "local_storage",
    "password",
    "prompt",
    "raw_dom",
    "raw_response",
    "secret",
    "session",
    "session_id",
    "session_storage",
    "storage_state",
    "token",
    "tokens",
}
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.I),
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class WorkflowRuntimeError(RuntimeError):
    """Stable, bounded runtime failure."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.details = copy.deepcopy(dict(details or {}))
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _brief_binding(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded identity used by derived-artifact invalidation."""

    revision = document.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise WorkflowRuntimeError("BRIEF_INVALID", "brief revision is invalid")
    digest = _digest(document)
    calls: dict[str, int] = {}
    for name in ("gpt_calls", "external_calls"):
        value = document.get(name, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise WorkflowRuntimeError("BRIEF_INVALID", f"brief {name} counter is invalid")
        calls[name] = value
    return {
        "revision": revision,
        "digest": digest,
        "gpt_calls": calls["gpt_calls"],
        "external_calls": calls["external_calls"],
    }


def _brief_changed(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> bool:
    if before is None:
        return False
    old_binding = _brief_binding(before)
    new_binding = _brief_binding(after)
    return old_binding["revision"] != new_binding["revision"] or old_binding["digest"] != new_binding["digest"]


@dataclass
class _TransactionFileState:
    path: Path
    existed: bool
    data: bytes | None


@dataclass
class _DerivedArtifactQuarantine:
    root: Path
    target_root: Path
    stale_root: Path
    stale_root_created: bool
    moves: list[tuple[Path, Path]]
    metadata: list[dict[str, Any]]
    committed: bool = False

    def public_view(self) -> dict[str, Any]:
        return {
            "quarantined": True,
            "files": copy.deepcopy(self.metadata),
            "path": self.target_root.relative_to(self.root).as_posix(),
            "invalidation_path": (self.target_root / _DERIVED_INVALIDATION_FILENAME).relative_to(self.root).as_posix(),
        }

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        if self.committed:
            raise WorkflowRuntimeError("WORKFLOW_TRANSACTION_ROLLBACK_FAILED", "committed derived quarantine cannot be rolled back")
        failures: list[str] = []
        invalidation = self.target_root / _DERIVED_INVALIDATION_FILENAME
        try:
            _remove_exact_path(invalidation)
        except Exception as exc:  # noqa: BLE001 - bounded rollback report
            failures.append("invalidation")
        for source, target in reversed(self.moves):
            target_present = target.exists() or target.is_symlink()
            source_present = source.exists() or source.is_symlink()
            if not target_present:
                if not source_present:
                    failures.append(source.relative_to(self.root).as_posix())
                continue
            if source_present:
                failures.append(source.relative_to(self.root).as_posix())
                continue
            try:
                os.replace(target, source)
            except OSError:
                failures.append(source.relative_to(self.root).as_posix())
        if self.target_root.exists() or self.target_root.is_symlink():
            try:
                self.target_root.rmdir()
            except OSError:
                failures.append(self.target_root.relative_to(self.root).as_posix())
        if self.stale_root_created and (self.stale_root.exists() or self.stale_root.is_symlink()):
            try:
                self.stale_root.rmdir()
            except OSError:
                failures.append(self.stale_root.relative_to(self.root).as_posix())
        if failures:
            raise WorkflowRuntimeError(
                "WORKFLOW_TRANSACTION_ROLLBACK_FAILED",
                "derived artifact quarantine rollback failed",
                details={"failed_paths": sorted(set(failures))[:8]},
            )


def _capture_transaction_file(path: Path, *, label: str) -> _TransactionFileState:
    if path.is_symlink():
        raise WorkflowRuntimeError("WORKFLOW_TRANSACTION_INVALID", f"{label} must not be a symlink")
    if not path.exists():
        return _TransactionFileState(path=path, existed=False, data=None)
    if not path.is_file():
        raise WorkflowRuntimeError("WORKFLOW_TRANSACTION_INVALID", f"{label} must be a regular file")
    try:
        size = path.stat().st_size
        if size > _TRANSACTION_STATE_MAX_BYTES:
            raise WorkflowRuntimeError("WORKFLOW_TRANSACTION_INVALID", f"{label} exceeds its bounded size")
        data = path.read_bytes()
    except WorkflowRuntimeError:
        raise
    except OSError as exc:
        raise WorkflowRuntimeError("WORKFLOW_TRANSACTION_INVALID", f"{label} could not be captured") from exc
    return _TransactionFileState(path=path, existed=True, data=data)


def _atomic_bytes_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    fd: int | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".rollback", dir=str(path.parent))
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(data)
        os.replace(temporary, path)
        temporary = None
    except (OSError, TypeError, ValueError) as exc:
        raise WorkflowRuntimeError("WORKFLOW_TRANSACTION_ROLLBACK_FAILED", "workflow state could not be restored") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _remove_exact_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_dir() and not path.is_symlink():
        raise WorkflowRuntimeError("WORKFLOW_TRANSACTION_ROLLBACK_FAILED", "rollback target is a directory")
    try:
        path.unlink()
    except OSError as exc:
        raise WorkflowRuntimeError("WORKFLOW_TRANSACTION_ROLLBACK_FAILED", "rollback target could not be removed") from exc


def _restore_transaction_file(state: _TransactionFileState, *, label: str) -> None:
    if state.existed:
        if state.data is None:
            raise WorkflowRuntimeError("WORKFLOW_TRANSACTION_ROLLBACK_FAILED", f"{label} snapshot is missing")
        _atomic_bytes_write(state.path, state.data)
    else:
        _remove_exact_path(state.path)


def _hash_derived_artifact(path: Path) -> tuple[str, int]:
    """Hash one bounded, regular workflow artifact without reading its content into state."""

    if path.is_symlink() or not path.is_file():
        raise WorkflowRuntimeError("DERIVED_ARTIFACT_INVALID", "workflow-derived artifact is not a regular file")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise WorkflowRuntimeError("DERIVED_ARTIFACT_UNREADABLE", "workflow-derived artifact metadata is unreadable") from exc
    if size > _DERIVED_ARTIFACT_MAX_BYTES:
        raise WorkflowRuntimeError("DERIVED_ARTIFACT_TOO_LARGE", "workflow-derived artifact exceeds its bounded size")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise WorkflowRuntimeError("DERIVED_ARTIFACT_UNREADABLE", "workflow-derived artifact cannot be read") from exc
    return digest.hexdigest(), int(size)


def _quarantine_derived_artifacts(
    workspace_root: Path,
    *,
    operation: str,
    old_brief: Mapping[str, Any],
    new_brief: Mapping[str, Any],
) -> _DerivedArtifactQuarantine | None:
    """Move known derived inputs aside after a canonical brief revision.

    The helper is intentionally called only after a validated intake mutation.
    It does not inspect or repair arbitrary load failures, and it never uses a
    derived artifact as input to the invalidation record.  A bounded rollback
    is attempted if any move or receipt write fails; the caller still receives
    a fail-closed error and must not persist a new runnable checkpoint.
    """

    root = workspace_root.resolve()
    research_root = root / ".research"
    existing = [root / relative for relative in _DERIVED_ARTIFACT_RELATIVE_PATHS if (root / relative).exists() or (root / relative).is_symlink()]
    if not existing:
        return None
    if research_root.is_symlink() or not research_root.is_dir():
        raise WorkflowRuntimeError("DERIVED_ARTIFACT_QUARANTINE_FAILED", "workflow research directory is not a regular directory")

    old_binding = _brief_binding(old_brief)
    new_binding = _brief_binding(new_brief)
    operation_text = _bounded_text(operation, "operation", required=True, maximum=64)
    directory_name = (
        f"rev-{old_binding['revision']}-to-{new_binding['revision']}"
        f"-{old_binding['digest'][:12]}-{new_binding['digest'][:12]}"
    )
    stale_root = root / _DERIVED_STALE_RELATIVE_PATH
    target_root = stale_root / directory_name
    stale_root_created = False
    try:
        stale_root.relative_to(research_root)
        target_root.relative_to(research_root)
    except ValueError as exc:  # pragma: no cover - constants are the guard
        raise WorkflowRuntimeError("DERIVED_ARTIFACT_QUARANTINE_FAILED", "stale artifact path escapes .research") from exc
    if stale_root.exists() or stale_root.is_symlink():
        if stale_root.is_symlink() or not stale_root.is_dir():
            raise WorkflowRuntimeError("DERIVED_ARTIFACT_QUARANTINE_FAILED", "stale artifact directory is not a regular directory")
    else:
        try:
            stale_root.mkdir(parents=False)
            stale_root_created = True
        except OSError as exc:
            raise WorkflowRuntimeError("DERIVED_ARTIFACT_QUARANTINE_FAILED", "stale artifact directory could not be created") from exc
    if target_root.exists() or target_root.is_symlink():
        raise WorkflowRuntimeError("DERIVED_ARTIFACT_QUARANTINE_FAILED", "stale artifact quarantine target already exists")
    try:
        target_root.mkdir()
    except OSError as exc:
        raise WorkflowRuntimeError("DERIVED_ARTIFACT_QUARANTINE_FAILED", "stale artifact quarantine target could not be created") from exc

    metadata: list[dict[str, Any]] = []
    moves: list[tuple[Path, Path]] = []
    try:
        for source in existing:
            try:
                source.relative_to(root)
            except ValueError as exc:  # pragma: no cover - constants are the guard
                raise WorkflowRuntimeError("DERIVED_ARTIFACT_QUARANTINE_FAILED", "derived artifact path escapes workspace") from exc
            if source.is_symlink() or not source.is_file():
                raise WorkflowRuntimeError("DERIVED_ARTIFACT_INVALID", "workflow-derived artifact is not a regular file")
            digest, size = _hash_derived_artifact(source)
            relative = source.relative_to(root).as_posix()
            target = target_root / Path(relative).name
            if target.exists() or target.is_symlink():
                raise WorkflowRuntimeError("DERIVED_ARTIFACT_QUARANTINE_FAILED", "stale artifact target already exists")
            os.replace(source, target)
            moves.append((source, target))
            moved_digest, moved_size = _hash_derived_artifact(target)
            if moved_digest != digest or moved_size != size:
                raise WorkflowRuntimeError("DERIVED_ARTIFACT_QUARANTINE_FAILED", "quarantined artifact changed during move")
            metadata.append(
                {
                    "relative_path": relative,
                    "sha256": digest,
                    "size_bytes": size,
                    "quarantined_path": (target.relative_to(root)).as_posix(),
                }
            )
        invalidation: dict[str, Any] = {
            "schema_version": _DERIVED_INVALIDATION_SCHEMA_VERSION,
            "operation": operation_text,
            "old_brief_revision": old_binding["revision"],
            "new_brief_revision": new_binding["revision"],
            "old_brief_digest": old_binding["digest"],
            "new_brief_digest": new_binding["digest"],
            "old_gpt_calls": old_binding["gpt_calls"],
            "new_gpt_calls": new_binding["gpt_calls"],
            "old_external_calls": old_binding["external_calls"],
            "new_external_calls": new_binding["external_calls"],
            "files": metadata,
        }
        _atomic_json_write(target_root / _DERIVED_INVALIDATION_FILENAME, invalidation)
    except WorkflowRuntimeError as exc:
        rollback_failures = _rollback_derived_moves(moves)
        _remove_empty_directory(target_root)
        if stale_root_created:
            _remove_empty_directory(stale_root)
        if target_root.exists() or target_root.is_symlink():
            rollback_failures.append(target_root.relative_to(root).as_posix())
        if stale_root_created and (stale_root.exists() or stale_root.is_symlink()):
            rollback_failures.append(stale_root.relative_to(root).as_posix())
        if rollback_failures:
            raise WorkflowRuntimeError(
                "WORKFLOW_TRANSACTION_ROLLBACK_FAILED",
                "derived artifact quarantine rollback failed",
                details={"failed_paths": sorted(set(rollback_failures))[:8]},
            ) from exc
        if exc.code == "DERIVED_ARTIFACT_QUARANTINE_FAILED":
            raise
        raise WorkflowRuntimeError(
            "DERIVED_ARTIFACT_QUARANTINE_FAILED",
            "workflow-derived invalidation receipt could not be written",
        ) from exc
    except (OSError, TypeError, ValueError) as exc:
        rollback_failures = _rollback_derived_moves(moves)
        _remove_empty_directory(target_root)
        if stale_root_created:
            _remove_empty_directory(stale_root)
        if target_root.exists() or target_root.is_symlink():
            rollback_failures.append(target_root.relative_to(root).as_posix())
        if stale_root_created and (stale_root.exists() or stale_root.is_symlink()):
            rollback_failures.append(stale_root.relative_to(root).as_posix())
        if rollback_failures:
            raise WorkflowRuntimeError(
                "WORKFLOW_TRANSACTION_ROLLBACK_FAILED",
                "derived artifact quarantine rollback failed",
                details={"failed_paths": sorted(set(rollback_failures))[:8]},
            ) from exc
        raise WorkflowRuntimeError("DERIVED_ARTIFACT_QUARANTINE_FAILED", "workflow-derived artifacts could not be quarantined") from exc
    return _DerivedArtifactQuarantine(
        root=root,
        target_root=target_root,
        stale_root=stale_root,
        stale_root_created=stale_root_created,
        moves=moves,
        metadata=metadata,
    )


def _rollback_derived_moves(moves: list[tuple[Path, Path]]) -> list[str]:
    """Best-effort rollback used only before a new workflow checkpoint exists."""

    failures: list[str] = []
    for source, target in reversed(moves):
        if source.exists() or source.is_symlink():
            if target.exists() or target.is_symlink():
                failures.append(source.name)
            continue
        if not target.exists() and not target.is_symlink():
            if not source.exists():
                failures.append(source.name)
            continue
        try:
            os.replace(target, source)
        except OSError:
            failures.append(source.name)
    return failures


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _rollback_workflow_mutation(
    *,
    brief_state: _TransactionFileState,
    checkpoint_state: _TransactionFileState,
    quarantine: _DerivedArtifactQuarantine | None,
) -> None:
    """Restore the pre-intake transaction before exposing a failure."""

    failures: list[str] = []
    if quarantine is not None:
        try:
            quarantine.rollback()
        except WorkflowRuntimeError as exc:
            failures.append("derived_artifacts")
    try:
        _restore_transaction_file(brief_state, label="project brief")
    except WorkflowRuntimeError:
        failures.append("project_brief")
    try:
        _restore_transaction_file(checkpoint_state, label="workflow checkpoint")
    except WorkflowRuntimeError:
        failures.append("workflow_checkpoint")
    if failures:
        raise WorkflowRuntimeError(
            "WORKFLOW_TRANSACTION_ROLLBACK_FAILED",
            "workflow mutation rollback failed; runnable state is not claimed",
            details={"failed_components": sorted(set(failures))},
        )


def _assert_safe(value: Any, *, path: str = "$", depth: int = 0) -> None:
    """Reject high-confidence secrets before runtime state/output handling."""

    if depth > 12:
        raise WorkflowRuntimeError("INPUT_TOO_DEEP", f"input nesting is too deep at {path}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key).strip().lower().replace("-", "_")
            if name in _FORBIDDEN_KEYS:
                raise WorkflowRuntimeError("SECRET_REJECTED", f"sensitive field is not allowed at {path}")
            _assert_safe(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            if pattern.search(value):
                raise WorkflowRuntimeError("SECRET_REJECTED", f"high-confidence secret at {path}")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise WorkflowRuntimeError("INPUT_INVALID", f"unsupported value at {path}")


def _bounded_text(value: Any, field: str, *, required: bool = False, maximum: int = 4000) -> str:
    if not isinstance(value, str):
        raise WorkflowRuntimeError("INPUT_INVALID", f"{field} must be a string")
    checked = value.strip()
    if required and not checked:
        raise WorkflowRuntimeError("INPUT_INVALID", f"{field} must be non-empty")
    if len(checked) > maximum or "\x00" in checked:
        raise WorkflowRuntimeError("INPUT_INVALID", f"{field} is invalid or too long")
    return checked


def _bounded_copy(value: Any, *, depth: int = 0) -> Any:
    """Copy JSON-safe runner metadata without retaining raw transport text."""

    if depth > 8:
        raise WorkflowRuntimeError("RUNNER_RESULT_INVALID", "runner result is too deeply nested")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            name = str(key)
            if name.lower() in _FORBIDDEN_KEYS:
                raise WorkflowRuntimeError("RUNNER_RESULT_INVALID", "runner result contains a sensitive field")
            result[name] = _bounded_copy(child, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_bounded_copy(child, depth=depth + 1) for child in value]
    if isinstance(value, str):
        if len(value) > 4000 or "\x00" in value:
            raise WorkflowRuntimeError("RUNNER_RESULT_INVALID", "runner result contains oversized text")
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise WorkflowRuntimeError("RUNNER_RESULT_INVALID", "runner result must be JSON-safe")


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically replace one project-local JSON checkpoint."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    fd: int | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
        temporary = None
    except (OSError, TypeError, ValueError) as exc:
        raise WorkflowRuntimeError("CHECKPOINT_WRITE_FAILED", "workflow checkpoint could not be written") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _normalize_brief_input(
    value: Mapping[str, Any] | None,
    *,
    include_defaults: bool = True,
) -> dict[str, Any]:
    """Map the runtime vocabulary onto the existing intake vocabulary."""

    if value is None:
        source: dict[str, Any] = {}
    elif isinstance(value, Mapping):
        source = dict(value)
    else:
        raise WorkflowRuntimeError("BRIEF_INVALID", "brief must be an object")
    _assert_safe(source, path="brief")
    normalized: dict[str, Any] = {}
    for key, child in source.items():
        canonical = _TEXT_FIELDS.get(str(key), str(key))
        normalized[canonical] = copy.deepcopy(child)
    # Keep all runtime-level requirement slots explicit in a new canonical
    # brief without inventing their contents or asking more than one question.
    if include_defaults:
        normalized.setdefault("goal", "")
        normalized.setdefault("problem_statement", "")
        normalized.setdefault("desired_outcome", "")
        normalized.setdefault("input", "")
        normalized.setdefault("expected_output", "")
        normalized.setdefault("scope", [])
        normalized.setdefault("non_goals", [])
        normalized.setdefault("constraints", [])
        normalized.setdefault("available_assets", [])
        normalized.setdefault("acceptance_criteria", [])
        normalized.setdefault("preferences", [])
    _assert_safe(normalized, path="brief")
    return normalized


def _orchestrator_failure(exc: BaseException, *, fallback: str = "ORCHESTRATOR_FAILED") -> WorkflowRuntimeError:
    code = getattr(exc, "code", None)
    stable_code = code if isinstance(code, str) and code.strip() else fallback
    if stable_code == "RUNTIME_COMPOSITION_NOT_READY":
        # Preserve the long-standing MCP blocked error while exposing the
        # bounded consultant/context diagnostics in ``details``.
        stable_code = "RUNNER_NOT_CONFIGURED"
    details = getattr(exc, "details", None)
    if not isinstance(details, Mapping):
        bounded_view = getattr(exc, "bounded_view", None)
        if callable(bounded_view):
            try:
                details = bounded_view()
            except Exception:  # pragma: no cover - defensive diagnostic boundary
                details = None
    return WorkflowRuntimeError(stable_code, str(exc), details=details if isinstance(details, Mapping) else None)


def _validate_bootstrap_state_metadata(value: Any, brief: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "bootstrap state metadata is invalid")
    detached = _bounded_copy(dict(value))
    try:
        expected_digest = bootstrap_state_digest(detached)
    except Exception as exc:  # pragma: no cover - defensive validation boundary
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "bootstrap state digest could not be calculated") from exc
    if detached.get("schema_version") != "bootstrap_state.v1":
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "bootstrap state schema is invalid")
    if detached.get("project_id") != brief.get("project_id"):
        raise WorkflowRuntimeError("PROJECT_IDENTITY_MISMATCH", "bootstrap state project identity differs")
    if detached.get("brief_digest") != _digest(brief):
        raise WorkflowRuntimeError("CHECKPOINT_STALE", "bootstrap state brief digest is stale")
    if not isinstance(detached.get("state_digest"), str) or not _HEX64.fullmatch(detached["state_digest"]):
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "bootstrap state digest is invalid")
    if detached["state_digest"] != expected_digest:
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "bootstrap state digest is inconsistent")
    if detached.get("project_scope_verified") is not True:
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "bootstrap state project scope is not verified")
    if detached.get("stage_created") is not False or detached.get("stage_started") is not False:
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "bootstrap state claims Stage side effects")
    for field in ("context_digest", "discovery_digest", "blueprint_digest"):
        if not isinstance(detached.get(field), str) or _HEX64.fullmatch(detached[field]) is None:
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", f"bootstrap state {field} is invalid")
    return detached


def _validate_human_accept_receipt_metadata(value: Any, brief: Mapping[str, Any], design_review: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the persisted Human ACCEPT record against current facts."""

    try:
        return validate_human_accept_receipt(
            value,
            project_id=str(brief.get("project_id")),
            brief_digest=_digest(brief),
            design_digest=str(design_review.get("design_digest")),
        )
    except Exception as exc:  # noqa: BLE001 - bounded contract boundary
        code = getattr(exc, "code", None)
        raise WorkflowRuntimeError(
            code if isinstance(code, str) and code.strip() else "CHECKPOINT_INVALID",
            "Human ACCEPT receipt is invalid or stale",
        ) from exc


def _reify_human_accept_receipt(
    payload: dict[str, Any],
    *,
    brief: Mapping[str, Any],
    design_review: Mapping[str, Any],
    workspace_root: Path,
    checkpoint_path: Path,
) -> dict[str, Any]:
    """Materialize the canonical receipt for an older accepted checkpoint.

    Older accepted checkpoints recorded the explicit Human ACCEPT decision and
    its actor but predated the standalone receipt artifact.  Reification is
    allowed only from that persisted decision plus the current brief/design
    digests; it never invents an approval or accepts a caller-supplied digest.
    The checkpoint is updated atomically so subsequent resume calls use the
    same receipt and do not repeat the migration.
    """

    if not isinstance(payload, dict) or not isinstance(design_review, Mapping):
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "accepted Design Review is not reifiable")
    expected_project_id = str(brief.get("project_id"))
    expected_brief_digest = _digest(brief)
    expected_design_digest = str(design_review.get("design_digest"))
    existing = payload.get("human_accept_receipt")
    embedded = design_review.get("human_accept_receipt")
    if isinstance(existing, Mapping) and isinstance(embedded, Mapping) and _bounded_copy(dict(existing)) != _bounded_copy(dict(embedded)):
        raise WorkflowRuntimeError("HUMAN_ACCEPT_RECEIPT_BINDING_MISMATCH", "checkpoint and Design Review receipts disagree")
    if existing is None:
        existing = embedded
    decision = design_review.get("human_decision")
    if not isinstance(decision, Mapping) or decision.get("decision") != "ACCEPT":
        raise WorkflowRuntimeError("HUMAN_ACCEPT_RECEIPT_MISSING", "accepted Design Review has no explicit ACCEPT decision to reify")
    actor = decision.get("actor")
    if not isinstance(actor, str) or not actor.strip():
        raise WorkflowRuntimeError("HUMAN_ACCEPT_RECEIPT_INVALID", "accepted Design Review actor is missing")

    receipt_path = workspace_root / HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH
    receipt: dict[str, Any]
    if receipt_path.exists() or receipt_path.is_symlink():
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise WorkflowRuntimeError("HUMAN_ACCEPT_RECEIPT_INVALID", "canonical Human ACCEPT receipt is not a regular file")
        try:
            persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkflowRuntimeError("HUMAN_ACCEPT_RECEIPT_INVALID", "canonical Human ACCEPT receipt is unreadable") from exc
        receipt = _validate_human_accept_receipt_metadata(persisted, brief, design_review)
        if isinstance(existing, Mapping) and _bounded_copy(dict(existing)) != receipt:
            raise WorkflowRuntimeError("HUMAN_ACCEPT_RECEIPT_BINDING_MISMATCH", "checkpoint receipt differs from the canonical receipt")
    elif isinstance(existing, Mapping):
        receipt = _validate_human_accept_receipt_metadata(existing, brief, design_review)
        _atomic_json_write(receipt_path, receipt)
    else:
        accepted_at = decision.get("accepted_at")
        if not isinstance(accepted_at, str):
            accepted_at = design_review.get("accepted_at")
        receipt = build_human_accept_receipt(
            project_id=expected_project_id,
            brief_digest=expected_brief_digest,
            design_digest=expected_design_digest,
            actor=actor,
            accepted_at=accepted_at if isinstance(accepted_at, str) else None,
            source="legacy_accepted_checkpoint_reification",
        )
        _atomic_json_write(receipt_path, receipt)

    if decision.get("receipt_digest") not in {None, receipt.get("receipt_digest")}:
        raise WorkflowRuntimeError("HUMAN_ACCEPT_RECEIPT_BINDING_MISMATCH", "accepted decision receipt digest is stale")
    changed = payload.get("human_accept_receipt") != receipt
    payload["human_accept_receipt"] = copy.deepcopy(receipt)
    updated_review = copy.deepcopy(dict(design_review))
    if updated_review.get("human_accept_receipt") != receipt:
        changed = True
    updated_review["human_accept_receipt"] = copy.deepcopy(receipt)
    updated_decision = copy.deepcopy(dict(decision))
    if updated_decision.get("receipt_digest") != receipt.get("receipt_digest"):
        updated_decision["receipt_digest"] = receipt.get("receipt_digest")
        changed = True
    updated_review["human_decision"] = updated_decision
    if payload.get("design_review") != updated_review:
        changed = True
    payload["design_review"] = updated_review
    if changed:
        payload["last_operation"] = "accepted_receipt_reified"
        payload["updated_at"] = _now()
        _assert_safe(payload, path="checkpoint")
        _atomic_json_write(checkpoint_path, payload)
    return receipt


def _validate_execution_handoff_metadata(
    value: Any,
    *,
    brief: Mapping[str, Any],
    design_review: Mapping[str, Any],
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Validate a full accepted-design handoff carried by a checkpoint."""

    handoff_design = value.get("design") if isinstance(value, Mapping) else None
    package_digest = handoff_design.get("digest") if isinstance(handoff_design, Mapping) else None
    try:
        checked = validate_execution_handoff(
            value,
            project_id=str(brief.get("project_id")),
            brief_digest=_digest(brief),
            design_package_digest=package_digest if isinstance(package_digest, str) else None,
            design_digest=str(design_review.get("design_digest")),
        )
    except Exception as exc:  # noqa: BLE001 - bounded contract boundary
        code = getattr(exc, "code", None)
        raise WorkflowRuntimeError(
            code if isinstance(code, str) and code.strip() else "CHECKPOINT_INVALID",
            "execution handoff is invalid or stale",
        ) from exc
    if workspace_root is not None:
        _validate_execution_handoff_files(
            checked,
            workspace_root=workspace_root,
            brief=brief,
            design_review=design_review,
        )
    return checked


def _validate_execution_handoff_files(
    handoff: Mapping[str, Any],
    *,
    workspace_root: Path,
    brief: Mapping[str, Any],
    design_review: Mapping[str, Any],
) -> None:
    """Bind a persisted handoff to the current project-local evidence.

    ``validate_execution_handoff`` checks the self-binding envelope.  A
    checkpoint must also prove that the referenced Design Package, Human
    ACCEPT receipt, and research receipts still contain the bytes that were
    accepted.  This keeps a valid-looking copied envelope from outliving the
    evidence it claims to describe.
    """

    if handoff.get("repository_root") != workspace_root.as_posix():
        raise WorkflowRuntimeError("WORKSPACE_IDENTITY_MISMATCH", "execution handoff workspace differs")

    def canonical_file(relative: str, label: str) -> tuple[Path, bytes]:
        candidate = workspace_root / Path(relative)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(workspace_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkflowRuntimeError("EXECUTION_HANDOFF_PATH_INVALID", f"{label} path is invalid") from exc
        if candidate.is_symlink() or resolved != candidate or not candidate.is_file():
            raise WorkflowRuntimeError("EXECUTION_HANDOFF_INPUT_INVALID", f"{label} is not a regular file")
        try:
            raw = candidate.read_bytes()
        except (OSError, UnicodeError) as exc:
            raise WorkflowRuntimeError("EXECUTION_HANDOFF_INPUT_INVALID", f"{label} is unreadable") from exc
        if len(raw) > _TRANSACTION_STATE_MAX_BYTES:
            raise WorkflowRuntimeError("EXECUTION_HANDOFF_INPUT_INVALID", f"{label} is too large")
        return candidate, raw

    design = handoff.get("design")
    if not isinstance(design, Mapping) or design.get("path") != ".research/DESIGN_PACKAGE.json":
        raise WorkflowRuntimeError("EXECUTION_HANDOFF_PATH_INVALID", "Design Package path is not canonical")
    _, package_raw = canonical_file(".research/DESIGN_PACKAGE.json", "Design Package")
    try:
        package_payload = json.loads(package_raw.decode("utf-8"))
        checked_package = validate_design_package(package_payload)
    except (UnicodeError, json.JSONDecodeError, DesignPackageError) as exc:
        raise WorkflowRuntimeError("DESIGN_PACKAGE_INVALID", "Design Package is invalid during handoff verification") from exc
    if _digest(checked_package) != design.get("digest"):
        raise WorkflowRuntimeError("EXECUTION_HANDOFF_BINDING_MISMATCH", "execution handoff Design Package digest is stale")
    if hashlib.sha256(package_raw).hexdigest() != design.get("file_digest"):
        raise WorkflowRuntimeError("EXECUTION_HANDOFF_BINDING_MISMATCH", "execution handoff Design Package file digest is stale")

    decision = handoff.get("human_decision")
    if not isinstance(decision, Mapping) or decision.get("receipt_path") != HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix():
        raise WorkflowRuntimeError("EXECUTION_HANDOFF_PATH_INVALID", "Human ACCEPT receipt path is not canonical")
    _, receipt_raw = canonical_file(HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix(), "Human ACCEPT receipt")
    try:
        receipt_payload = json.loads(receipt_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise WorkflowRuntimeError("HUMAN_ACCEPT_RECEIPT_INVALID", "Human ACCEPT receipt is invalid during handoff verification") from exc
    checked_receipt = _validate_human_accept_receipt_metadata(receipt_payload, brief, design_review)
    if checked_receipt.get("receipt_digest") != decision.get("receipt_digest"):
        raise WorkflowRuntimeError("EXECUTION_HANDOFF_BINDING_MISMATCH", "execution handoff Human ACCEPT digest is stale")
    receipt_file_digest = hashlib.sha256(receipt_raw).hexdigest()
    if decision.get("receipt_file_digest") not in {None, receipt_file_digest}:
        raise WorkflowRuntimeError("EXECUTION_HANDOFF_BINDING_MISMATCH", "execution handoff Human ACCEPT file digest is stale")

    provenance = handoff.get("research_provenance")
    input_digests = handoff.get("input_digests")
    if not isinstance(provenance, list) or not provenance or not isinstance(input_digests, Mapping):
        raise WorkflowRuntimeError("RESEARCH_PROVENANCE_INVALID", "execution handoff research provenance is incomplete")
    for item in provenance:
        if not isinstance(item, Mapping):
            raise WorkflowRuntimeError("RESEARCH_PROVENANCE_INVALID", "execution handoff research provenance is invalid")
        relative = item.get("path")
        digest = item.get("digest")
        if not isinstance(relative, str) or not relative.strip() or not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise WorkflowRuntimeError("RESEARCH_PROVENANCE_INVALID", "execution handoff research receipt descriptor is invalid")
        if input_digests.get(relative) != digest:
            raise WorkflowRuntimeError("RESEARCH_PROVENANCE_BINDING_MISMATCH", "research receipt descriptor is not bound to handoff inputs")
        _, raw = canonical_file(relative, "research receipt")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise WorkflowRuntimeError("RESEARCH_PROVENANCE_BINDING_MISMATCH", "research receipt digest is stale")


def _validate_orchestrator_transition_metadata(
    value: Any,
    *,
    brief: Mapping[str, Any],
    expected_transition: str | None = None,
) -> dict[str, Any]:
    try:
        checked = validate_workflow_transition(value)
    except WorkflowOrchestratorError as exc:
        raise _orchestrator_failure(exc, fallback="ORCHESTRATOR_TRANSITION_INVALID") from exc
    except Exception as exc:  # pragma: no cover - defensive validation boundary
        raise WorkflowRuntimeError("ORCHESTRATOR_TRANSITION_INVALID", "orchestrator transition is invalid") from exc
    if expected_transition is not None and checked.get("transition") != expected_transition:
        raise WorkflowRuntimeError("ORCHESTRATOR_TRANSITION_INVALID", "orchestrator transition is not at the required boundary")
    if checked.get("project_id") != brief.get("project_id"):
        raise WorkflowRuntimeError("PROJECT_IDENTITY_MISMATCH", "orchestrator transition project identity differs")
    if checked.get("brief_digest") != _digest(brief):
        raise WorkflowRuntimeError("CHECKPOINT_STALE", "orchestrator transition brief digest is stale")
    return checked


def _runtime_brief_view(brief: Mapping[str, Any]) -> dict[str, Any]:
    acceptance = brief.get("acceptance_criteria")
    if not acceptance:
        acceptance = brief.get("success_criteria", [])
    return {
        "problem": copy.deepcopy(brief.get("problem_statement", "")),
        "project_goal": copy.deepcopy(brief.get("goal", "")),
        "observable_outcome": copy.deepcopy(brief.get("desired_outcome", "")),
        "input": copy.deepcopy(brief.get("input", "")),
        "expected_output": copy.deepcopy(brief.get("expected_output", "")),
        "scope": copy.deepcopy(brief.get("scope", [])),
        "non_goals": copy.deepcopy(brief.get("non_goals", [])),
        "constraints": copy.deepcopy(brief.get("constraints", [])),
        "available_assets": copy.deepcopy(brief.get("available_assets", [])),
        "acceptance": copy.deepcopy(acceptance),
        "human_preferences": copy.deepcopy(brief.get("preferences", [])),
    }


def _bounded_evidence_text(value: Any, *, limit: int = 600) -> str | None:
    return sanitize_bounded_evidence(value, limit=limit)


def _bounded_evidence_list(value: Any, *, limit: int = 32) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in list(value)[:limit]:
        text = _bounded_evidence_text(item)
        if text is not None and text not in result:
            result.append(text)
    return result


def _next_action_view(
    brief: Mapping[str, Any],
    *,
    phase: str,
    design_review_state: str = "NOT_REQUIRED",
) -> dict[str, Any]:
    """Derive the only runtime-level action vocabulary from validated state.

    This is deliberately a small protocol projection.  It does not decide a
    Stage route or inspect a provider result.  A checkpoint phase is accepted
    only when it is consistent with the canonical brief state.
    """

    if phase not in WORKFLOW_PHASES:
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "workflow checkpoint phase is invalid")
    if design_review_state not in DESIGN_REVIEW_STATES:
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "workflow design review state is invalid")

    state = brief.get("state")
    question = brief.get("next_question")
    if phase == "BLOCKED":
        if state not in {BriefState.APPROVED.value, BriefState.CANCELLED.value}:
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "blocked phase is inconsistent with brief state")
        return {
            "next_action": "BLOCKED",
            "question": None,
            "question_id": None,
            "next_tool": None,
        }
    if state == BriefState.DRAFT.value:
        if phase != "INTAKE":
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "DRAFT brief must remain in intake phase")
        if not isinstance(question, Mapping):
            raise WorkflowRuntimeError("BRIEF_INVALID", "DRAFT brief must expose one question")
        question_id = question.get("id")
        if not isinstance(question_id, str) or not question_id.strip():
            raise WorkflowRuntimeError("BRIEF_INVALID", "brief question must have an id")
        return {
            "next_action": "ASK_REQUIREMENT",
            "question": copy.deepcopy(dict(question)),
            "question_id": question_id,
            "next_tool": "workflow_answer",
        }

    if state == BriefState.WAITING_USER_APPROVAL.value:
        if phase != "WAITING_USER_APPROVAL":
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "approval request has an invalid workflow phase")
        if question is not None:
            raise WorkflowRuntimeError("BRIEF_INVALID", "approval brief cannot retain a question")
        return {
            "next_action": "REQUEST_BRIEF_APPROVAL",
            "question": None,
            "question_id": None,
            "next_tool": "workflow_answer",
        }

    if state == BriefState.CANCELLED.value:
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "cancelled brief must remain blocked")

    if state != BriefState.APPROVED.value:
        raise WorkflowRuntimeError("BRIEF_INVALID", "brief state is not actionable")
    if question is not None:
        raise WorkflowRuntimeError("BRIEF_INVALID", "approved brief cannot retain a question")
    if phase == "AWAITING_DESIGN_REVIEW":
        if design_review_state != "AWAITING_DESIGN_REVIEW":
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "design review phase is inconsistent")
        return {
            "next_action": "REQUEST_DESIGN_REVIEW",
            "question": None,
            "question_id": None,
            "next_tool": "workflow_answer",
        }
    if design_review_state == "AWAITING_DESIGN_REVIEW":
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "pending design review must remain awaiting review")
    if phase == "DESIGN_ACCEPTED" and design_review_state != "ACCEPTED":
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "design-accepted workflow requires accepted design review")
    if phase in {"PLANNED", "RUNNING", "COMPLETE", "BLOCKED"} and design_review_state not in {"NOT_REQUIRED", "ACCEPTED"}:
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "execution phase has an invalid design review state")
    if phase == "PLANNED" and design_review_state != "ACCEPTED":
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "planned workflow requires accepted design review")
    if phase == "READY" and design_review_state == "ACCEPTED":
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "accepted design review must expose design-accepted phase")
    if phase == "COMPLETE":
        action = "COMPLETE"
        tool = None
    elif phase == "RUNNING":
        action = "CONTINUE_WORKFLOW"
        tool = "workflow_run"
    elif phase in {"READY", "DESIGN_ACCEPTED", "PLANNED"}:
        action = "RUN_WORKFLOW"
        tool = "workflow_run"
    else:
        raise WorkflowRuntimeError("CHECKPOINT_INVALID", "approved brief has an invalid workflow phase")
    return {"next_action": action, "question": None, "question_id": None, "next_tool": tool}


def _validated_runner_outcome(result: Mapping[str, Any]) -> str:
    """Accept only the narrow runtime outcome envelope from an existing runner.

    A provider's raw ``status`` is intentionally not a lifecycle signal.  The
    runner must explicitly attest that it validated its own execution contract
    before this thin runtime may persist a phase transition.
    """

    candidate = result.get("runner_outcome")
    if not isinstance(candidate, Mapping):
        raise WorkflowRuntimeError(
            "RUNNER_OUTCOME_INVALID",
            "runner must return a validated workflow_runner_outcome.v1 envelope",
        )
    if candidate.get("schema_version") != "workflow_runner_outcome.v1":
        raise WorkflowRuntimeError("RUNNER_OUTCOME_INVALID", "runner outcome schema is invalid")
    outcome = candidate.get("outcome")
    if not isinstance(outcome, str) or outcome.upper() not in RUNNER_OUTCOMES:
        raise WorkflowRuntimeError("RUNNER_OUTCOME_INVALID", "runner outcome is not allowed")
    if candidate.get("validated") is not True:
        raise WorkflowRuntimeError("RUNNER_OUTCOME_INVALID", "runner outcome is not validated")
    normalized = outcome.upper()
    if normalized == "COMPLETE" and candidate.get("evidence_validated") is not True:
        raise WorkflowRuntimeError("RUNNER_OUTCOME_INVALID", "COMPLETE runner outcome requires validated evidence")
    if normalized == "PLANNED":
        if candidate.get("stage_contract_validated") is not True:
            raise WorkflowRuntimeError(
                "RUNNER_OUTCOME_INVALID",
                "PLANNED runner outcome requires a validated Stage Contract",
            )
        if candidate.get("planned_evidence_validated") is not True:
            raise WorkflowRuntimeError(
                "RUNNER_OUTCOME_INVALID",
                "PLANNED runner outcome requires validated planned evidence",
            )
        stage_contract = result.get("stage_contract")
        if not isinstance(stage_contract, Mapping) or stage_contract.get("schema_version") != "stage_contract.v1":
            raise WorkflowRuntimeError(
                "RUNNER_OUTCOME_INVALID",
                "PLANNED runner outcome requires a stage_contract.v1 envelope",
            )
    return normalized


class WorkflowRuntime:
    """Project-local runtime wrapper around existing Core V1 seams."""

    def __init__(
        self,
        workspace_root: str | os.PathLike[str],
        *,
        checkpoint_path: str | os.PathLike[str] | None = None,
        runner: Callable[[Mapping[str, Any]], Mapping[str, Any]] | Any | None = None,
        orchestrator: Any | None = None,
        orchestrator_factory: Callable[[Path], Any] | None = None,
        human_artifact_writer: Callable[..., Any] = write_human_artifacts,
    ) -> None:
        try:
            self.workspace_root = resolve_project_root(workspace_root)
        except ProjectStateError as exc:
            raise WorkflowRuntimeError("WORKSPACE_INVALID", "workspace_root must be an existing directory") from exc
        raw_checkpoint = checkpoint_path if checkpoint_path is not None else DEFAULT_CHECKPOINT_RELATIVE
        candidate = Path(raw_checkpoint).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.workspace_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkflowRuntimeError("CHECKPOINT_PATH_INVALID", "checkpoint must remain inside workspace_root") from exc
        if resolved == self.workspace_root:
            raise WorkflowRuntimeError("CHECKPOINT_PATH_INVALID", "checkpoint must be a file below workspace_root")
        self.checkpoint_path = resolved
        self.runner = runner
        if orchestrator is not None and (
            not callable(getattr(orchestrator, "prepare", None))
            or not callable(getattr(orchestrator, "plan_accepted", None))
        ):
            raise WorkflowRuntimeError("ORCHESTRATOR_INVALID", "orchestrator must expose prepare and plan_accepted")
        if orchestrator_factory is not None and not callable(orchestrator_factory):
            raise WorkflowRuntimeError("ORCHESTRATOR_INVALID", "orchestrator_factory must be callable")
        if not callable(human_artifact_writer):
            raise WorkflowRuntimeError("ARTIFACT_WRITER_INVALID", "human_artifact_writer must be callable")
        self.orchestrator = orchestrator
        self.orchestrator_factory = orchestrator_factory
        self.human_artifact_writer = human_artifact_writer
        self.intake = ProjectRequirementsIntake(self.workspace_root)

    @property
    def project_brief_path(self) -> Path:
        return self.workspace_root / ".research" / "PROJECT_BRIEF.json"

    @property
    def bootstrap_state_path(self) -> Path:
        return self.workspace_root / BOOTSTRAP_STATE_RELATIVE_PATH

    def _validate_stage_controller_claim(self, stage_plan: Mapping[str, Any]) -> dict[str, Any]:
        """Require every planned transition to be backed by StageController state."""

        if not isinstance(stage_plan, Mapping):
            raise WorkflowRuntimeError("ORCHESTRATOR_TRANSITION_INVALID", "planned Stage metadata is missing")
        stage_id = stage_plan.get("stage_id")
        contract_digest = stage_plan.get("contract_digest")
        if not isinstance(stage_id, str) or not stage_id.strip() or not isinstance(contract_digest, str) or not _HEX64.fullmatch(contract_digest):
            raise WorkflowRuntimeError("ORCHESTRATOR_TRANSITION_INVALID", "planned Stage identity or contract digest is invalid")
        state_path_value = stage_plan.get("stage_state_path", ".research/stage-state.json")
        contract_path_value = stage_plan.get("stage_contract_path")
        if not isinstance(state_path_value, str) or not isinstance(contract_path_value, str):
            raise WorkflowRuntimeError("ORCHESTRATOR_TRANSITION_INVALID", "planned Stage paths are missing")
        state_path = self.workspace_root / Path(state_path_value)
        contract_path = self.workspace_root / Path(contract_path_value)
        try:
            state_resolved = state_path.resolve(strict=True)
            contract_resolved = contract_path.resolve(strict=True)
            state_resolved.relative_to(self.workspace_root)
            contract_resolved.relative_to(self.workspace_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkflowRuntimeError("STAGE_PLAN_PATH_INVALID", "planned Stage paths must remain inside the workspace") from exc
        if state_path.is_symlink() or contract_path.is_symlink() or not state_path.is_file() or not contract_path.is_file():
            raise WorkflowRuntimeError("STAGE_CONTROLLER_STATE_MISSING", "planned Stage must have controller and contract files")
        try:
            contract_payload = json.loads(contract_path.read_text(encoding="utf-8"))
            checked_contract = validate_stage_contract_v1(contract_payload)
        except (OSError, UnicodeError, json.JSONDecodeError, ContractValidationError) as exc:
            raise WorkflowRuntimeError("STAGE_CONTRACT_INVALID", "planned Stage contract could not be validated") from exc
        if hashlib.sha256(canonical_json(dict(contract_payload)).encode("utf-8")).hexdigest() != contract_digest:
            raise WorkflowRuntimeError("STAGE_CONTRACT_DIGEST_MISMATCH", "planned Stage contract digest is stale")
        if checked_contract.get("stage_id") != stage_id:
            raise WorkflowRuntimeError("STAGE_PLAN_BINDING_MISMATCH", "planned Stage contract identity differs")
        try:
            controller = StageController.from_state(state_path)
            stage = controller.show_stage(stage_id)
        except (StageControllerError, ContractValidationError, OSError) as exc:
            raise WorkflowRuntimeError("STAGE_CONTROLLER_STATE_INVALID", "StageController state could not prove the planned Stage") from exc
        if stage.get("status") != StageState.PLANNED.value:
            raise WorkflowRuntimeError("STAGE_BOUNDARY_VIOLATION", "planned transition requires StageController status=PLANNED")
        try:
            state_contract = validate_stage_contract_v1(stage.get("contract"))
        except ContractValidationError as exc:
            raise WorkflowRuntimeError("STAGE_CONTROLLER_STATE_INVALID", "StageController contract is invalid") from exc
        if canonical_json(state_contract) != canonical_json(checked_contract):
            raise WorkflowRuntimeError("STAGE_CONTROLLER_STATE_INVALID", "StageController contract differs from planned contract")
        return copy.deepcopy(dict(stage))

    def _resolve_orchestrator(self) -> Any | None:
        if self.orchestrator is not None:
            return self.orchestrator
        if self.orchestrator_factory is None:
            return None
        try:
            candidate = self.orchestrator_factory(self.workspace_root)
        except WorkflowRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - configured factory boundary
            raise _orchestrator_failure(exc, fallback="ORCHESTRATOR_NOT_CONFIGURED") from exc
        if candidate is None or not callable(getattr(candidate, "prepare", None)) or not callable(getattr(candidate, "plan_accepted", None)):
            raise WorkflowRuntimeError("ORCHESTRATOR_NOT_CONFIGURED", "configured orchestrator factory returned an invalid object")
        candidate_root = getattr(candidate, "workspace_root", None)
        if candidate_root is not None:
            try:
                if Path(candidate_root).expanduser().resolve() != self.workspace_root:
                    raise WorkflowRuntimeError("WORKSPACE_IDENTITY_MISMATCH", "orchestrator workspace does not match runtime workspace")
            except (OSError, RuntimeError, ValueError) as exc:
                raise WorkflowRuntimeError("WORKSPACE_IDENTITY_MISMATCH", "orchestrator workspace is invalid") from exc
        self.orchestrator = candidate
        return candidate

    def _load_checkpoint(self) -> dict[str, Any]:
        if not self.checkpoint_path.is_file():
            raise WorkflowRuntimeError("WORKFLOW_NOT_FOUND", "workflow has not been started")
        try:
            payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "workflow checkpoint is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != WORKFLOW_CHECKPOINT_SCHEMA_VERSION:
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "workflow checkpoint schema is invalid")
        _assert_safe(payload, path="checkpoint")
        if payload.get("workspace_root") != self.workspace_root.as_posix():
            raise WorkflowRuntimeError("WORKSPACE_IDENTITY_MISMATCH", "checkpoint workspace does not match configured workspace")
        if payload.get("checkpoint_path") != self.checkpoint_path.relative_to(self.workspace_root).as_posix():
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "checkpoint path identity is inconsistent")
        if payload.get("phase") not in WORKFLOW_PHASES:
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "workflow checkpoint phase is invalid")
        design_state = payload.get("design_review_state", "NOT_REQUIRED")
        if design_state not in DESIGN_REVIEW_STATES:
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "workflow design review state is invalid")
        canonical_relative = self.project_brief_path.relative_to(self.workspace_root).as_posix()
        if payload.get("project_brief_path") != canonical_relative:
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "project brief path identity is inconsistent")
        try:
            brief = self.intake.state
        except ProjectIntakeError as exc:
            raise WorkflowRuntimeError(exc.code, str(exc)) from exc
        if brief is None:
            raise WorkflowRuntimeError("WORKFLOW_NOT_FOUND", "workflow project brief does not exist")
        if payload.get("project_id") != brief.get("project_id"):
            raise WorkflowRuntimeError("PROJECT_IDENTITY_MISMATCH", "checkpoint and project brief identities differ")
        if payload.get("brief_revision") != brief.get("revision"):
            raise WorkflowRuntimeError("CHECKPOINT_STALE", "checkpoint brief revision does not match canonical brief")
        if payload.get("brief_state") != brief.get("state"):
            raise WorkflowRuntimeError("CHECKPOINT_STALE", "checkpoint brief state does not match canonical brief")
        if payload.get("brief_digest") != _digest(brief):
            raise WorkflowRuntimeError("CHECKPOINT_STALE", "checkpoint brief digest does not match canonical brief")
        design_review = payload.get("design_review")
        if design_state == "AWAITING_DESIGN_REVIEW":
            if not isinstance(design_review, Mapping) or design_review.get("state") != "AWAITING_DESIGN_REVIEW":
                raise WorkflowRuntimeError("CHECKPOINT_INVALID", "pending design review summary is missing")
        if design_state == "ACCEPTED":
            if not isinstance(design_review, Mapping) or design_review.get("state") != "ACCEPTED":
                raise WorkflowRuntimeError("CHECKPOINT_INVALID", "accepted design review summary is missing")
            if payload.get("phase") not in {"DESIGN_ACCEPTED", "PLANNED", "RUNNING", "COMPLETE", "BLOCKED"}:
                raise WorkflowRuntimeError("CHECKPOINT_INVALID", "accepted design review has an invalid phase")
            if payload.get("phase") == "DESIGN_ACCEPTED":
                if design_review.get("step15_status") not in {"READY", "NOT_STARTED"}:
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "accepted design review must await Step 15")
                if design_review.get("stage_created") is not False:
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "accepted design review cannot claim a created Stage")
            if payload.get("phase") == "PLANNED":
                if design_review.get("step15_status") != "PLANNED" or design_review.get("stage_created") is not True:
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "planned workflow requires validated Step 15 evidence")
                last_run = payload.get("last_run")
                if (
                    not isinstance(last_run, Mapping)
                    or last_run.get("outcome") != "PLANNED"
                    or last_run.get("planned_evidence_validated") is not True
                    or not isinstance(last_run.get("stage_contract_digest"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", last_run["stage_contract_digest"])
                ):
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "planned workflow evidence receipt is missing")
        if isinstance(design_review, Mapping):
            summary = design_review.get("summary")
            digest = design_review.get("design_digest")
            if not isinstance(summary, Mapping) or not isinstance(digest, str) or digest != design_summary_digest(summary):
                raise WorkflowRuntimeError("CHECKPOINT_INVALID", "design review digest is inconsistent")
        if design_state == "ACCEPTED":
            if not isinstance(design_review, Mapping):
                raise WorkflowRuntimeError("CHECKPOINT_INVALID", "accepted Design Review summary is missing")
            human_accept_receipt = _reify_human_accept_receipt(
                payload,
                brief=brief,
                design_review=design_review,
                workspace_root=self.workspace_root,
                checkpoint_path=self.checkpoint_path,
            )
            if not isinstance(design_review, Mapping):
                raise WorkflowRuntimeError("CHECKPOINT_INVALID", "Human ACCEPT receipt has no accepted Design Review")
            checked_receipt = _validate_human_accept_receipt_metadata(human_accept_receipt, brief, design_review)
            human_decision = design_review.get("human_decision")
            if (
                not isinstance(human_decision, Mapping)
                or human_decision.get("decision") != "ACCEPT"
                or human_decision.get("actor") != checked_receipt.get("actor")
                or (human_decision.get("receipt_digest") is not None and human_decision.get("receipt_digest") != checked_receipt.get("receipt_digest"))
            ):
                raise WorkflowRuntimeError("CHECKPOINT_INVALID", "Human ACCEPT decision does not match its receipt")
        else:
            human_accept_receipt = payload.get("human_accept_receipt")
            review_receipt = design_review.get("human_accept_receipt") if isinstance(design_review, Mapping) else None
            if human_accept_receipt is None:
                human_accept_receipt = review_receipt
        bootstrap_state = payload.get("bootstrap_state")
        execution_handoff = payload.get("execution_handoff")
        if execution_handoff is not None:
            if not isinstance(design_review, Mapping) or design_state != "ACCEPTED":
                raise WorkflowRuntimeError("CHECKPOINT_INVALID", "execution handoff requires an accepted Design Review")
            checked_handoff = _validate_execution_handoff_metadata(
                execution_handoff,
                brief=brief,
                design_review=design_review,
                workspace_root=self.workspace_root,
            )
            if payload.get("phase") != "PLANNED" or checked_handoff.get("status") != "STAGE_PLANNED":
                raise WorkflowRuntimeError("CHECKPOINT_INVALID", "execution handoff is at an invalid checkpoint boundary")
        orchestrator_transition = payload.get("orchestrator_transition")
        if bootstrap_state is not None:
            _validate_bootstrap_state_metadata(bootstrap_state, brief)
        if orchestrator_transition is not None:
            expected = "AWAITING_DESIGN_REVIEW" if payload.get("phase") == "AWAITING_DESIGN_REVIEW" else "STAGE_PLANNED" if payload.get("phase") == "PLANNED" else None
            checked_transition = _validate_orchestrator_transition_metadata(
                orchestrator_transition,
                brief=brief,
                expected_transition=expected,
            )
            transition_bootstrap = checked_transition.get("bootstrap_state")
            transition_handoff = checked_transition.get("execution_handoff")
            if bootstrap_state is not None:
                if not isinstance(transition_bootstrap, Mapping):
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "orchestrator transition/bootstrap metadata is incomplete")
                if transition_bootstrap.get("state_digest") != bootstrap_state.get("state_digest"):
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "orchestrator transition bootstrap digest is inconsistent")
            elif isinstance(transition_handoff, Mapping):
                if execution_handoff is None:
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "orchestrator transition execution handoff is not checkpointed")
                if transition_handoff.get("handoff_digest") != execution_handoff.get("handoff_digest"):
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "orchestrator transition handoff digest is inconsistent")
            elif payload.get("phase") == "PLANNED":
                artifacts = checked_transition.get("artifacts")
                if not isinstance(artifacts, Mapping) or not isinstance(artifacts.get("execution_handoff"), Mapping):
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "orchestrator transition execution handoff is incomplete")
            elif payload.get("phase") == "AWAITING_DESIGN_REVIEW":
                pending_artifacts = checked_transition.get("artifacts")
                pending_review = checked_transition.get("design_review")
                if not isinstance(pending_artifacts, Mapping) or not isinstance(pending_review, Mapping):
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "accepted-design pending transition is incomplete")
                if not isinstance(pending_review.get("gpt_reconsultation_ref"), str) and not isinstance(pending_review.get("research_provenance"), (Mapping, list)):
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "pending transition has no bounded research provenance")
        if payload.get("phase") in {"AWAITING_DESIGN_REVIEW", "PLANNED"} and orchestrator_transition is not None:
            if payload.get("phase") == "AWAITING_DESIGN_REVIEW" and payload.get("design_review") != orchestrator_transition.get("design_review"):
                raise WorkflowRuntimeError("CHECKPOINT_INVALID", "pending Design Review does not match orchestrator transition")
            if payload.get("phase") == "PLANNED":
                last_run = payload.get("last_run")
                stage_plan = orchestrator_transition.get("stage_plan")
                if not isinstance(last_run, Mapping) or not isinstance(stage_plan, Mapping) or last_run.get("stage_contract_digest") != stage_plan.get("contract_digest"):
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "planned Stage evidence does not match orchestrator transition")
                if bootstrap_state is None:
                    try:
                        self._validate_stage_controller_claim(stage_plan)
                    except WorkflowRuntimeError as exc:
                        active_stage_checkpoint = False
                        if exc.code == "STAGE_BOUNDARY_VIOLATION":
                            try:
                                controller = StageController.from_state(self.workspace_root / STAGE_STATE_RELATIVE_PATH)
                                observed_stage = controller.show_stage(stage_plan.get("stage_id"))
                            except (StageControllerError, ContractValidationError, OSError):
                                observed_stage = None
                            if (
                                isinstance(observed_stage, Mapping)
                                and observed_stage.get("status") == StageState.ACTIVE.value
                                and observed_stage.get("contract_digest") == stage_plan.get("contract_digest")
                            ):
                                # Stage start is a separate public operation;
                                # its workflow checkpoint remains PLANNED
                                # until the first Core V1 execution result.
                                active_stage_checkpoint = True
                        if not active_stage_checkpoint:
                            # A historical accepted-design checkpoint may
                            # outlive its closed Stage. Keep persisted
                            # artifacts immutable, but expose the accepted
                            # review at the Step-15 boundary so the next
                            # public workflow_run can register a new Stage.
                            review = payload.get("design_review")
                            decision = review.get("human_decision") if isinstance(review, Mapping) else None
                            if (
                                exc.code != "STAGE_BOUNDARY_VIOLATION"
                                or not isinstance(review, Mapping)
                                or review.get("state") != "ACCEPTED"
                                or not isinstance(decision, Mapping)
                                or decision.get("decision") != "ACCEPT"
                            ):
                                raise
                            recovered_review = copy.deepcopy(dict(review))
                            recovered_review.update(
                                {"step15_status": "READY", "stage_created": False, "stage_started": False}
                            )
                            payload = copy.deepcopy(payload)
                            payload.update(
                                {
                                    "phase": "DESIGN_ACCEPTED",
                                    "next_action": "RUN_WORKFLOW",
                                    "design_review": recovered_review,
                                    "execution_handoff": None,
                                    "orchestrator_transition": None,
                                    "last_run": None,
                                    "last_operation": "stale_planned_checkpoint_recovered",
                                }
                            )
        action = _next_action_view(
            brief,
            phase=str(payload.get("phase")),
            design_review_state=str(design_state),
        )
        if payload.get("next_action") != action["next_action"]:
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "checkpoint next action is inconsistent")
        return payload

    def _save_checkpoint(
        self,
        *,
        operation: str,
        brief_document: Mapping[str, Any],
        phase: str | None = None,
        last_run: Mapping[str, Any] | None = None,
        design_review_state: str = "NOT_REQUIRED",
        design_review: Mapping[str, Any] | None = None,
        bootstrap_state: Mapping[str, Any] | None = None,
        human_accept_receipt: Mapping[str, Any] | None = None,
        execution_handoff: Mapping[str, Any] | None = None,
        orchestrator_transition: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if phase is None:
            phase = (
                "READY"
                if brief_document.get("state") == BriefState.APPROVED.value
                else "WAITING_USER_APPROVAL"
                if brief_document.get("state") == BriefState.WAITING_USER_APPROVAL.value
                else "BLOCKED"
                if brief_document.get("state") == BriefState.CANCELLED.value
                else "INTAKE"
            )
        if phase not in WORKFLOW_PHASES:
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "invalid workflow phase")
        if design_review_state not in DESIGN_REVIEW_STATES:
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "invalid design review state")
        if design_review_state == "AWAITING_DESIGN_REVIEW" and phase != "AWAITING_DESIGN_REVIEW":
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "pending design review must use awaiting phase")
        if design_review_state == "ACCEPTED" and phase not in {
            "DESIGN_ACCEPTED",
            "PLANNED",
            "RUNNING",
            "COMPLETE",
            "BLOCKED",
        }:
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "accepted design review must use design-accepted or later phase")
        action = _next_action_view(
            brief_document,
            phase=phase,
            design_review_state=design_review_state,
        )
        payload: dict[str, Any] = {
            "schema_version": WORKFLOW_CHECKPOINT_SCHEMA_VERSION,
            "workspace_root": self.workspace_root.as_posix(),
            "checkpoint_path": self.checkpoint_path.relative_to(self.workspace_root).as_posix(),
            "project_brief_path": self.project_brief_path.relative_to(self.workspace_root).as_posix(),
            "project_id": brief_document.get("project_id"),
            "brief_revision": brief_document.get("revision", 0),
            "brief_state": brief_document.get("state"),
            "brief_digest": _digest(brief_document),
            "phase": phase,
            "next_action": action["next_action"],
            "design_review_state": design_review_state,
            "last_operation": _bounded_text(operation, "operation", required=True, maximum=64),
            "updated_at": _now(),
        }
        if design_review is not None:
            payload["design_review"] = _bounded_copy(dict(design_review))
        if human_accept_receipt is not None:
            if not isinstance(design_review, Mapping) or design_review_state != "ACCEPTED":
                raise WorkflowRuntimeError("CHECKPOINT_INVALID", "Human ACCEPT receipt requires an accepted Design Review")
            payload["human_accept_receipt"] = _validate_human_accept_receipt_metadata(
                human_accept_receipt,
                brief_document,
                design_review,
            )
        if last_run is not None:
            payload["last_run"] = _bounded_copy(dict(last_run))
        if bootstrap_state is not None:
            payload["bootstrap_state"] = _validate_bootstrap_state_metadata(bootstrap_state, brief_document)
        if execution_handoff is not None:
            if not isinstance(design_review, Mapping) or design_review_state != "ACCEPTED" or phase != "PLANNED":
                raise WorkflowRuntimeError("CHECKPOINT_INVALID", "execution handoff requires a planned accepted Design Review")
            payload["execution_handoff"] = _validate_execution_handoff_metadata(
                execution_handoff,
                brief=brief_document,
                design_review=design_review,
                workspace_root=self.workspace_root,
            )
        if orchestrator_transition is not None:
            expected_transition = "AWAITING_DESIGN_REVIEW" if phase == "AWAITING_DESIGN_REVIEW" else "STAGE_PLANNED" if phase == "PLANNED" else None
            checked_transition = _validate_orchestrator_transition_metadata(
                orchestrator_transition,
                brief=brief_document,
                expected_transition=expected_transition,
            )
            transition_bootstrap = checked_transition.get("bootstrap_state")
            transition_handoff = checked_transition.get("execution_handoff")
            if bootstrap_state is not None:
                if (
                    not isinstance(transition_bootstrap, Mapping)
                    or transition_bootstrap.get("state_digest") != payload["bootstrap_state"].get("state_digest")
                ):
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "orchestrator transition/bootstrap metadata is inconsistent")
            elif execution_handoff is not None:
                if not isinstance(transition_handoff, Mapping) or transition_handoff.get("handoff_digest") != payload["execution_handoff"].get("handoff_digest"):
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "orchestrator transition/execution handoff metadata is inconsistent")
            elif phase == "PLANNED":
                artifacts = checked_transition.get("artifacts")
                if not isinstance(artifacts, Mapping) or not isinstance(artifacts.get("execution_handoff"), Mapping):
                    raise WorkflowRuntimeError("CHECKPOINT_INVALID", "orchestrator transition/execution handoff metadata is incomplete")
            payload["orchestrator_transition"] = checked_transition
        _assert_safe(payload, path="checkpoint")
        _atomic_json_write(self.checkpoint_path, payload)
        return payload

    def record_routing_decision(self, response_text: str) -> dict[str, Any]:
        """Parse and persist one unambiguous GPT routing contract."""
        try:
            decision = parse_research_routing(response_text)
        except ContractValidationError as exc:
            raise WorkflowRuntimeError("ROUTING_CONTRACT_INVALID", str(exc)) from exc
        checkpoint = self._load_checkpoint()
        payload = copy.deepcopy(checkpoint)
        payload["latest_routing_decision"] = decision
        payload["research_routing_status"] = "VALID"
        payload["last_operation"] = "routing_decision"
        payload["updated_at"] = _now()
        _assert_safe(payload, path="checkpoint")
        _atomic_json_write(self.checkpoint_path, payload)
        return copy.deepcopy(decision)

    def run_research_turn(self, consultant: Any, prompt: str, **consult_kwargs: Any) -> dict[str, Any]:
        """Execute exactly one consultation, record its routing, then stop."""
        if not callable(getattr(consultant, "consult", None)):
            raise WorkflowRuntimeError("RESEARCH_CONSULTANT_INVALID", "consultant must expose consult")
        checkpoint = self._load_checkpoint()
        session = checkpoint.get("research_session") if isinstance(checkpoint.get("research_session"), Mapping) else {}
        from .bridge_adapter import normalize_bridge_envelope, BridgeEnvelopeError

        if not isinstance(prompt, str) or not prompt.strip():
            raise WorkflowRuntimeError("RESEARCH_PROMPT_INVALID", "research prompt must be non-empty")
        mode = "continue" if session.get("consultation_id") else "fresh"
        if "mode" in consult_kwargs and consult_kwargs["mode"] not in ({"continue", "normal"} if session else {"fresh"}):
            raise WorkflowRuntimeError("RESEARCH_SESSION_MISMATCH", "mode conflicts with saved research session")
        if "continue_from" in consult_kwargs and consult_kwargs["continue_from"] != session.get("consultation_id"):
            raise WorkflowRuntimeError("RESEARCH_SESSION_MISMATCH", "continuation must use the saved consultation")
        consult_kwargs["mode"] = mode
        if session:
            consult_kwargs["continue_from"] = session["consultation_id"]
        brief = self.intake.state
        project_url = consult_kwargs.get("project_url") or brief.get("brief", {}).get("chatgpt_project_url")
        if session.get("project_url") and project_url != session["project_url"]:
            raise WorkflowRuntimeError("RESEARCH_SESSION_MISMATCH", "research Project binding changed")
        consult_kwargs["project_url"] = project_url
        if checkpoint.get("research_routing_status") == "INVALID":
            prompt = "The previous research body was received, but its final routing contract was invalid. Do not research again. Return only one corrected final routing contract based on the previous conclusions."
        response = consultant.consult(prompt=f"{prompt.rstrip()}\n\n{routing_contract_prompt_text()}", **consult_kwargs)
        try:
            response = normalize_bridge_envelope(response, expected_project_url=project_url, expected_mode=mode, require_receipt=True)
        except BridgeEnvelopeError as exc:
            raise WorkflowRuntimeError(exc.code, str(exc)) from exc
        receipt = response["receipt"]
        if (receipt.get("status") != "complete" or receipt.get("conversation_validated") is not True
                or receipt.get("project_scope_verified") is not True or not receipt.get("conversation_id")):
            raise WorkflowRuntimeError("RESEARCH_RECEIPT_INVALID", "completed verified conversation and Project receipt required")
        if session and (receipt["conversation_id"] != session["conversation_id"] or receipt.get("parent_consultation_id") != session["consultation_id"]):
            raise WorkflowRuntimeError("RESEARCH_SESSION_MISMATCH", "continuation receipt does not match saved lineage")
        payload = copy.deepcopy(checkpoint)
        payload["research_session"] = {
            "project_id": checkpoint["project_id"], "project_url": project_url,
            "consultation_id": response["consultation_id"],
            "conversation_id": receipt["conversation_id"],
        }
        payload["research_routing_status"] = "INVALID"
        payload.pop("latest_routing_decision", None)
        payload["last_operation"] = "research_consultation_received"
        payload["updated_at"] = _now()
        _assert_safe(payload, path="checkpoint")
        _atomic_json_write(self.checkpoint_path, payload)
        # Consultation is a transport fact even if the independent route fails.
        decision = self.record_routing_decision(response["response_text"])
        return {"routing_decision": decision, "consultation": {k: response[k] for k in ("consultation_id", "receipt", "receipt_path") if k in response}}

    def advance_research(self, consultant: Any | None = None, *, prompt: str = "") -> dict[str, Any]:
        """Advance one research orchestration step; never performs more than one request."""
        checkpoint = self._load_checkpoint()
        decision = checkpoint.get("latest_routing_decision")
        if not isinstance(decision, Mapping):
            raise WorkflowRuntimeError("RESEARCH_ROUTING_MISSING", "no routing decision is available")
        action = decision.get("next_action")
        if action in {"TARGETED_RESEARCH", "DESIGN_SYNTHESIS"}:
            if consultant is None:
                raise WorkflowRuntimeError("RESEARCH_CONSULTANT_REQUIRED", "a consultant is required for this advance")
            gaps = ", ".join(decision.get("unresolved_gaps", []))
            task = (f"Continue targeted research focused only on these unresolved gaps: {gaps}." if action == "TARGETED_RESEARCH" else
                    "Stop expanding research. Synthesize the recommended design, meaningful alternatives, stage order, risks, unknowns, and validation plan from the evidence already gathered.")
            return self.run_research_turn(consultant, f"{prompt.rstrip()}\n\n{task}".strip())
        payload = copy.deepcopy(checkpoint)
        gate = "HUMAN_DESIGN_REVIEW" if action == "HUMAN_DESIGN_REVIEW" else "WAITING_FOR_EXPERIMENT" if action == "MINIMAL_EXPERIMENT" else "STOPPED"
        payload["research_orchestrator"] = {"gate": gate, "reason": list(decision.get("unresolved_gaps", []))}
        payload["last_operation"] = "research_advance"
        payload["updated_at"] = _now()
        if gate == "HUMAN_DESIGN_REVIEW":
            try:
                self.human_artifact_writer(self.workspace_root, event={"event": "design_review_requested"}, stage_metadata={"routing_decision": dict(decision), "research_session": payload.get("research_session")})
            except Exception as exc:
                raise WorkflowRuntimeError("HUMAN_REVIEW_WRITE_FAILED", "HUMAN_REVIEW.md could not be generated") from exc
        _assert_safe(payload, path="checkpoint")
        _atomic_json_write(self.checkpoint_path, payload)
        return {"gate": gate, "routing_decision": copy.deepcopy(dict(decision))}

    def save_experiment_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a verified local experiment result and leave the workflow gated."""
        required = ("experiment_id", "status", "inputs", "observations", "result", "limitations", "created_at", "source")
        if not isinstance(result, Mapping) or any(k not in result for k in required):
            raise WorkflowRuntimeError("EXPERIMENT_RESULT_INVALID", "experiment result is missing required fields")
        if result.get("status") not in {"PASS", "FAIL"} or result.get("source") != "codex_local_execution":
            raise WorkflowRuntimeError("EXPERIMENT_RESULT_INVALID", "status/source are invalid")
        payload = copy.deepcopy(self._load_checkpoint())
        artifact = self.workspace_root / ".research" / "experiments" / f"{result['experiment_id']}.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(artifact, dict(result))
        payload["experiment_result"] = {"experiment_id": result["experiment_id"], "path": artifact.relative_to(self.workspace_root).as_posix(), "status": result["status"]}
        payload["last_operation"] = "experiment_result_saved"
        payload["updated_at"] = _now()
        _atomic_json_write(self.checkpoint_path, payload)
        return dict(payload["experiment_result"])

    def save_design_package(self, package: Mapping[str, Any]) -> dict[str, Any]:
        try:
            checked = validate_design_package(package)
        except DesignPackageError as exc:
            raise WorkflowRuntimeError("DESIGN_PACKAGE_INVALID", str(exc)) from exc
        path = self.workspace_root / ".research" / "DESIGN_PACKAGE.json"
        _atomic_json_write(path, checked)
        payload = copy.deepcopy(self._load_checkpoint())
        payload["design_package"] = {"path": path.relative_to(self.workspace_root).as_posix(), "status": "READY"}
        payload["last_operation"] = "design_package_saved"
        payload["updated_at"] = _now()
        _atomic_json_write(self.checkpoint_path, payload)
        return checked

    def _result(self, operation: str, *, brief: Mapping[str, Any], checkpoint: Mapping[str, Any], **extra: Any) -> dict[str, Any]:
        action = _next_action_view(
            brief,
            phase=str(checkpoint.get("phase")),
            design_review_state=str(checkpoint.get("design_review_state", "NOT_REQUIRED")),
        )
        if checkpoint.get("next_action") != action["next_action"]:
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "checkpoint next action is inconsistent")
        requirements_view = _runtime_brief_view(brief.get("brief", {}))
        requirements_view["original_requirement"] = copy.deepcopy(
            brief.get("original_requirement", brief.get("rough_requirement", ""))
        )
        requirements_view["requirement_baseline"] = copy.deepcopy(brief.get("requirement_baseline", {}))
        result: dict[str, Any] = {
            "schema_version": WORKFLOW_RUNTIME_RESULT_SCHEMA_VERSION,
            "operation": operation,
            "workspace_root": self.workspace_root.as_posix(),
            "project_id": brief.get("project_id"),
            "phase": checkpoint.get("phase"),
            "project_brief_path": self.project_brief_path.relative_to(self.workspace_root).as_posix(),
            "checkpoint_path": self.checkpoint_path.relative_to(self.workspace_root).as_posix(),
            "brief_state": brief.get("state"),
            "brief_revision": brief.get("revision", 0),
            "requirements": requirements_view,
            "original_requirement": copy.deepcopy(brief.get("original_requirement", brief.get("rough_requirement", ""))),
            "requirement_baseline": copy.deepcopy(brief.get("requirement_baseline", {})),
            "next_action": action["next_action"],
            "next_tool": action["next_tool"],
            "question": action["question"],
            "question_id": action["question_id"],
            "question_count": 1 if action["question"] is not None else 0,
            "checkpoint": copy.deepcopy(dict(checkpoint)),
        }
        if checkpoint.get("design_review") is not None:
            result["design_review"] = copy.deepcopy(checkpoint.get("design_review"))
        if checkpoint.get("bootstrap_state") is not None:
            result["bootstrap_state"] = copy.deepcopy(checkpoint.get("bootstrap_state"))
        if checkpoint.get("human_accept_receipt") is not None:
            result["human_accept_receipt"] = copy.deepcopy(checkpoint.get("human_accept_receipt"))
        if checkpoint.get("execution_handoff") is not None:
            result["execution_handoff"] = copy.deepcopy(checkpoint.get("execution_handoff"))
        if checkpoint.get("orchestrator_transition") is not None:
            result["orchestrator_transition"] = copy.deepcopy(checkpoint.get("orchestrator_transition"))
        result["stage_created"] = bool(
            isinstance(checkpoint.get("design_review"), Mapping)
            and checkpoint.get("design_review", {}).get("stage_created") is True
        )
        result["stage_started"] = bool(
            isinstance(checkpoint.get("design_review"), Mapping)
            and checkpoint.get("design_review", {}).get("stage_started") is True
        )
        result.update(extra)
        _assert_safe(result, path="result")
        if result["question_count"] > 1:
            raise WorkflowRuntimeError("INTAKE_MULTIPLE_QUESTIONS", "runtime must expose at most one question")
        return result

    def start(self, **payload: Any) -> dict[str, Any]:
        """Start or revise deterministic requirements intake."""

        workspace = payload.pop("workspace", payload.pop("workspace_root", None))
        if workspace is not None and Path(workspace).expanduser().resolve() != self.workspace_root:
            raise WorkflowRuntimeError("WORKSPACE_IDENTITY_MISMATCH", "request workspace does not match runtime workspace")
        mode = payload.pop("mode", None)
        rough_requirement = payload.pop("rough_requirement", payload.pop("requirement", None))
        for alias in ("raw_requirement", "original_requirement", "user_requirement"):
            if rough_requirement is None and alias in payload:
                rough_requirement = payload.pop(alias)
            else:
                payload.pop(alias, None)
        supplied_brief = payload.pop("brief", None)
        # Accept the high-level fields directly in the MCP tool payload.
        direct = {key: payload.pop(key) for key in list(payload) if key in _BRIEF_FIELDS}
        if payload:
            raise WorkflowRuntimeError("INPUT_INVALID", f"unknown workflow_start field(s): {sorted(payload)!r}")
        if direct:
            supplied_brief = {**(dict(supplied_brief) if isinstance(supplied_brief, Mapping) else {}), **direct}
        existing = self.intake.state
        has_input = bool(direct) or supplied_brief is not None or rough_requirement is not None or mode is not None
        normalized_brief = (
            _normalize_brief_input(supplied_brief)
            if has_input or existing is None
            else None
        )
        brief_state = _capture_transaction_file(self.project_brief_path, label="project brief")
        checkpoint_state = _capture_transaction_file(self.checkpoint_path, label="workflow checkpoint")
        try:
            intake_result = self.intake.initialize(
                mode=mode,
                rough_requirement=rough_requirement,
                brief=normalized_brief,
                entrypoint="workflow_start",
            )
        except ProjectIntakeError as exc:
            raise WorkflowRuntimeError(exc.code, str(exc)) from exc
        document = intake_result["project_brief"]
        derived_quarantine: _DerivedArtifactQuarantine | None = None
        try:
            if _brief_changed(existing, document):
                derived_quarantine = _quarantine_derived_artifacts(
                    self.workspace_root,
                    operation="workflow_start",
                    old_brief=existing,
                    new_brief=document,
                )
            checkpoint = self._save_checkpoint(operation="workflow_start", brief_document=document)
        except Exception as exc:  # noqa: BLE001 - transaction boundary
            try:
                _rollback_workflow_mutation(
                    brief_state=brief_state,
                    checkpoint_state=checkpoint_state,
                    quarantine=derived_quarantine,
                )
            except WorkflowRuntimeError as rollback_error:
                raise rollback_error from exc
            raise
        if derived_quarantine is not None:
            derived_view = derived_quarantine.public_view()
            derived_quarantine.commit()
        else:
            derived_view = None
        return self._result(
            "workflow_start",
            brief=document,
            checkpoint=checkpoint,
            intake=intake_result,
            derived_artifact_quarantine=derived_view,
        )

    def answer(
        self,
        *,
        answer: Any = None,
        question_id: str | None = None,
        update: Mapping[str, Any] | None = None,
        approve: bool = False,
        mode: str | None = None,
        feedback: Any = None,
        design_summary: Mapping[str, Any] | None = None,
        trigger: str | None = None,
        consultation_ref: str | None = None,
    ) -> dict[str, Any]:
        """Apply one user answer/update, or explicit approval, through intake."""

        checkpoint = self._load_checkpoint()
        previous_document = self.intake.state
        brief_state = _capture_transaction_file(self.project_brief_path, label="project brief")
        checkpoint_state = _capture_transaction_file(self.checkpoint_path, label="workflow checkpoint")
        if not isinstance(approve, bool):
            raise WorkflowRuntimeError("INPUT_INVALID", "approve must be a boolean")
        selected_mode: str | None = None
        if mode is not None:
            if not isinstance(mode, str) or mode.strip().upper() not in WORKFLOW_ANSWER_MODES:
                raise WorkflowRuntimeError(
                    "MODE_INVALID",
                    "workflow_answer mode must be ANSWER, UPDATE, APPROVE, DESIGN_SUBMIT, DESIGN_ACCEPT, DESIGN_FEEDBACK, or DESIGN_REVIEW",
                )
            selected_mode = mode.strip().upper()
        if selected_mode == "DESIGN_SUBMIT":
            if design_summary is None or not isinstance(design_summary, Mapping) or not isinstance(trigger, str):
                raise WorkflowRuntimeError("INPUT_INVALID", "DESIGN_SUBMIT requires design_summary and trigger")
            if answer is not None or update is not None or approve or feedback is not None or consultation_ref is None:
                raise WorkflowRuntimeError("INPUT_INVALID", "DESIGN_SUBMIT requires design fields and consultation_ref only")
            return self.submit_design(design_summary, trigger=trigger, consultation_ref=consultation_ref)
        if selected_mode in {"DESIGN_ACCEPT", "DESIGN_FEEDBACK", "DESIGN_REVIEW"}:
            if trigger is not None or consultation_ref is not None or question_id is not None:
                raise WorkflowRuntimeError("INPUT_INVALID", "design review decision cannot include trigger, consultation_ref, or question_id")
            if selected_mode == "DESIGN_ACCEPT":
                if not approve or answer is not None or update is not None or feedback is not None or design_summary is not None:
                    raise WorkflowRuntimeError("INPUT_INVALID", "DESIGN_ACCEPT requires approve=true only")
                return self.accept_design_review(checkpoint=checkpoint)
            if answer is not None or update is not None or design_summary is not None:
                raise WorkflowRuntimeError("INPUT_INVALID", "design feedback cannot include intake answer, update, or design")
            if selected_mode == "DESIGN_FEEDBACK" and (feedback is None or approve):
                raise WorkflowRuntimeError("INPUT_INVALID", "DESIGN_FEEDBACK requires feedback")
            if selected_mode == "DESIGN_REVIEW" and ((approve and feedback is not None) or (not approve and feedback is None)):
                raise WorkflowRuntimeError("INPUT_INVALID", "DESIGN_REVIEW requires exactly one of approve or feedback")
            if approve:
                return self.accept_design_review(checkpoint=checkpoint)
            if feedback is None:
                raise WorkflowRuntimeError("INPUT_INVALID", "design feedback is required")
            return self.record_design_feedback(feedback, checkpoint=checkpoint)
        if feedback is not None or design_summary is not None or trigger is not None or consultation_ref is not None:
            raise WorkflowRuntimeError("INPUT_INVALID", "design review fields require a design review mode")
        supplied = int(answer is not None) + int(update is not None) + int(approve)
        if supplied > 1:
            raise WorkflowRuntimeError("INPUT_INVALID", "answer, update, and approve are mutually exclusive")
        if selected_mode == "ANSWER":
            if answer is None or update is not None or approve:
                raise WorkflowRuntimeError("INPUT_INVALID", "ANSWER mode requires answer only")
        elif selected_mode == "UPDATE":
            if update is None or answer is not None or approve:
                raise WorkflowRuntimeError("INPUT_INVALID", "UPDATE mode requires update only")
        elif selected_mode == "APPROVE":
            if answer is not None or update is not None:
                raise WorkflowRuntimeError("INPUT_INVALID", "APPROVE mode cannot include answer or update")
            approve = True
        elif supplied != 1:
            raise WorkflowRuntimeError("INPUT_INVALID", "answer, update, or approve is required")
        if question_id is not None and answer is None:
            raise WorkflowRuntimeError("INPUT_INVALID", "question_id is only valid with answer")
        try:
            if approve:
                intake_result = self.intake.approve(actor="workflow-user", rationale="explicit workflow approval")
            elif update is not None:
                intake_result = self.intake.update(_normalize_brief_input(update, include_defaults=False))
            elif answer is not None:
                if isinstance(answer, Mapping):
                    current = self.intake.current_question or {}
                    expected_id = str(current.get("id", ""))
                    normalized_answer = _normalize_brief_input(answer, include_defaults=False)
                    if len(normalized_answer) == 1 and "answer" in normalized_answer and expected_id:
                        normalized_answer = {expected_id: normalized_answer["answer"]}
                    elif expected_id == "success_criteria" and "acceptance_criteria" in normalized_answer:
                        normalized_answer = {
                            **{key: value for key, value in normalized_answer.items() if key != "acceptance_criteria"},
                            "success_criteria": normalized_answer["acceptance_criteria"],
                        }
                    intake_result = self.intake.answer(
                        normalized_answer,
                        question_id=question_id,
                    )
                else:
                    intake_result = self.intake.answer(answer, question_id=question_id)
            else:  # The mutually-exclusive input check above makes this unreachable.
                raise WorkflowRuntimeError("INPUT_INVALID", "answer, update, or approve is required")
        except ProjectIntakeError as exc:
            raise WorkflowRuntimeError(exc.code, str(exc)) from exc
        document = intake_result["project_brief"]
        derived_quarantine: _DerivedArtifactQuarantine | None = None
        try:
            if _brief_changed(previous_document, document):
                derived_quarantine = _quarantine_derived_artifacts(
                    self.workspace_root,
                    operation="workflow_answer",
                    old_brief=previous_document,
                    new_brief=document,
                )
            checkpoint = self._save_checkpoint(operation="workflow_answer", brief_document=document)
        except Exception as exc:  # noqa: BLE001 - transaction boundary
            try:
                _rollback_workflow_mutation(
                    brief_state=brief_state,
                    checkpoint_state=checkpoint_state,
                    quarantine=derived_quarantine,
                )
            except WorkflowRuntimeError as rollback_error:
                raise rollback_error from exc
            raise
        if derived_quarantine is not None:
            derived_view = derived_quarantine.public_view()
            derived_quarantine.commit()
        else:
            derived_view = None
        return self._result(
            "workflow_answer",
            brief=document,
            checkpoint=checkpoint,
            intake=intake_result,
            derived_artifact_quarantine=derived_view,
        )

    def submit_design(
        self,
        design_summary: Mapping[str, Any],
        *,
        trigger: str,
        consultation_ref: str | None = None,
    ) -> dict[str, Any]:
        """Persist a proposed Stage design and pause for explicit human review.

        This is deliberately a workflow-level checkpoint.  It does not call
        Step 15 or create a Stage.  If a prior reviewer supplied feedback, a
        bounded reference to the GPT re-consultation is required before a new
        design revision can be submitted.
        """

        checkpoint = self._load_checkpoint()
        brief = self.intake.state
        if brief is None or brief.get("state") != BriefState.APPROVED.value:
            raise WorkflowRuntimeError("HUMAN_APPROVAL_REQUIRED", "design review requires an approved project brief")
        normalized_trigger = _bounded_text(trigger, "trigger", required=True, maximum=64).upper()
        if not design_review_trigger_required(normalized_trigger):
            raise WorkflowRuntimeError(
                "DESIGN_REVIEW_NOT_REQUIRED",
                "workflow design review is triggered only by INITIAL_ARCHITECTURE or MAJOR_REPLAN",
            )
        previous = checkpoint.get("design_review")
        previous_state = str(checkpoint.get("design_review_state", "NOT_REQUIRED"))
        if previous_state == "AWAITING_DESIGN_REVIEW" and isinstance(previous, Mapping):
            if previous.get("feedback_required") is not True:
                raise WorkflowRuntimeError("DESIGN_REVIEW_ALREADY_PENDING", "design review is already awaiting human decision")
            if not isinstance(consultation_ref, str) or not consultation_ref.strip():
                raise WorkflowRuntimeError(
                    "DESIGN_REVIEW_RECONSULT_REQUIRED",
                    "updated design must include a bounded GPT re-consultation reference after user feedback",
                )
        if consultation_ref is not None:
            consultation_ref = _bounded_text(consultation_ref, "consultation_ref", required=True, maximum=256)
        try:
            normalized = normalize_design_summary(design_summary)
        except DesignReviewError as exc:
            raise WorkflowRuntimeError(exc.code, str(exc)) from exc
        revision = 1
        if isinstance(previous, Mapping) and isinstance(previous.get("revision"), int):
            revision = previous["revision"] + 1
        review = {
            "schema_version": "workflow_design_review.v1",
            "state": "AWAITING_DESIGN_REVIEW",
            "trigger": normalized_trigger,
            "revision": revision,
            "design_digest": design_summary_digest(normalized),
            "summary": normalized,
            "feedback": None,
            "feedback_digest": None,
            "feedback_required": False,
            "gpt_reconsultation_ref": consultation_ref,
            "stage_created": False,
            "stage_started": False,
            "step15_status": "AWAITING_DESIGN_REVIEW",
            "human_decision": None,
        }
        checkpoint = self._save_checkpoint(
            operation="design_review_requested",
            brief_document=brief,
            phase="AWAITING_DESIGN_REVIEW",
            design_review_state="AWAITING_DESIGN_REVIEW",
            design_review=review,
        )
        return self._result(
            "design_review_requested",
            brief=brief,
            checkpoint=checkpoint,
            design_review=copy.deepcopy(review),
            stage_created=False,
        )

    def record_design_feedback(
        self,
        feedback: Any,
        *,
        checkpoint: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record human design feedback without approving or changing a design."""

        current = dict(checkpoint) if checkpoint is not None else self._load_checkpoint()
        if current.get("phase") != "AWAITING_DESIGN_REVIEW" or current.get("design_review_state") != "AWAITING_DESIGN_REVIEW":
            raise WorkflowRuntimeError("DESIGN_REVIEW_NOT_PENDING", "no workflow design review is awaiting feedback")
        review = current.get("design_review")
        if not isinstance(review, Mapping):
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "pending design review summary is missing")
        if isinstance(feedback, str):
            bounded_feedback: Any = _bounded_text(feedback, "feedback", required=True, maximum=2000)
        elif isinstance(feedback, Mapping):
            try:
                _assert_safe(feedback, path="design_feedback")
                bounded_feedback = _bounded_copy(dict(feedback))
            except WorkflowRuntimeError:
                raise
        else:
            raise WorkflowRuntimeError("INPUT_INVALID", "design feedback must be a bounded string or object")
        updated = copy.deepcopy(dict(review))
        updated["feedback"] = bounded_feedback
        updated["feedback_digest"] = _digest(bounded_feedback)
        updated["feedback_required"] = True
        updated["human_decision"] = None
        updated["step15_status"] = "AWAITING_DESIGN_REVIEW"
        brief = self.intake.state
        if brief is None:
            raise WorkflowRuntimeError("BRIEF_NOT_FOUND", "workflow requirements have not been started")
        saved = self._save_checkpoint(
            operation="design_review_feedback",
            brief_document=brief,
            phase="AWAITING_DESIGN_REVIEW",
            design_review_state="AWAITING_DESIGN_REVIEW",
            design_review=updated,
            bootstrap_state=current.get("bootstrap_state"),
        )
        return self._result(
            "design_review_feedback",
            brief=brief,
            checkpoint=saved,
            design_review=copy.deepcopy(updated),
            stage_created=False,
        )

    def accept_design_review(
        self,
        *,
        checkpoint: Mapping[str, Any] | None = None,
        actor: str = "workflow-user",
    ) -> dict[str, Any]:
        """Accept a pending design and expose the Step 15-ready boundary.

        ``stage_created`` remains false and Step 15 is only marked READY.  The
        next actual StageController operation is still owned by the existing
        Step 15/Core seam; only a validated runner outcome may mark PLANNED.
        """

        current = dict(checkpoint) if checkpoint is not None else self._load_checkpoint()
        if current.get("phase") == "DESIGN_ACCEPTED" and current.get("design_review_state") == "ACCEPTED":
            review = current.get("design_review")
            decision = review.get("human_decision") if isinstance(review, Mapping) else None
            if isinstance(review, Mapping) and review.get("state") == "ACCEPTED" and isinstance(decision, Mapping) and decision.get("decision") == "ACCEPT":
                # Reuse an existing Human ACCEPT after the stale planned
                # checkpoint recovery projection.  This is intentionally
                # non-persistent; workflow_run owns the new Stage planning
                # transition and its receipts.
                accepted = copy.deepcopy(dict(review))
                accepted.update({"step15_status": "READY", "stage_created": False, "stage_started": False})
                recovered = copy.deepcopy(current)
                recovered["design_review"] = accepted
                recovered["phase"] = "DESIGN_ACCEPTED"
                recovered["next_action"] = "RUN_WORKFLOW"
                return self._result(
                    "design_review_accepted",
                    brief=self.intake.state or {},
                    checkpoint=recovered,
                    design_review=accepted,
                    stage_created=False,
                )
        if current.get("phase") != "AWAITING_DESIGN_REVIEW" or current.get("design_review_state") != "AWAITING_DESIGN_REVIEW":
            raise WorkflowRuntimeError("DESIGN_REVIEW_NOT_PENDING", "no workflow design review is awaiting approval")
        review = current.get("design_review")
        if not isinstance(review, Mapping):
            raise WorkflowRuntimeError("CHECKPOINT_INVALID", "pending design review summary is missing")
        if review.get("feedback_required") is True:
            raise WorkflowRuntimeError(
                "DESIGN_REVIEW_RECONSULT_REQUIRED",
                "design feedback must be returned to GPT and a revised design reviewed before approval",
            )
        checked_actor = _bounded_text(actor, "actor", required=True, maximum=128)
        brief = self.intake.state
        if brief is None:
            raise WorkflowRuntimeError("BRIEF_NOT_FOUND", "workflow requirements have not been started")
        human_accept_receipt = build_human_accept_receipt(
            project_id=str(brief.get("project_id")),
            brief_digest=_digest(brief),
            design_digest=str(review.get("design_digest")),
            actor=checked_actor,
            source="runtime_design_review_acceptance",
        )
        receipt_path = self.workspace_root / HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH
        if receipt_path.exists() or receipt_path.is_symlink():
            if receipt_path.is_symlink() or not receipt_path.is_file():
                raise WorkflowRuntimeError("HUMAN_ACCEPT_RECEIPT_INVALID", "canonical Human ACCEPT receipt is not a regular file")
            try:
                persisted_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise WorkflowRuntimeError("HUMAN_ACCEPT_RECEIPT_INVALID", "canonical Human ACCEPT receipt is unreadable") from exc
            checked_persisted = _validate_human_accept_receipt_metadata(persisted_receipt, brief, review)
            if checked_persisted != human_accept_receipt:
                raise WorkflowRuntimeError("HUMAN_ACCEPT_RECEIPT_BINDING_MISMATCH", "canonical Human ACCEPT receipt differs from this decision")
        else:
            _atomic_json_write(receipt_path, human_accept_receipt)
        accepted = copy.deepcopy(dict(review))
        accepted.update(
            {
                "state": "ACCEPTED",
                "human_decision": {
                    "decision": "ACCEPT",
                    "actor": checked_actor,
                    "receipt_digest": human_accept_receipt["receipt_digest"],
                },
                "human_accept_receipt": copy.deepcopy(human_accept_receipt),
                # Acceptance authorizes the next Step 15 operation; it does
                # not attest that Step 15 created or validated a Stage yet.
                "step15_status": "READY",
                "stage_created": False,
                "stage_started": False,
            }
        )
        saved = self._save_checkpoint(
            operation="design_review_accepted",
            brief_document=brief,
            phase="DESIGN_ACCEPTED",
            design_review_state="ACCEPTED",
            design_review=accepted,
            bootstrap_state=current.get("bootstrap_state"),
            human_accept_receipt=human_accept_receipt,
            orchestrator_transition=current.get("orchestrator_transition"),
        )
        return self._result(
            "design_review_accepted",
            brief=brief,
            checkpoint=saved,
            design_review=copy.deepcopy(accepted),
            stage_created=False,
        )

    def status(self) -> dict[str, Any]:
        checkpoint = self._load_checkpoint()
        try:
            document = validate_project_brief(self.intake.state or {}, self.workspace_root)
        except ProjectIntakeError as exc:
            raise WorkflowRuntimeError(exc.code, str(exc)) from exc
        if checkpoint.get("project_id") != document.get("project_id"):
            raise WorkflowRuntimeError("PROJECT_IDENTITY_MISMATCH", "checkpoint and project brief identities differ")
        return self._result("workflow_status", brief=document, checkpoint=checkpoint)

    def resume(self) -> dict[str, Any]:
        """Rehydrate solely from the two project-local persisted documents."""

        return self.status() | {"operation": "workflow_resume", "resumed": True}

    def _build_bootstrap_evidence(
        self,
        *,
        brief: Mapping[str, Any],
        bootstrap: Mapping[str, Any],
        transition: Mapping[str, Any],
        stage_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build a read-only, allow-listed projection for HUMAN_REVIEW.

        The orchestrator has already verified these inputs before this method
        is called.  We still bind the files to the validated Bootstrap state
        here, so the renderer never consumes a stale or arbitrary JSON file.
        Missing evidence is represented explicitly; it does not become a
        synthetic Stage Contract or a lifecycle transition.
        """

        brief_source = brief.get("brief") if isinstance(brief.get("brief"), Mapping) else brief
        brief_view = _runtime_brief_view(brief_source)
        bounded_brief = {
            "project_goal": _bounded_evidence_text(brief_view.get("project_goal")),
            "observable_outcome": _bounded_evidence_text(brief_view.get("observable_outcome")),
            "scope": _bounded_evidence_list(brief_view.get("scope")),
            "non_goals": _bounded_evidence_list(brief_view.get("non_goals")),
            "constraints": _bounded_evidence_list(brief_view.get("constraints")),
            "available_assets": _bounded_evidence_list(brief_view.get("available_assets")),
            "acceptance": _bounded_evidence_list(brief_view.get("acceptance")),
            "human_preferences": _bounded_evidence_list(brief_view.get("human_preferences")),
        }
        missing: list[str] = []
        expected_brief_digest = _digest(brief)
        if bootstrap.get("brief_digest") != expected_brief_digest:
            missing.append("approved_brief_digest_mismatch")

        def load_verified(
            relative: Path,
            *,
            section: str,
            expected_schema: str,
            expected_marker: str,
            expected_status: str,
            expected_file_digest_key: str,
        ) -> Mapping[str, Any] | None:
            path = self.workspace_root / relative
            try:
                path.relative_to(self.workspace_root)
            except ValueError:
                missing.append(f"{section}_path_invalid")
                return None
            if path.is_symlink() or not path.is_file():
                missing.append(f"{section}_missing")
                return None
            try:
                raw = path.read_bytes()
            except OSError:
                missing.append(f"{section}_unreadable")
                return None
            if len(raw) > _TRANSACTION_STATE_MAX_BYTES:
                missing.append(f"{section}_too_large")
                return None
            actual_file_digest = hashlib.sha256(raw).hexdigest()
            state_section = bootstrap.get(section)
            expected_file_digest = (
                state_section.get(expected_file_digest_key)
                if isinstance(state_section, Mapping)
                else None
            )
            if expected_file_digest != actual_file_digest:
                missing.append(f"{section}_digest_mismatch")
                return None
            try:
                document = json.loads(raw.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                missing.append(f"{section}_invalid_json")
                return None
            if not isinstance(document, Mapping):
                missing.append(f"{section}_not_object")
                return None
            if (
                document.get("schema_version") != expected_schema
                or document.get("marker") != expected_marker
                or document.get("status") != expected_status
                or document.get("project_id") != brief.get("project_id")
                or document.get("brief_digest") != expected_brief_digest
            ):
                missing.append(f"{section}_identity_invalid")
                return None
            return document

        discovery = load_verified(
            DISCOVERY_REPORT_RELATIVE_PATH,
            section="discovery",
            expected_schema=DISCOVERY_REPORT_SCHEMA_VERSION,
            expected_marker=DISCOVERY_REPORT_MARKER,
            expected_status=DISCOVERY_STATUS,
            expected_file_digest_key="report_digest",
        )
        blueprint = load_verified(
            BLUEPRINT_MANIFEST_RELATIVE_PATH,
            section="blueprint",
            expected_schema=BLUEPRINT_MANIFEST_SCHEMA_VERSION,
            expected_marker=PROJECT_BLUEPRINT_MARKER,
            expected_status=PROJECT_BLUEPRINT_STATUS,
            expected_file_digest_key="manifest_digest",
        )
        if isinstance(discovery, Mapping):
            if discovery.get("evidence_digest") != bootstrap.get("discovery_digest"):
                missing.append("discovery_evidence_digest_mismatch")
            discovery_evidence_digest = discovery.get("evidence_digest")
        else:
            discovery_evidence_digest = None
        if isinstance(blueprint, Mapping):
            if blueprint.get("discovery_digest") != discovery_evidence_digest:
                missing.append("blueprint_discovery_digest_mismatch")
            if blueprint.get("blueprint_digest") != bootstrap.get("blueprint_digest"):
                missing.append("blueprint_evidence_digest_mismatch")

        def primary_summary(value: Any) -> str | None:
            return _bounded_evidence_text(value.get("summary")) if isinstance(value, Mapping) else _bounded_evidence_text(value)

        def project_inference_values(document: Mapping[str, Any] | None) -> list[str]:
            if not isinstance(document, Mapping):
                return []
            explicit = _bounded_evidence_list(document.get("project_inferences"))
            if explicit:
                return explicit
            values: list[str] = []
            for item in _bounded_evidence_list(document.get("relevant_non_repo_methods")) + _bounded_evidence_list(document.get("anti_tunnel_architecture_risks")):
                if "项目推断" in item and item not in values:
                    values.append(item)
            why = _bounded_evidence_text(document.get("why_primary"))
            if why and "项目推断" in why and why not in values:
                values.append(why)
            return values[:32]

        def evidence_gap_values(document: Mapping[str, Any] | None) -> list[str]:
            if not isinstance(document, Mapping):
                return []
            values = _bounded_evidence_list(document.get("evidence_gaps"))
            for field in ("facts_needing_local_verification", "open_questions", "important_risks"):
                for item in _bounded_evidence_list(document.get(field)):
                    if ("证据不足" in item or field != "important_risks") and item not in values:
                        values.append(item)
            return values[:32]

        discovery_primary = discovery.get("primary_recommendation") if isinstance(discovery, Mapping) else None
        blueprint_primary = blueprint.get("primary_route") if isinstance(blueprint, Mapping) else None
        blueprint_primary_view = None
        if isinstance(blueprint_primary, Mapping):
            blueprint_primary_view = {
                "title": _bounded_evidence_text(blueprint_primary.get("title") or blueprint_primary.get("name") or blueprint_primary.get("route_id")),
                "summary": _bounded_evidence_text(blueprint_primary.get("summary") or blueprint_primary.get("description")),
                "steps": _bounded_evidence_list(blueprint_primary.get("steps")),
            }
            blueprint_primary_view = {key: item for key, item in blueprint_primary_view.items() if item not in (None, [], {})}
        external_evidence: list[str] = []
        for document in (discovery, blueprint):
            if isinstance(document, Mapping):
                for item in _bounded_evidence_list(document.get("external_evidence")):
                    if item not in external_evidence:
                        external_evidence.append(item)
        project_inferences = project_inference_values(discovery) + project_inference_values(blueprint)
        project_inferences = list(dict.fromkeys(project_inferences))[:32]
        evidence_gaps = list(dict.fromkeys(evidence_gap_values(discovery) + evidence_gap_values(blueprint)))[:32]
        if not external_evidence:
            evidence_gaps.append("证据不足：Discovery/Blueprint 未提供可验证的论文、成熟 OSS 或工程案例证据。")

        stage = stage_metadata if isinstance(stage_metadata, Mapping) else {}
        contract = stage.get("contract") if isinstance(stage.get("contract"), Mapping) else {}
        stage_created = transition.get("stage_created") is True
        stage_contract = {
            "created": stage_created,
            "status": "CREATED" if stage_created else "NOT_CREATED",
            "brief_scope": bounded_brief["scope"],
            "allowed_paths": _bounded_evidence_list(contract.get("allowed_paths") or stage.get("allowed_paths")),
            "protected_paths": _bounded_evidence_list(contract.get("protected_paths") or stage.get("protected_paths")),
        }
        artifacts = {
            "brief_path": ".research/PROJECT_BRIEF.json",
            "discovery_report_path": DISCOVERY_REPORT_RELATIVE_PATH.as_posix(),
            "blueprint_manifest_path": BLUEPRINT_MANIFEST_RELATIVE_PATH.as_posix(),
            "brief_digest": expected_brief_digest,
            "discovery_report_digest": (
                bootstrap.get("discovery", {}).get("report_digest")
                if isinstance(bootstrap.get("discovery"), Mapping)
                else None
            ),
            "blueprint_manifest_digest": (
                bootstrap.get("blueprint", {}).get("manifest_digest")
                if isinstance(bootstrap.get("blueprint"), Mapping)
                else None
            ),
        }
        artifacts = {key: value for key, value in artifacts.items() if isinstance(value, str) and value}
        return {
            "status": "VERIFIED" if not missing and discovery is not None and blueprint is not None else "PARTIAL",
            "validated": not missing and discovery is not None and blueprint is not None and bootstrap.get("project_scope_verified") is True,
            "missing": list(dict.fromkeys(missing)),
            "brief": bounded_brief,
            "discovery": {
                "status": discovery.get("status") if isinstance(discovery, Mapping) else None,
                "evidence_digest": discovery.get("evidence_digest") if isinstance(discovery, Mapping) else None,
                "candidate_zero": primary_summary(discovery_primary),
                "primary_recommendation": primary_summary(discovery_primary),
                "alternatives": _bounded_evidence_list(discovery.get("alternatives")) if isinstance(discovery, Mapping) else [],
                "external_evidence": external_evidence,
                "project_inferences": project_inference_values(discovery),
                "evidence_gaps": evidence_gap_values(discovery),
            },
            "blueprint": {
                "status": blueprint.get("status") if isinstance(blueprint, Mapping) else None,
                "evidence_digest": blueprint.get("blueprint_digest") if isinstance(blueprint, Mapping) else None,
                "candidate_zero": _bounded_evidence_text(blueprint_primary.get("title") if isinstance(blueprint_primary, Mapping) else None),
                "primary_route": blueprint_primary_view,
                "recommended_route": primary_summary(blueprint_primary),
                "alternatives": _bounded_evidence_list(blueprint.get("alternatives")) if isinstance(blueprint, Mapping) else [],
                "external_evidence": _bounded_evidence_list(blueprint.get("external_evidence")) if isinstance(blueprint, Mapping) else [],
                "project_inferences": project_inference_values(blueprint),
                "evidence_gaps": evidence_gap_values(blueprint),
                "validation_plan": _bounded_evidence_list(blueprint.get("validation_plan")) if isinstance(blueprint, Mapping) else [],
            },
            "stage_contract": stage_contract,
            "artifacts": artifacts,
        }

    def _write_orchestrator_artifacts(
        self,
        *,
        brief: Mapping[str, Any],
        transition: Mapping[str, Any],
        review: Mapping[str, Any],
        event_name: str,
        decision: str,
        stage_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        bootstrap = transition.get("bootstrap_state")
        if not isinstance(bootstrap, Mapping):
            # New Research-to-Design transitions carry bounded requirement,
            # design, and provenance descriptors directly.  Keep this writer
            # read-only and avoid manufacturing a legacy Bootstrap envelope.
            attempts = transition.get("attempts") if isinstance(transition.get("attempts"), list) else []
            consultation = {
                "type": "DESIGN_APPROVAL_HANDOFF",
                "mode": "RECORDED",
                "status": "COMPLETE",
                "request_count": 0,
                "evidence_digest": _digest(transition.get("artifacts", {})),
                "reason": "bounded approved-design transition for Stage planning",
                "resulting_action": "REQUEST_DESIGN_REVIEW" if decision == "HUMAN_GATE" else "RUN_WORKFLOW",
            }
            metadata = {
                "project_id": brief.get("project_id"),
                "brief_digest": _digest(brief),
                "event": event_name,
                "workflow_decision": decision,
                "next_action": consultation["resulting_action"],
                "stage": dict(stage_metadata or {"status": "AWAITING_DESIGN_REVIEW", "stage_status": "AWAITING_DESIGN_REVIEW"}),
                "design_review": {
                    "revision": review.get("revision"),
                    "design_digest": review.get("design_digest"),
                    "state": review.get("state"),
                    "stage_created": review.get("stage_created"),
                    "stage_started": review.get("stage_started"),
                },
                "execution_handoff_evidence": copy.deepcopy(dict(transition.get("artifacts", {})))
                if isinstance(transition.get("artifacts"), Mapping)
                else {},
            }
            event = {
                "event": event_name,
                "kind": event_name,
                "decision": decision,
                "resulting_action": consultation["resulting_action"],
                "revision": review.get("revision"),
                "rationale": "bounded approved-design transition; no Stage execution started",
            }
            try:
                self.human_artifact_writer(
                    self.workspace_root,
                    metadata,
                    event=event,
                    stage_metadata=metadata["stage"],
                    consultation_metadata=consultation,
                    design_review_metadata=review,
                )
            except HumanArtifactError as exc:
                raise WorkflowRuntimeError("HUMAN_ARTIFACT_WRITE_FAILED", str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - configured writer boundary
                raise WorkflowRuntimeError("HUMAN_ARTIFACT_WRITE_FAILED", "approved-design human artifacts could not be written") from exc
            return
        attempts = transition.get("attempts") if isinstance(transition.get("attempts"), list) else []
        consultation = {
            "type": "BOOTSTRAP",
            "mode": "FRESH",
            "status": "COMPLETE",
            "request_count": 1 if any(isinstance(item, Mapping) and item.get("request_count") == 1 for item in attempts) else 0,
            "evidence_digest": bootstrap.get("state_digest"),
            "reason": "bounded Bootstrap evidence for Design Review",
            "resulting_action": "REQUEST_DESIGN_REVIEW" if decision == "HUMAN_GATE" else "RUN_WORKFLOW",
        }
        bootstrap_evidence = self._build_bootstrap_evidence(
            brief=brief,
            bootstrap=bootstrap,
            transition=transition,
            stage_metadata=stage_metadata,
        )
        metadata = {
            "project_id": brief.get("project_id"),
            "brief_digest": _digest(brief),
            "event": event_name,
            "workflow_decision": decision,
            "next_action": consultation["resulting_action"],
            "stage": dict(stage_metadata or {"status": "AWAITING_DESIGN_REVIEW", "stage_status": "AWAITING_DESIGN_REVIEW"}),
            "bootstrap_evidence": bootstrap_evidence,
        }
        event = {
            "event": event_name,
            "kind": event_name,
            "decision": decision,
            "resulting_action": consultation["resulting_action"],
            "revision": review.get("revision"),
            "rationale": "bounded Bootstrap transition; no Stage execution started",
        }
        try:
            self.human_artifact_writer(
                self.workspace_root,
                metadata,
                event=event,
                stage_metadata=metadata["stage"],
                consultation_metadata=consultation,
                design_review_metadata=review,
            )
        except HumanArtifactError as exc:
            raise WorkflowRuntimeError("HUMAN_ARTIFACT_WRITE_FAILED", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - configured writer boundary
            raise WorkflowRuntimeError("HUMAN_ARTIFACT_WRITE_FAILED", "Bootstrap human artifacts could not be written") from exc

    def _run_orchestrator_prepare(
        self,
        *,
        document: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        orchestrator = self._resolve_orchestrator()
        if orchestrator is None:
            raise WorkflowRuntimeError("ORCHESTRATOR_NOT_CONFIGURED", "approved workflow requires a configured Bootstrap orchestrator")
        trigger = request.get("trigger", "INITIAL_ARCHITECTURE")
        try:
            raw_transition = orchestrator.prepare(brief=document, trigger=trigger)
        except WorkflowRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - orchestrator boundary
            raise _orchestrator_failure(exc) from exc
        transition = _validate_orchestrator_transition_metadata(
            raw_transition,
            brief=document,
            expected_transition="AWAITING_DESIGN_REVIEW",
        )
        bootstrap = transition.get("bootstrap_state")
        if bootstrap is not None:
            bootstrap = _validate_bootstrap_state_metadata(bootstrap, document)
        review = transition.get("design_review")
        if not isinstance(review, Mapping) or review.get("stage_created") is not False or review.get("stage_started") is not False:
            raise WorkflowRuntimeError("ORCHESTRATOR_TRANSITION_INVALID", "Design Review transition must not create or start a Stage")
        # The orchestrator is producer-only.  Runtime owns the canonical
        # project-local checkpoint and is the sole writer of its transition.
        if bootstrap is not None:
            _atomic_json_write(self.bootstrap_state_path, bootstrap)
        self._write_orchestrator_artifacts(
            brief=document,
            transition=transition,
            review=review,
            event_name="design_review_requested",
            decision="HUMAN_GATE",
        )
        saved = self._save_checkpoint(
            operation="orchestrator_prepare",
            brief_document=document,
            phase="AWAITING_DESIGN_REVIEW",
            design_review_state="AWAITING_DESIGN_REVIEW",
            design_review=review,
            bootstrap_state=bootstrap,
            orchestrator_transition=transition,
        )
        return self._result(
            "workflow_run",
            brief=document,
            checkpoint=saved,
            result={"status": "AWAITING_DESIGN_REVIEW", "transition": transition["transition"]},
            orchestrator_transition=transition,
            bootstrap_state=bootstrap,
            artifacts=copy.deepcopy(transition.get("artifacts", {})),
        )

    def _run_orchestrator_plan(
        self,
        *,
        document: Mapping[str, Any],
        checkpoint: Mapping[str, Any],
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        orchestrator = self._resolve_orchestrator()
        if orchestrator is None:
            raise WorkflowRuntimeError("ORCHESTRATOR_NOT_CONFIGURED", "accepted Design Review requires a configured Bootstrap orchestrator")
        review = checkpoint.get("design_review")
        if not isinstance(review, Mapping) or review.get("state") != "ACCEPTED":
            raise WorkflowRuntimeError("DESIGN_REVIEW_NOT_ACCEPTED", "an accepted Design Review is required before Stage planning")
        stage_options = request.get("stage_options")
        if stage_options is not None and not isinstance(stage_options, Mapping):
            raise WorkflowRuntimeError("RUN_REQUEST_INVALID", "stage_options must be an object")
        try:
            raw_transition = orchestrator.plan_accepted(
                review,
                brief=document,
                stage_options=stage_options,
            )
        except WorkflowRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - orchestrator boundary
            raise _orchestrator_failure(exc) from exc
        transition = _validate_orchestrator_transition_metadata(
            raw_transition,
            brief=document,
            expected_transition="STAGE_PLANNED",
        )
        bootstrap = checkpoint.get("bootstrap_state")
        if bootstrap is not None:
            bootstrap = _validate_bootstrap_state_metadata(bootstrap, document)
            transition = {**transition, "bootstrap_state": bootstrap}
        else:
            # A new accepted-design transition must be backed by a full
            # execution handoff, or by a bounded external descriptor whose
            # StageController claim is checked below.  The latter keeps
            # injected deterministic test/orchestration seams compatible
            # without allowing a fake Stage to enter the checkpoint.
            handoff = transition.get("execution_handoff")
            if isinstance(handoff, Mapping):
                handoff = _validate_execution_handoff_metadata(
                    handoff,
                    brief=document,
                    design_review=review,
                    workspace_root=self.workspace_root,
                )
                transition = {**transition, "execution_handoff": handoff}
            else:
                artifacts = transition.get("artifacts")
                if not isinstance(artifacts, Mapping) or not isinstance(artifacts.get("execution_handoff"), Mapping):
                    # Preserve the legacy injected Step 15 test seam.  It is
                    # not the new accepted-design handoff and remains
                    # subject to the old runner attestation contract.
                    verification = stage_plan = transition.get("stage_plan")
                    verification = stage_plan.get("verification") if isinstance(stage_plan, Mapping) else None
                    if not isinstance(verification, Mapping) or verification.get("passed") is not True:
                        raise WorkflowRuntimeError("ORCHESTRATOR_TRANSITION_INVALID", "accepted-design execution handoff is missing")
        stage_plan = transition.get("stage_plan")
        if not isinstance(stage_plan, Mapping) or stage_plan.get("stage_status") != "PLANNED" or stage_plan.get("stage_started") is not False:
            raise WorkflowRuntimeError("ORCHESTRATOR_TRANSITION_INVALID", "Step 15 transition did not prove a planned Stage")
        contract_digest = stage_plan.get("contract_digest")
        if not isinstance(contract_digest, str) or _HEX64.fullmatch(contract_digest) is None:
            raise WorkflowRuntimeError("ORCHESTRATOR_TRANSITION_INVALID", "planned Stage contract digest is invalid")
        if bootstrap is None and (
            isinstance(transition.get("execution_handoff"), Mapping)
            or isinstance(transition.get("artifacts", {}).get("execution_handoff") if isinstance(transition.get("artifacts"), Mapping) else None, Mapping)
            or isinstance(stage_plan.get("stage_controller"), Mapping)
        ):
            self._validate_stage_controller_claim(stage_plan)
        accepted = copy.deepcopy(dict(review))
        accepted.update({"step15_status": "PLANNED", "stage_created": True, "stage_started": False})
        checkpoint_receipt: Mapping[str, Any] | None = None
        receipt_path = self.workspace_root / HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH
        if receipt_path.is_file() and not receipt_path.is_symlink():
            try:
                persisted_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise WorkflowRuntimeError("HUMAN_ACCEPT_RECEIPT_INVALID", "Human ACCEPT receipt is unreadable during Stage planning") from exc
            checkpoint_receipt = _validate_human_accept_receipt_metadata(persisted_receipt, document, review)
            accepted["human_accept_receipt"] = copy.deepcopy(dict(checkpoint_receipt))
            human_decision = accepted.get("human_decision")
            if isinstance(human_decision, Mapping):
                accepted["human_decision"] = {
                    **dict(human_decision),
                    "decision": "ACCEPT",
                    "receipt_digest": checkpoint_receipt["receipt_digest"],
                }
        last_run = {
            "status": "PLANNED",
            "provider_id": "workflow-orchestrator",
            "outcome": "PLANNED",
            "result_digest": _digest(transition),
            "planned_evidence_validated": True,
            "stage_contract_digest": contract_digest,
        }
        self._write_orchestrator_artifacts(
            brief=document,
            transition=transition,
            review=accepted,
            event_name="design_review_accepted",
            decision="APPROVED",
            stage_metadata=stage_plan,
        )
        saved = self._save_checkpoint(
            operation="orchestrator_plan_accepted",
            brief_document=document,
            phase="PLANNED",
            last_run=last_run,
            design_review_state="ACCEPTED",
            design_review=accepted,
            bootstrap_state=bootstrap,
            human_accept_receipt=checkpoint_receipt,
            execution_handoff=transition.get("execution_handoff") if isinstance(transition.get("execution_handoff"), Mapping) else None,
            orchestrator_transition=transition,
        )
        return self._result(
            "workflow_run",
            brief=document,
            checkpoint=saved,
            result=copy.deepcopy(dict(stage_plan)),
            orchestrator_transition=transition,
            bootstrap_state=bootstrap,
            execution_handoff=transition.get("execution_handoff") if isinstance(transition.get("execution_handoff"), Mapping) else None,
            stage_plan=copy.deepcopy(dict(stage_plan)),
        )

    def run(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Delegate one run to the configured existing Core V1 runner."""

        checkpoint = self._load_checkpoint()
        document = self.intake.state
        if document is None:
            raise WorkflowRuntimeError("BRIEF_NOT_FOUND", "workflow requirements have not been started")
        if document.get("state") != BriefState.APPROVED.value:
            raise WorkflowRuntimeError("HUMAN_APPROVAL_REQUIRED", "workflow_run requires an approved project brief")
        if checkpoint.get("design_review_state") == "AWAITING_DESIGN_REVIEW":
            raise WorkflowRuntimeError(
                "DESIGN_REVIEW_REQUIRED",
                "workflow_run requires explicit acceptance of the pending Stage design review",
            )
        if not isinstance(request, Mapping):
            raise WorkflowRuntimeError("RUN_REQUEST_INVALID", "workflow_run request must be an object")
        _assert_safe(request, path="run_request")
        if self.runner is None and (self.orchestrator_factory is not None or self.orchestrator is not None):
            if checkpoint.get("phase") == "READY":
                return self._run_orchestrator_prepare(document=document, request=request)
            if checkpoint.get("phase") == "DESIGN_ACCEPTED":
                return self._run_orchestrator_plan(document=document, checkpoint=checkpoint, request=request)
        if self.runner is None:
            raise WorkflowRuntimeError("RUNNER_NOT_CONFIGURED", "workflow_run requires an injected existing Core V1 runner")
        target = getattr(self.runner, "run", self.runner)
        if not callable(target):
            raise WorkflowRuntimeError("RUNNER_INVALID", "configured workflow runner is not callable")
        try:
            raw = target(copy.deepcopy(dict(request)))
        except WorkflowRuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001 - runner boundary
            raise WorkflowRuntimeError("RUNNER_FAILED", "existing workflow runner failed") from exc
        if not isinstance(raw, Mapping):
            raise WorkflowRuntimeError("RUNNER_RESULT_INVALID", "existing workflow runner must return an object")
        bounded = _bounded_copy(dict(raw))
        _assert_safe(bounded, path="runner_result")
        outcome = _validated_runner_outcome(bounded)
        summary = {
            "status": bounded.get("status"),
            "decision": bounded.get("decision"),
            "provider_id": bounded.get("provider_id"),
            "outcome": outcome,
            "result_digest": _digest(bounded),
        }
        if outcome == "PLANNED":
            summary.update(
                {
                    "planned_evidence_validated": True,
                    "stage_contract_digest": _digest(bounded["stage_contract"]),
                }
            )
        phase = {
            "CONTINUE_WORKFLOW": "RUNNING",
            "PLANNED": "PLANNED",
            "COMPLETE": "COMPLETE",
            "BLOCKED": "BLOCKED",
        }[outcome]
        persisted_review = checkpoint.get("design_review") if isinstance(checkpoint.get("design_review"), Mapping) else None
        if outcome == "PLANNED":
            if not isinstance(persisted_review, Mapping):
                raise WorkflowRuntimeError("RUNNER_OUTCOME_INVALID", "PLANNED outcome requires an accepted design review")
            persisted_review = copy.deepcopy(dict(persisted_review))
            persisted_review.update(
                {
                    "step15_status": "PLANNED",
                    "stage_created": True,
                    "stage_started": False,
                }
            )
        checkpoint = self._save_checkpoint(
            operation="workflow_run",
            brief_document=document,
            phase=phase,
            last_run=summary,
            design_review_state=str(checkpoint.get("design_review_state", "NOT_REQUIRED")),
            design_review=persisted_review,
        )
        return self._result("workflow_run", brief=document, checkpoint=checkpoint, result=bounded)


__all__ = [
    "DEFAULT_CHECKPOINT_RELATIVE",
    "RUNNER_OUTCOMES",
    "WORKFLOW_ANSWER_MODES",
    "WORKFLOW_CHECKPOINT_SCHEMA_VERSION",
    "WORKFLOW_NEXT_ACTIONS",
    "WORKFLOW_PHASES",
    "WORKFLOW_RUNTIME_RESULT_SCHEMA_VERSION",
    "WorkflowRuntime",
    "WorkflowRuntimeError",
]


