#!/usr/bin/env python3
"""Small, fail-closed CLI for the Stage-Oriented Research workflow.

The CLI is deliberately a thin adapter around :class:`StageController`.  It
persists only the controller snapshot and bounded consultation metadata; it
does not run a worker loop, modify project files outside ``.research``, or
store prompts, raw GPT replies, cookies, tokens, or browser state.

Examples (run from a project repository)::

    python /path/to/stage_cli.py prepare --contract .research/stages/stage-001/contract.json
    python /path/to/stage_cli.py start stage-001
    python /path/to/stage_cli.py show stage-001
    python /path/to/stage_cli.py consult stage-001 --evidence .research/result.txt
    python /path/to/stage_cli.py fresh-review stage-001 --reason "closeout review"
    python /path/to/stage_cli.py show-artifacts stage-001
    python /path/to/stage_cli.py approve stage-001

``--repo`` defaults to the current directory, so a user normally only needs
to provide a stage id and (for a first consultation) bounded evidence paths.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from src.contracts import ContractValidationError, canonical_json, sha256_json  # noqa: E402
from src.stage_controller import (  # noqa: E402
    StageController,
    StageControllerError,
    StageState,
)
from src.bridge_adapter import (  # noqa: E402
    BridgeEnvelopeError,
    normalize_bridge_envelope,
    normalize_project_url,
)
from src.stage_integration import (  # noqa: E402
    StageIntegrationError,
    parse_dialogue_decision,
    subprocess_bridge_runner,
)
from src.project_intake import (  # noqa: E402
    ProjectIntakeError,
    ProjectRequirementsIntake,
    natural_language_entry,
)
from src.stage_planning import (  # noqa: E402
    StagePlanningError,
    plan_stage,
)
from src.human_artifacts import (  # noqa: E402
    HumanArtifactError,
    write_human_artifacts,
)
from src.research_prompt_policy import method_evidence_policy_text  # noqa: E402


CLI_SCHEMA_VERSION = "stage_cli.v1"
REVIEW_INDEX_SCHEMA_VERSION = "stage_cli_review_index.v1"
DEFAULT_STATE_RELATIVE = Path(".research") / "stage-state.json"
DEFAULT_REVIEW_INDEX_RELATIVE = Path(".research") / "reviews" / "index.json"
DEFAULT_TIMEOUT_MS = 300_000
MAX_TIMEOUT_MS = 600_000

# These names are intentionally conservative.  They are used before any
# payload can be persisted or sent to the bridge; unknown/ambiguous material
# is rejected rather than guessed safe.
SENSITIVE_KEY_NAMES = {
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "client_secret",
    "cookie",
    "cookies",
    "dom",
    "local_storage",
    "password",
    "passwd",
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
SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)


class CliError(RuntimeError):
    """A bounded, user-facing CLI failure with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _assert_no_secrets(value: Any, *, path: str = "$", depth: int = 0) -> None:
    """Reject high-confidence sensitive keys/values recursively.

    The bridge has its own content checks.  This second local check protects
    CLI state and error receipts, where silently dropping a suspicious field
    would make the resulting audit misleading.
    """

    if depth > 12:
        raise CliError("SECRET_SCAN_DEPTH_EXCEEDED", "input nesting is too deep to validate safely")
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).strip().lower().replace("-", "_")
            if key_text in SENSITIVE_KEY_NAMES:
                raise CliError("SECRET_REJECTED", f"sensitive field is not allowed at {path}")
            _assert_no_secrets(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_no_secrets(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str):
        for pattern in SECRET_VALUE_PATTERNS:
            if pattern.search(value):
                raise CliError("SECRET_REJECTED", f"high-confidence secret pattern at {path}")


def _resolve_directory(value: str | os.PathLike[str], field: str) -> Path:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CliError("PATH_NOT_FOUND", f"{field} does not exist") from exc
    if not resolved.is_dir():
        raise CliError("PATH_NOT_DIRECTORY", f"{field} must be a directory")
    return resolved


def _resolve_under(root: Path, value: str | os.PathLike[str], field: str, *, must_exist: bool) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise CliError("PATH_NOT_FOUND", f"{field} does not exist") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CliError("PATH_OUTSIDE_REPOSITORY", f"{field} must remain inside the repository") from exc
    return resolved


def _relative(root: Path, path: Path, field: str = "path") -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as exc:
        raise CliError("PATH_OUTSIDE_REPOSITORY", f"{field} must remain inside the repository") from exc


def _path_is_within(root: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _path_prefixes(relative_path: str) -> set[str]:
    parts = [part for part in relative_path.replace("\\", "/").split("/") if part and part != "."]
    return {"/".join(parts[:index]) for index in range(1, len(parts) + 1)}


def _is_protected(relative_path: str, protected_paths: Sequence[str]) -> bool:
    prefixes = _path_prefixes(relative_path)
    normalized = {str(item).replace("\\", "/").strip("/") for item in protected_paths}
    return any(item in prefixes or relative_path == item for item in normalized if item)


def _load_json(path: Path, field: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError("JSON_INVALID", f"could not read {field}") from exc
    if not isinstance(payload, dict):
        raise CliError("JSON_OBJECT_REQUIRED", f"{field} must contain an object")
    _assert_no_secrets(payload, path=field)
    return payload


def _approved_project_url(repo: Path) -> str | None:
    """Resolve the already-approved project binding for Stage consultations.

    Bootstrap stores the fixed project route in the canonical brief.  The
    Stage CLI must forward that route to the bridge; otherwise a real Stage
    consultation silently starts at the generic ChatGPT landing page and
    cannot prove project scope.  Only the narrow URL value is read and
    validated; the brief is never copied into the outgoing prompt here.
    """

    brief_path = repo / ".research" / "PROJECT_BRIEF.json"
    if not brief_path.is_file():
        return None
    brief = _load_json(brief_path, "project brief")
    candidates: list[Any] = []
    candidates.append(brief.get("chatgpt_project_url"))
    top_brief = brief.get("brief")
    if isinstance(top_brief, Mapping):
        candidates.append(top_brief.get("chatgpt_project_url"))
        binding = top_brief.get("chatgpt_project_binding")
        if isinstance(binding, Mapping):
            candidates.append(binding.get("url"))
    binding = brief.get("chatgpt_project_binding")
    if isinstance(binding, Mapping):
        candidates.append(binding.get("url"))
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            return normalize_project_url(candidate)
        except BridgeEnvelopeError as exc:
            raise CliError("PROJECT_BINDING_INVALID", "approved ChatGPT Project binding is invalid") from exc
    return None


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _assert_no_secrets(payload, path=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _capture_state_file(path: Path) -> tuple[bool, bytes | None]:
    """Capture the exact persisted controller state for a bounded rollback."""

    if not path.exists():
        return False, None
    if not path.is_file():
        raise CliError("STATE_PATH_INVALID", "state path must be a file")
    try:
        return True, path.read_bytes()
    except OSError as exc:
        raise CliError("STATE_INVALID", "persisted Stage state could not be read") from exc


def _restore_state_file(path: Path, snapshot: tuple[bool, bytes | None]) -> None:
    """Restore a prior state file without exposing rollback details to users."""

    existed, content = snapshot
    if not existed:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    if content is None:
        raise OSError("state rollback snapshot is incomplete")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.rollback-{os.getpid()}")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _record_artifact_write_failure(
    repo: Path,
    *,
    operation: str,
    stage_id: Any,
    event: Any,
    error: Exception,
) -> None:
    """Record bounded artifact-writer failures outside controller state.

    The lifecycle transaction is rolled back before this audit entry is
    written.  Only stable identifiers and the writer's error code are kept;
    exception text can contain paths or transport payloads and is therefore
    deliberately excluded.
    """

    try:
        index = _load_review_index(repo)
        entries = index.get("artifact_errors")
        if not isinstance(entries, list):
            entries = []
        event_name = event.get("event") if isinstance(event, Mapping) else event
        if not isinstance(event_name, str) or not event_name.strip():
            event_name = "unknown"
        failure_code = getattr(error, "code", None)
        if not isinstance(failure_code, str) or not failure_code.strip():
            failure_code = "HUMAN_ARTIFACT_WRITE_FAILED"
        entries.append(
            {
                "operation": str(operation)[:100],
                "stage_id": str(stage_id)[:256] if stage_id is not None else None,
                "event": event_name[:100],
                "status": "failed",
                "failure_code": failure_code[:100],
                "error_type": type(error).__name__[:100],
                "created_at": _now(),
            }
        )
        index["artifact_errors"] = entries[-64:]
        _write_json(_review_index_path(repo), index)
    except Exception:
        # Artifact failure reporting must never mask the lifecycle failure or
        # cause a second state mutation when the audit directory is unwritable.
        return


def _write_lifecycle_artifacts(
    repo: Path,
    *,
    operation: str,
    event: Any,
    stage: Mapping[str, Any],
    consultation: Mapping[str, Any] | None = None,
    controller: StageController | None = None,
    state_snapshot: Mapping[str, Any] | None = None,
    state_path: Path | None = None,
    state_file_snapshot: tuple[bool, bytes | None] | None = None,
    rollback_state: bool = True,
) -> dict[str, Path]:
    """Write human artifacts as part of a controller transaction.

    Controller mutations happen before this call so the writer receives the
    committed stage view.  For local lifecycle transitions a failed write
    restores both the in-memory controller and its persisted state.  Callers
    that already crossed an external consultation boundary can disable that
    rollback to keep the consumed request fail-closed.
    """

    try:
        artifact_stage: Mapping[str, Any] = stage
        # StageController intentionally stores consultation requests, not
        # response metadata.  The review index is the bounded source of truth
        # for completed consultations, so pass its history on every lifecycle
        # render.  This keeps the decision log append-like across a later
        # planning/start/approval event instead of resetting it to the latest
        # controller snapshot.
        review_index = _load_review_index(repo)
        history = review_index.get("consultations")
        if isinstance(history, list):
            artifact_stage = copy.deepcopy(dict(stage))
            artifact_stage["consultations"] = copy.deepcopy(history[-64:])
        artifact_event = event
        if isinstance(event, Mapping):
            artifact_event = copy.deepcopy(dict(event))
            # Controller events do not currently carry wall-clock time.  The
            # artifact timestamp records when this bounded local rendering
            # observed the route decision, without changing controller state.
            artifact_event.setdefault("created_at", _now())
        return write_human_artifacts(
            repo,
            event=artifact_event,
            stage_metadata=artifact_stage,
            consultation_metadata=consultation,
        )
    except Exception as exc:  # noqa: BLE001 - writer boundary must fail closed
        if rollback_state and controller is not None and state_snapshot is not None:
            try:
                controller._load_snapshot(copy.deepcopy(dict(state_snapshot)))
            except Exception:
                pass
        if rollback_state and state_path is not None and state_file_snapshot is not None:
            try:
                _restore_state_file(state_path, state_file_snapshot)
            except Exception:
                pass
        _record_artifact_write_failure(
            repo,
            operation=operation,
            stage_id=stage.get("stage_id") if isinstance(stage, Mapping) else None,
            event=event,
            error=exc,
        )
        # Preserve the stable writer code for callers while avoiding raw
        # exception text in CLI output.
        code = exc.code if isinstance(exc, HumanArtifactError) else "HUMAN_ARTIFACT_WRITE_FAILED"
        raise CliError(code, "human lifecycle artifacts could not be updated") from exc


def _state_path(repo: Path, supplied: str | None) -> Path:
    value = supplied or str(DEFAULT_STATE_RELATIVE)
    resolved = _resolve_under(repo, value, "state path", must_exist=False)
    if resolved.exists() and resolved.is_dir():
        raise CliError("STATE_PATH_INVALID", "state path must be a file")
    return resolved


def _load_controller(repo: Path, supplied_state: str | None) -> tuple[StageController, Path]:
    state_path = _state_path(repo, supplied_state)
    if not state_path.is_file():
        raise CliError("STATE_NOT_FOUND", "no prepared Stage state exists; run prepare first")
    try:
        # StageController intentionally strips legacy sensitive keys when
        # making a snapshot.  The product surface is stricter: a persisted
        # state containing such a key is rejected so users cannot mistake a
        # silently sanitized state for a complete audit record.
        raw_state = json.loads(state_path.read_text(encoding="utf-8"))
        _assert_no_secrets(raw_state, path="stage state")
        controller = StageController.from_state(state_path)
    except CliError:
        raise
    except (StageControllerError, ContractValidationError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CliError("STATE_INVALID", "persisted Stage state failed validation") from exc
    return controller, state_path


def _stage_id_from(args: argparse.Namespace) -> str | None:
    value = getattr(args, "stage_id", None)
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        raise CliError("STAGE_ID_INVALID", "stage id must be non-empty")
    return value


def _stage_view(stage: Mapping[str, Any]) -> dict[str, Any]:
    """Return a useful, bounded view rather than dumping internal state."""

    contract = stage.get("contract") if isinstance(stage.get("contract"), Mapping) else {}
    latest = stage.get("latest_result")
    view: dict[str, Any] = {
        "stage_id": stage.get("stage_id") or contract.get("stage_id"),
        "status": stage.get("status"),
        "stage_name": contract.get("stage_name"),
        "project_id": contract.get("project_id"),
        "project_goal": contract.get("project_goal"),
        "stage_goal": contract.get("stage_goal"),
        "user_visible_goal": contract.get("user_visible_goal"),
        "allowed_paths": contract.get("allowed_paths", []),
        "protected_paths": contract.get("protected_paths", []),
        "required_checks": contract.get("required_checks", []),
        "review_artifact_requirements": contract.get("review_artifact_requirements", []),
        "baseline_digest": stage.get("baseline_digest"),
        "iteration_index": stage.get("iteration_index", 0),
        "open_iteration_index": stage.get("open_iteration_index"),
        "attempt_count": stage.get("attempt_count", 0),
        "retry_count": stage.get("retry_count", 0),
        "retry_budget": stage.get("retry_budget"),
        "execution_evidence_complete": bool(stage.get("execution_evidence_complete", False)),
        "latest_attempt": (
            copy.deepcopy(stage.get("execution_attempts", [])[-1])
            if isinstance(stage.get("execution_attempts"), list) and stage.get("execution_attempts")
            else None
        ),
        "stage_result_binding": copy.deepcopy(stage.get("stage_result_binding")),
        "latest_result": copy.deepcopy(latest),
        "lanes": copy.deepcopy(stage.get("lanes", ["PRIMARY"])),
        "consultation_count": len(stage.get("consultation_requests", [])),
        "review_count": len(stage.get("reviews", [])),
        "pending_human_gate": copy.deepcopy(stage.get("pending_human_gate")),
        "stop_reason": copy.deepcopy(stage.get("stop_reason")),
    }
    _assert_no_secrets(view, path="stage_view")
    return view


def _emit(payload: Mapping[str, Any]) -> None:
    _assert_no_secrets(payload, path="output")
    # Keep CLI output parseable from Windows subprocesses regardless of the
    # active console code page; JSON consumers recover the original Unicode.
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2))


def _actor(args: argparse.Namespace) -> tuple[str, str]:
    actor = str(getattr(args, "actor", None) or "local-human").strip()
    rationale = str(getattr(args, "rationale", None) or "").strip()
    if not actor:
        raise CliError("ACTOR_INVALID", "actor must be non-empty")
    _assert_no_secrets({"actor": actor, "rationale": rationale}, path="human_action")
    return actor, rationale


def _prepare(args: argparse.Namespace, repo: Path, state_arg: str | None) -> dict[str, Any]:
    contract_path = _resolve_under(repo, args.contract, "contract path", must_exist=True)
    if contract_path.suffix.lower() != ".json":
        raise CliError("CONTRACT_PATH_INVALID", "contract path must be a JSON file")
    contract = _load_json(contract_path, "stage contract")
    requested = _stage_id_from(args)
    if requested is not None and requested != contract.get("stage_id"):
        raise CliError("STAGE_ID_MISMATCH", "stage id does not match the contract")
    repository_root = contract.get("repository_root")
    if not isinstance(repository_root, str) or not repository_root.strip():
        raise CliError("CONTRACT_INVALID", "contract repository_root is required")
    root_candidate = Path(repository_root).expanduser()
    if not root_candidate.is_absolute():
        root_candidate = repo / root_candidate
    try:
        if root_candidate.resolve(strict=True) != repo:
            raise CliError("REPOSITORY_ROOT_MISMATCH", "contract repository_root does not match --repo")
    except CliError:
        raise
    except (OSError, RuntimeError) as exc:
        raise CliError("REPOSITORY_ROOT_INVALID", "contract repository_root could not be resolved") from exc

    state_path = _state_path(repo, state_arg)
    try:
        controller = StageController(state_path=state_path)
        before_controller = controller.snapshot()
        before_state_file = _capture_state_file(state_path)
        result = controller.prepare_stage(contract)
    except (StageControllerError, ContractValidationError) as exc:
        raise CliError("PREPARE_REJECTED", "contract failed Stage preparation validation") from exc
    _write_lifecycle_artifacts(
        repo,
        operation="prepare",
        event=result.get("event") or "prepare_stage",
        stage=result["stage"],
        controller=controller,
        state_snapshot=before_controller,
        state_path=state_path,
        state_file_snapshot=before_state_file,
    )
    return {
        "schema_version": CLI_SCHEMA_VERSION,
        "operation": "prepare",
        "state_path": _relative(repo, state_path, "state path"),
        "contract_path": _relative(repo, contract_path, "contract path"),
        "stage": _stage_view(result["stage"]),
    }


def _plan_stage(args: argparse.Namespace, repo: Path, state_arg: str | None) -> dict[str, Any]:
    """Run Step 15 planning and register exactly one PLANNED Stage."""

    # Validate the repository and the state boundary before invoking the
    # planner.  Step 15 always owns the canonical state file; rejecting a
    # custom path first guarantees an invalid CLI request cannot create any
    # Stage contract, state snapshot, or planning envelope as a side effect.
    repo = _resolve_directory(repo, "repository")
    if state_arg is not None:
        normalized_state = str(state_arg).replace("\\", "/").strip()
        if normalized_state != DEFAULT_STATE_RELATIVE.as_posix():
            raise CliError("STATE_PATH_INVALID", "Step 15 planning requires .research/stage-state.json")
        _state_path(repo, state_arg)
    state_path = repo / DEFAULT_STATE_RELATIVE
    before_state_file = _capture_state_file(state_path)

    raw_stage_id = getattr(args, "stage_id", None)
    option_stage_id = getattr(args, "stage_id_option", None)
    if raw_stage_id and option_stage_id and raw_stage_id != option_stage_id:
        raise CliError("STAGE_ID_MISMATCH", "positional stage id and --stage-id disagree")
    stage_id = option_stage_id or raw_stage_id
    try:
        result = plan_stage(
            repo,
            stage_id=stage_id,
            stage_name=getattr(args, "stage_name", None),
            allowed_paths=getattr(args, "allowed_path", None),
            required_checks=getattr(args, "required_check", None),
            review_artifact_requirements=getattr(args, "review_artifact", None),
            max_iterations=getattr(args, "max_iterations", 1),
            retry_budget=getattr(args, "retry_budget", None),
            bootstrap_state_path=getattr(args, "bootstrap_state", None),
        )
    except StagePlanningError as exc:
        raise CliError(exc.code, str(exc)) from exc
    stage = result.get("stage") if isinstance(result, Mapping) else None
    if not isinstance(stage, Mapping):
        # The Stage planning schema always carries a bounded stage snapshot;
        # fail closed if a future planner implementation omits it.
        raise CliError("STAGE_PLANNING_INVALID", "Stage planning did not return a bounded Stage snapshot")
    _write_lifecycle_artifacts(
        repo,
        operation="plan-stage",
        event={"event": "stage_planning", "stage_id": result.get("stage_id")},
        stage=stage,
        state_path=state_path,
        state_file_snapshot=before_state_file,
    )
    return result


def _lifecycle(args: argparse.Namespace, repo: Path, state_arg: str | None) -> dict[str, Any]:
    controller, state_path = _load_controller(repo, state_arg)
    stage_id = _stage_id_from(args)
    actor, rationale = _actor(args)
    operation = args.operation
    before_controller = controller.snapshot()
    before_state_file = _capture_state_file(state_path)
    try:
        if operation == "start":
            result = controller.start_stage(stage_id, actor=actor, rationale=rationale)
        elif operation in {"stop", "pause"}:
            result = controller.stop_stage(stage_id, actor=actor, rationale=rationale)
        elif operation == "approve":
            result = controller.approve_stage(stage_id, actor=actor, rationale=rationale)
        elif operation == "reject":
            result = controller.reject_stage(stage_id, actor=actor, rationale=rationale)
        else:  # pragma: no cover - argparse constrains this branch
            raise CliError("OPERATION_INVALID", f"unsupported lifecycle operation: {operation}")
    except (StageControllerError, ContractValidationError) as exc:
        raise CliError(f"{operation.upper()}_REJECTED", f"Stage {operation} was rejected by the state contract") from exc
    _write_lifecycle_artifacts(
        repo,
        operation=operation,
        event=result.get("event") or f"{operation}_stage",
        stage=result["stage"],
        controller=controller,
        state_snapshot=before_controller,
        state_path=state_path,
        state_file_snapshot=before_state_file,
    )
    return {
        "schema_version": CLI_SCHEMA_VERSION,
        "operation": operation,
        "state_path": _relative(repo, state_path, "state path"),
        "stage": _stage_view(result["stage"]),
        "event": copy.deepcopy(result.get("event")),
    }


def _show(args: argparse.Namespace, repo: Path, state_arg: str | None) -> dict[str, Any]:
    controller, state_path = _load_controller(repo, state_arg)
    stage_id = _stage_id_from(args)
    try:
        if args.all:
            raw = controller.state
            stages = raw.get("stages", {})
            result: dict[str, Any] = {
                "schema_version": CLI_SCHEMA_VERSION,
                "operation": "show",
                "state_path": _relative(repo, state_path, "state path"),
                "current_stage_id": raw.get("current_stage_id"),
                "active_stage_id": raw.get("active_stage_id"),
                "stages": {key: _stage_view(value) for key, value in stages.items()},
            }
        else:
            result = {
                "schema_version": CLI_SCHEMA_VERSION,
                "operation": "show",
                "state_path": _relative(repo, state_path, "state path"),
                "stage": _stage_view(controller.show_stage(stage_id)),
            }
    except (StageControllerError, ContractValidationError) as exc:
        raise CliError("SHOW_REJECTED", "could not select the requested Stage") from exc
    return result


def _review_index_path(repo: Path) -> Path:
    path = repo / DEFAULT_REVIEW_INDEX_RELATIVE
    if not _path_is_within(repo, path):
        raise CliError("REVIEW_INDEX_INVALID", "review index escaped the repository")
    return path


def _load_review_index(repo: Path) -> dict[str, Any]:
    path = _review_index_path(repo)
    if not path.exists():
        return {"schema_version": REVIEW_INDEX_SCHEMA_VERSION, "consultations": []}
    payload = _load_json(path, "review index")
    if payload.get("schema_version") != REVIEW_INDEX_SCHEMA_VERSION or not isinstance(payload.get("consultations"), list):
        raise CliError("REVIEW_INDEX_INVALID", "review index schema is invalid")
    return payload


def _receipt_reference(repo: Path, value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return Path(value).name or None
    if _path_is_within(repo, resolved):
        return _relative(repo, resolved, "receipt path")
    return resolved.name or None


def _record_review(repo: Path, entry: Mapping[str, Any]) -> None:
    index = _load_review_index(repo)
    index["consultations"].append(copy.deepcopy(dict(entry)))
    # Keep bounded audit metadata; raw responses remain available only through
    # the bridge's own sanitized summary/receipt behavior.
    index["consultations"] = index["consultations"][-64:]
    _write_json(_review_index_path(repo), index)


def _safe_evidence_paths(
    repo: Path,
    raw_paths: Sequence[str] | None,
    stage: Mapping[str, Any],
) -> list[dict[str, str]]:
    contract = stage.get("contract", {})
    protected = contract.get("protected_paths", []) if isinstance(contract, Mapping) else []
    values = list(raw_paths or [])
    descriptors: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            raise CliError("EVIDENCE_PATH_INVALID", "evidence paths must be non-empty strings")
        path = _resolve_under(repo, raw, "evidence path", must_exist=True)
        if not path.is_file():
            raise CliError("EVIDENCE_NOT_FILE", "evidence path must be a regular file")
        relative = _relative(repo, path, "evidence path")
        if _is_protected(relative, protected):
            raise CliError("PROTECTED_PATH_REJECTED", "evidence path is inside protected scope")
        if relative in seen:
            continue
        seen.add(relative)
        descriptors.append({"sourcePath": relative})
    return descriptors


def _latest_normal_lineage(repo: Path, stage_id: str) -> dict[str, str] | None:
    """Return the bridge receipt id and conversation id for NORMAL reuse.

    These identifiers intentionally have different jobs at the bridge
    boundary.  ``continue_from`` is a *consultation id* because the bridge
    resolves it to ``.consultations/<id>/receipt.json`` and derives the
    conversation identity from that receipt.  The conversation UUID is kept
    in the local Stage request as an identity/audit field only.
    """

    index = _load_review_index(repo)
    for entry in reversed(index["consultations"]):
        if (
            isinstance(entry, Mapping)
            and entry.get("stage_id") == stage_id
            and entry.get("mode") == "NORMAL"
            and entry.get("status") == "complete"
        ):
            consultation_id = entry.get("consultation_id")
            conversation_id = entry.get("conversation_id")
            if not isinstance(consultation_id, str) or not consultation_id.strip():
                raise CliError(
                    "PREVIOUS_CONSULTATION_MISSING",
                    "the previous NORMAL consultation has no bridge receipt id",
                )
            if not isinstance(conversation_id, str) or not conversation_id.strip():
                raise CliError(
                    "PREVIOUS_CONVERSATION_MISSING",
                    "the previous NORMAL consultation has no validated conversation id",
                )
            if entry.get("request_count") != 1:
                raise CliError(
                    "PREVIOUS_CONSULTATION_INVALID",
                    "the previous NORMAL consultation did not consume exactly one request",
                )
            return {
                "consultation_id": consultation_id.strip(),
                "conversation_id": conversation_id.strip(),
            }
    return None


def _latest_normal_manifest(repo: Path, stage_id: str) -> dict[str, Any] | None:
    """Load the last CLI-created NORMAL manifest for delta reuse.

    The manifest is bounded metadata only.  If the index claims a completed
    consultation but its staged manifest disappeared, fail closed instead of
    silently uploading the full context again.
    """

    index = _load_review_index(repo)
    for entry in reversed(index["consultations"]):
        if (
            not isinstance(entry, Mapping)
            or entry.get("stage_id") != stage_id
            or entry.get("mode") != "NORMAL"
            or entry.get("status") != "complete"
        ):
            continue
        packet_id = entry.get("context_pack_id")
        if not isinstance(packet_id, str) or not packet_id:
            raise CliError("PREVIOUS_CONTEXT_MISSING", "the previous NORMAL consultation has no context-pack id")
        manifest_path = repo / ".consultations" / "staging" / packet_id / "context_manifest.json"
        if not manifest_path.is_file():
            raise CliError("PREVIOUS_CONTEXT_MISSING", "the previous NORMAL context manifest is unavailable")
        manifest = _load_json(manifest_path, "previous context manifest")
        if manifest.get("packet_id") != packet_id:
            raise CliError("PREVIOUS_CONTEXT_INVALID", "the previous context manifest id is inconsistent")
        return manifest
    return None


def _pack_spec(
    stage: Mapping[str, Any],
    *,
    mode: str,
    evidence: Sequence[dict[str, str]],
    blocker: str,
) -> dict[str, Any]:
    contract = stage.get("contract", {})
    latest_result = stage.get("latest_result")
    if not isinstance(latest_result, Mapping):
        latest_result = {}
    else:
        latest_result = copy.deepcopy(dict(latest_result))
        repair = stage.get("evidence_reference_repair")
        if isinstance(repair, Mapping) and isinstance(repair.get("repaired_evidence_refs"), list):
            latest_result["evidence_refs"] = list(repair["repaired_evidence_refs"])
    acceptance = contract.get("acceptance_description", "")
    required_checks = contract.get("required_checks", [])
    hard_constraints = [acceptance] if isinstance(acceptance, str) and acceptance else []
    hard_constraints.extend(str(item) for item in required_checks if isinstance(item, str) and item.strip())
    facts = [
        f"Project identity: {contract.get('project_id')}",
        f"Stage identity: {contract.get('stage_id')}",
        f"Stage status: {stage.get('status')}",
        f"Iteration index: {stage.get('iteration_index', 0)}",
        f"Frozen baseline digest: {stage.get('baseline_digest')}",
    ]
    hypothesis = contract.get("hypothesis") or contract.get("experiment") or contract.get("stage_goal")
    pack: dict[str, Any] = {
        "mode": mode.lower(),
        "projectGoal": contract.get("project_goal", ""),
        "currentStageGoal": contract.get("stage_goal", ""),
        "userVisibleGoal": contract.get("user_visible_goal", ""),
        "establishedFacts": facts,
        "currentMethod": str(hypothesis),
        "currentBlocker": blocker,
        "protectedForbiddenScope": list(contract.get("protected_paths", [])),
        "hardConstraints": hard_constraints,
        "latestResult": copy.deepcopy(dict(latest_result)),
        "evidence": list(evidence),
    }
    _assert_no_secrets(pack, path="context_pack")
    return pack


def build_stage_consultation_policy(mode: str) -> str:
    """Return the canonical policy used by NORMAL/FRESH Stage consultations."""

    normalized = str(mode).strip().upper()
    if normalized not in {"NORMAL", "FRESH"}:
        raise ValueError("mode must be NORMAL or FRESH")
    return method_evidence_policy_text(context=f"{normalized} Stage consultation")


def _consult(
    args: argparse.Namespace,
    repo: Path,
    state_arg: str | None,
    *,
    mode: str,
    runner: Callable[..., Mapping[str, Any]] = subprocess_bridge_runner,
) -> dict[str, Any]:
    controller, state_path = _load_controller(repo, state_arg)
    stage_id = _stage_id_from(args)
    try:
        stage = controller.show_stage(stage_id)
    except (StageControllerError, ContractValidationError) as exc:
        raise CliError("STAGE_SELECTION_REJECTED", "could not select the requested Stage") from exc
    if stage.get("status") != StageState.ACTIVE.value:
        raise CliError("STAGE_NOT_ACTIVE", "consultation is allowed only while the Stage is ACTIVE")
    selected_stage_id = str(stage["contract"]["stage_id"])
    transport = getattr(args, "transport", "project")
    approved_project_url = _approved_project_url(repo)
    if transport == "homepage_fallback":
        if approved_project_url is not None:
            raise CliError("PROJECT_SCOPE_REQUIRED", "an accepted project binding cannot use homepage fallback transport")
        contract = stage.get("contract", {})
        metadata = contract.get("metadata", {})
        if any(source.get(key) is True for source in (contract, metadata)
               for key in ("require_project_scope", "project_scope_required", "project_scope_verified")):
            raise CliError("PROJECT_SCOPE_REQUIRED", "canonical Contract requires Project UI scope")
    question = str(getattr(args, "question", None) or "Review the current bounded Stage evidence and recommend the next scoped action.").strip()
    blocker = str(getattr(args, "blocker", None) or "No additional blocker supplied.").strip()
    reason = str(getattr(args, "reason", None) or ("fresh closeout review" if mode == "FRESH" else "bounded Stage review")).strip()
    if not question or not blocker or not reason:
        raise CliError("CONSULTATION_INPUT_INVALID", "question, blocker, and reason must be non-empty")
    _assert_no_secrets({"question": question, "blocker": blocker, "reason": reason}, path="consultation_input")
    evidence = _safe_evidence_paths(repo, getattr(args, "evidence", None), stage)
    pack = _pack_spec(stage, mode=mode, evidence=evidence, blocker=blocker)
    # Keep the canonical evidence-layering policy in the bounded consultation
    # context.  It is transient prompt material and is never written as a raw
    # prompt/response by the CLI.
    pack["methodEvidencePolicy"] = build_stage_consultation_policy(mode)
    identity_path = repo / ".research" / "workflow-identity.json"
    if identity_path.is_file():
        identity = _load_json(identity_path, "workflow identity binding")
        if identity.get("project_id") != stage.get("contract", {}).get("project_id"):
            raise CliError("WORKFLOW_IDENTITY_MISMATCH", "review identity differs from Stage Contract")
        pack["establishedFacts"].append(f"Workflow identity: {identity.get('workflow_id')}")
    pack["establishedFacts"].append(f"Browser transport: {transport}; transport is not lifecycle authority")
    _assert_no_secrets(pack, path="context_pack")
    if mode == "NORMAL":
        previous_manifest = _latest_normal_manifest(repo, selected_stage_id)
        if previous_manifest is not None:
            pack["previousPack"] = {"manifest": previous_manifest}
            _assert_no_secrets(pack, path="context_pack")
    evidence_digest = _json_digest({"mode": mode, "pack": pack, "reason": reason})
    continue_from: str | None = None
    previous_conversation_id: str | None = None
    if mode == "NORMAL":
        lineage = _latest_normal_lineage(repo, selected_stage_id)
        if lineage is not None:
            # The Node bridge's continue_from is the parent *consultation*
            # id, not the UUID of the ChatGPT conversation itself.
            continue_from = lineage["consultation_id"]
            previous_conversation_id = lineage["conversation_id"]
    if getattr(args, "dry_run", False):
        return {
            "schema_version": CLI_SCHEMA_VERSION,
            "operation": "fresh-review" if mode == "FRESH" else "consult",
            "status": "dry_run",
            "mode": mode,
            "state_path": _relative(repo, state_path, "state path"),
            "stage_id": selected_stage_id,
            "conversation_continuity": continue_from is not None,
            "evidence_count": len(evidence),
            "evidence_digest": evidence_digest,
        }

    if runner is subprocess_bridge_runner and not getattr(args, "real_run", False):
        raise CliError("REAL_RUN_REQUIRED", "the headed bridge requires explicit --real-run")

    # The state controller records the consultation request before crossing
    # the external bridge boundary.  If the bridge fails, the digest remains
    # consumed and the semantic Stage request cannot silently be retried. The
    # bridge itself may perform its separate bounded pre-prompt recovery while
    # request_count remains zero.
    before_controller = controller.snapshot()
    before_state_file = _capture_state_file(state_path)
    try:
        request = controller.request_consultation(
            selected_stage_id,
            mode=mode,
            evidence_digest=evidence_digest,
            reason=reason,
            # Keep the local Stage request's identity field as the actual
            # conversation UUID.  The bridge receives the parent receipt id
            # separately below via continue_from.
            conversation_id=previous_conversation_id,
        )
    except (StageControllerError, ContractValidationError) as exc:
        raise CliError("CONSULTATION_REJECTED", "the Stage consultation gate rejected this evidence") from exc

    started = time.monotonic()
    try:
        result = runner(
            question,
            mode="fresh" if mode == "FRESH" or continue_from is None else "continue",
            continue_from=continue_from,
            context_pack=pack,
            root_dir=str(repo),
            profile_dir=getattr(args, "profile_dir", None),
            timeout_ms=int(getattr(args, "timeout_ms", DEFAULT_TIMEOUT_MS)),
            project_url=None if transport == "homepage_fallback" else approved_project_url,
            **({"transport": transport} if transport == "homepage_fallback" else {}),
            **({"bridge_root": getattr(args, "bridge_root")} if getattr(args, "bridge_root", None) else {}),
        )
    except StageIntegrationError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _record_review(
            repo,
            {
                "stage_id": selected_stage_id,
                "mode": mode,
                "status": "failed",
                "failure_code": exc.code,
                "request_id": request["request"]["request_id"],
                "evidence_digest": evidence_digest,
                "elapsed_ms": elapsed_ms,
                "created_at": _now(),
            },
        )
        raise CliError(exc.code, "headed bridge consultation failed; no semantic retry was attempted") from exc
    except Exception as exc:  # noqa: BLE001 - external boundary must fail closed
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _record_review(
            repo,
            {
                "stage_id": selected_stage_id,
                "mode": mode,
                "status": "failed",
                "failure_code": "BRIDGE_EXTERNAL_FAILURE",
                "request_id": request["request"]["request_id"],
                "evidence_digest": evidence_digest,
                "elapsed_ms": elapsed_ms,
                "created_at": _now(),
            },
        )
        raise CliError("BRIDGE_EXTERNAL_FAILURE", "headed bridge consultation failed; no semantic retry was attempted") from exc

    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not isinstance(result, Mapping):
        raise CliError("BRIDGE_RESULT_INVALID", "bridge returned no bounded result object")
    try:
        # Normalize both the existing snake_case CLI runner result and the
        # bridge's native camelCase response envelope at one boundary.  A
        # malformed result is recorded as consumed and is never retried.
        result = normalize_bridge_envelope(result)
    except BridgeEnvelopeError as exc:
        _record_review(
            repo,
            {
                "stage_id": selected_stage_id,
                "mode": mode,
                "status": "failed",
                "failure_code": exc.code,
                "request_id": request["request"]["request_id"],
                "evidence_digest": evidence_digest,
                "elapsed_ms": elapsed_ms,
                "created_at": _now(),
            },
        )
        raise CliError(exc.code, "bridge result failed request/response validation") from exc
    request_count = result["request_count"]
    response_text = result["response_text"]
    consultation_id = result["consultation_id"]
    _assert_no_secrets({"consultation_id": consultation_id, "response_text": response_text}, path="bridge_result")
    receipt = result.get("receipt") if isinstance(result.get("receipt"), Mapping) else {}
    conversation_id = result.get("conversation_id") if isinstance(result.get("conversation_id"), str) else None
    if conversation_id is None:
        conversation_id = receipt.get("conversation_id") if isinstance(receipt.get("conversation_id"), str) else None
    context_receipt = receipt.get("context_pack") if isinstance(receipt.get("context_pack"), Mapping) else {}
    packet_candidate = receipt.get("context_pack_id")
    if not isinstance(packet_candidate, str):
        packet_candidate = context_receipt.get("packet_id")
    packet_id = packet_candidate if isinstance(packet_candidate, str) else None
    try:
        workflow_decision = parse_dialogue_decision(response_text)
    except StageIntegrationError as exc:
        # A bridge receipt only proves that one request completed.  The
        # consultation is not a completed workflow review until its response
        # carries exactly one known decision marker.  Keep the request
        # consumed, record only bounded diagnostics, and never persist the
        # response text itself.
        _record_review(
            repo,
            {
                "stage_id": selected_stage_id,
                "mode": mode,
                "status": "failed",
                "reason": reason,
                "question_summary": question[:500],
                "blocker_summary": blocker[:500],
                "workflow_decision": None,
                "codex_disposition": None,
                "failure_code": exc.code,
                "consultation_id": consultation_id,
                "conversation_id": conversation_id,
                "request_count": request_count,
                "request_id": request["request"]["request_id"],
                "evidence_digest": evidence_digest,
                "pre_prompt_recovery": copy.deepcopy(result.get("pre_prompt_recovery")),
                "context_pack_id": packet_id,
                "response_char_count": len(response_text),
                "response_sha256": hashlib.sha256(response_text.encode("utf-8")).hexdigest(),
                "receipt_path": _receipt_reference(repo, result.get("receipt_path")),
                "elapsed_ms": elapsed_ms,
                "created_at": _now(),
            },
        )
        raise CliError(exc.code, "bridge response failed workflow decision validation; no semantic retry was attempted") from exc
    resulting_action = {
        "CONTINUE": "继续当前 Stage",
        "REPLAN": "重新规划当前 Stage",
        "STAGE_READY": "等待人工审阅",
        "HUMAN_GATE": "等待人工闸门处理",
        "BLOCKED": "Stage 进入阻塞",
    }.get(workflow_decision, "未提供")
    entry = {
        "stage_id": selected_stage_id,
        "type": "FRESH_CLOSEOUT" if mode == "FRESH" else "NORMAL_CONSULTATION",
        "mode": mode,
        "status": "complete",
        "created_at": _now(),
        "reason": reason,
        "question_summary": question[:500],
        "blocker_summary": blocker[:500],
        "evidence_summary": f"{len(evidence)} evidence item(s); digest={evidence_digest}",
        "gpt_conclusion_summary": f"WORKFLOW_DECISION: {workflow_decision}",
        "transport": transport,
        "workflow_decision": workflow_decision,
        "codex_disposition": None,
        "resulting_action": resulting_action,
        "consultation_id": consultation_id,
        "conversation_id": conversation_id,
        "request_count": 1,
        "request_id": request["request"]["request_id"],
        "evidence_digest": evidence_digest,
        "pre_prompt_recovery": copy.deepcopy(result.get("pre_prompt_recovery")),
        "context_pack_id": packet_id,
        "response_char_count": len(response_text),
        "response_sha256": hashlib.sha256(response_text.encode("utf-8")).hexdigest(),
        "receipt_path": _receipt_reference(repo, result.get("receipt_path")),
        "receipt": _receipt_reference(repo, result.get("receipt_path")),
        "elapsed_ms": elapsed_ms,
        "created_at": _now(),
    }
    try:
        completed_stage = controller.show_stage(selected_stage_id)
    except (StageControllerError, ContractValidationError) as exc:
        raise CliError("STAGE_SELECTION_REJECTED", "could not select the completed Stage") from exc
    # Keep the completed consultation out of the continuity index until the
    # canonical human artifacts are written.  If that local write fails, the
    # consumed controller digest remains the exact retry barrier; recording a
    # completed entry first would change the next NORMAL context digest and
    # accidentally authorize a second external request.
    _write_lifecycle_artifacts(
        repo,
        operation="fresh-review" if mode == "FRESH" else "consult",
        event={
            "event": "consultation_complete",
            "stage_id": selected_stage_id,
            "decision": workflow_decision,
            "details": {"decision": workflow_decision},
        },
        stage=completed_stage,
        consultation=entry,
        controller=controller,
        state_snapshot=before_controller,
        state_path=state_path,
        state_file_snapshot=before_state_file,
        # The consultation request has crossed the external bridge boundary;
        # preserve its consumed digest and bounded request record so a failed
        # artifact update cannot issue the same external review again.
        rollback_state=False,
    )
    _record_review(repo, entry)
    return {
        "schema_version": CLI_SCHEMA_VERSION,
        "operation": "fresh-review" if mode == "FRESH" else "consult",
        "status": "complete",
        "mode": mode,
        "state_path": _relative(repo, state_path, "state path"),
        "stage_id": selected_stage_id,
        "consultation_id": consultation_id,
        "conversation_id": conversation_id,
        "request_count": 1,
        "context_pack_id": packet_id,
        "response_char_count": len(response_text),
        "response_sha256": entry["response_sha256"],
        "receipt_path": entry["receipt_path"],
        "elapsed_ms": elapsed_ms,
        # The response is returned to the invoking Codex/user, but never
        # persisted in stage state or the review index.
        "response": response_text,
    }


def _show_artifacts(args: argparse.Namespace, repo: Path, state_arg: str | None) -> dict[str, Any]:
    controller, state_path = _load_controller(repo, state_arg)
    stage_id = _stage_id_from(args)
    try:
        stage = controller.show_stage(stage_id)
    except (StageControllerError, ContractValidationError) as exc:
        raise CliError("STAGE_SELECTION_REJECTED", "could not select the requested Stage") from exc
    contract = stage.get("contract", {})
    required = contract.get("review_artifact_requirements", [])
    latest = stage.get("latest_result") if isinstance(stage.get("latest_result"), Mapping) else {}
    declared = latest.get("review_artifacts", []) if isinstance(latest, Mapping) else []
    artifacts: list[dict[str, Any]] = []
    if isinstance(declared, Mapping):
        declared = [{"role": key, "value": value} for key, value in declared.items()]
    if isinstance(declared, list):
        for item in declared:
            if isinstance(item, str):
                item = {"uri": item}
            if not isinstance(item, Mapping):
                continue
            safe = {key: copy.deepcopy(value) for key, value in item.items() if key in {"role", "type", "uri", "path", "artifact_id", "lane"}}
            candidate = safe.get("path")
            if isinstance(candidate, str):
                path = _resolve_under(repo, candidate, "artifact path", must_exist=False)
                safe["path"] = _relative(repo, path, "artifact path")
                safe["exists"] = path.is_file()
            artifacts.append(safe)
    artifact_root = repo / ".research" / "stages" / str(contract.get("stage_id", stage_id or "")) / "artifacts"
    if artifact_root.is_dir():
        for path in sorted(artifact_root.rglob("*")):
            if path.is_file():
                relative = _relative(repo, path, "artifact path")
                if not any(item.get("path") == relative for item in artifacts):
                    artifacts.append({"path": relative, "exists": True})
    _assert_no_secrets(artifacts, path="artifacts")
    return {
        "schema_version": CLI_SCHEMA_VERSION,
        "operation": "show-artifacts",
        "state_path": _relative(repo, state_path, "state path"),
        "stage_id": contract.get("stage_id"),
        "required": copy.deepcopy(required),
        "artifacts": artifacts,
        "stage_status": stage.get("status"),
    }


def _requirements_brief_args(args: argparse.Namespace) -> dict[str, Any] | None:
    """Collect explicit requirements fields for the Step 11 adapter."""

    values: dict[str, Any] = {}
    raw = getattr(args, "brief", None)
    if raw:
        candidate = Path(str(raw)).expanduser()
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8")) if candidate.is_file() else json.loads(str(raw))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CliError("REQUIREMENTS_INPUT_INVALID", "brief must be valid JSON or a JSON file") from exc
        if not isinstance(payload, Mapping):
            raise CliError("REQUIREMENTS_INPUT_INVALID", "brief must contain an object")
        _assert_no_secrets(payload, path="brief")
        values.update(dict(payload))
    for key, attr in {
        "title": "title",
        "problem_statement": "problem_statement",
        "goal": "goal",
        "desired_outcome": "desired_outcome",
        "priority": "priority",
        "success_criteria": "success_criteria",
        "acceptance_criteria": "acceptance_criteria",
        "constraints": "constraints",
        "non_goals": "non_goals",
        "scope": "scope",
        "stakeholders": "stakeholders",
    }.items():
        value = getattr(args, attr, None)
        if value not in (None, "", []):
            values[key] = value
    return values or None


def _requirements_update_args(args: argparse.Namespace) -> Mapping[str, Any]:
    raw = getattr(args, "json_update", None) or getattr(args, "brief_update", None) or getattr(args, "updates", None)
    if raw:
        try:
            payload = json.loads(str(raw))
        except (TypeError, json.JSONDecodeError) as exc:
            raise CliError("REQUIREMENTS_INPUT_INVALID", "update must be valid JSON") from exc
        if not isinstance(payload, Mapping):
            raise CliError("REQUIREMENTS_INPUT_INVALID", "update must contain an object")
        return dict(payload)
    values: dict[str, Any] = {}
    for item in getattr(args, "field", []) or []:
        if "=" not in item:
            raise CliError("REQUIREMENTS_INPUT_INVALID", "--field values must use name=value")
        key, value = item.split("=", 1)
        if not key.strip():
            raise CliError("REQUIREMENTS_INPUT_INVALID", "--field name must be non-empty")
        values[key.strip()] = value.strip()
    if not values:
        raise CliError("REQUIREMENTS_INPUT_INVALID", "an update object or --field is required")
    return values


def _requirements_operation(args: argparse.Namespace, repo: Path) -> dict[str, Any]:
    """Run one project-level intake operation; no Stage/GPT boundary is used."""

    service = ProjectRequirementsIntake(repo)
    command = args.command
    try:
        if command in {"requirements-init", "requirements_init", "init-requirements"}:
            return service.initialize(
                mode=getattr(args, "mode", None),
                rough_requirement=(
                    getattr(args, "rough_requirement", None)
                    if getattr(args, "rough_requirement", None) is not None
                    else getattr(args, "rough_requirement_arg", None)
                ),
                brief=_requirements_brief_args(args),
            )
        if command in {"requirements-show", "requirements_show", "show-requirements"}:
            return service.show()
        if command in {"requirements-answer", "requirements_answer", "answer-requirements"}:
            answer = getattr(args, "answer_option", None)
            if answer is None:
                answer = getattr(args, "answer", None)
            if answer is None:
                raise CliError("REQUIREMENTS_INPUT_INVALID", "an answer is required")
            return service.answer(answer, question_id=getattr(args, "question_id", None))
        if command in {"requirements-update", "requirements_update", "update-requirements"}:
            return service.update(_requirements_update_args(args), rough_requirement=getattr(args, "rough_requirement", None))
        if command in {"requirements-approve", "requirements_approve", "approve-requirements"}:
            return service.approve(actor=getattr(args, "actor", "local-human"), rationale=getattr(args, "rationale", ""))
        if command in {"requirements-cancel", "requirements_cancel", "cancel-requirements"}:
            reason = getattr(args, "reason", None)
            if reason is None:
                reason = getattr(args, "reason_arg", None)
            return service.cancel(actor=getattr(args, "actor", "local-human"), reason=reason or "")
        if command in {"intake", "natural-language", "requirements"}:
            text_value = getattr(args, "text_option", None)
            if text_value is None:
                text_value = getattr(args, "text", None)
            if text_value is None:
                raise CliError("REQUIREMENTS_INPUT_INVALID", "natural-language intake text is required")
            return natural_language_entry(repo, text_value, mode=getattr(args, "mode", None))
        raise CliError("REQUIREMENTS_COMMAND_INVALID", "unknown requirements command")
    except ProjectIntakeError as exc:
        raise CliError(f"REQUIREMENTS_{exc.code}", str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded Stage-Oriented Research workflow CLI")
    parser.add_argument("--repo", "-C", default=".", help="project repository (default: current directory)")
    parser.add_argument("--state", help="state file inside --repo (default: .research/stage-state.json)")
    parser.add_argument("--bridge-root", help="existing chatgpt_browser_bridge checkout")
    parser.add_argument("--profile-dir", help="existing headed browser profile; never persisted or modified")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS, help="single bridge wait bound")
    parser.add_argument(
        "--real-run",
        action="store_true",
        help="explicitly allow the default headed bridge to contact ChatGPT (injected test runners remain offline)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", aliases=["create"], help="register a PLANNED stage contract")
    prepare.add_argument("stage_id", nargs="?", help="optional stage id, checked against contract")
    prepare.add_argument("--contract", required=True, help="stage_contract.v1 JSON inside the repository")

    stage_plan = sub.add_parser(
        "plan-stage",
        aliases=["plan_stage", "stage-plan", "planning"],
        help="derive and register one PLANNED stage from verified Bootstrap outputs",
    )
    stage_plan.add_argument("stage_id", nargs="?", help="optional deterministic Stage id override")
    stage_plan.add_argument("--stage-id", dest="stage_id_option", help="optional deterministic Stage id override")
    stage_plan.add_argument("--stage-name", default=None)
    stage_plan.add_argument("--allowed-path", action="append", default=None)
    stage_plan.add_argument("--required-check", action="append", default=None)
    stage_plan.add_argument("--review-artifact", action="append", default=None)
    stage_plan.add_argument("--max-iterations", type=int, default=1)
    stage_plan.add_argument("--retry-budget", type=int, default=None)
    stage_plan.add_argument("--bootstrap-state", default=None, help="canonical .research/bootstrap_state.json")

    for name, help_text in (
        ("start", "explicitly start a PLANNED stage"),
        ("stop", "stop an ACTIVE or STAGE_READY stage"),
        ("pause", "alias for stop; no background work is resumed"),
        ("approve", "approve a STAGE_READY stage"),
        ("reject", "return a STAGE_READY stage to ACTIVE"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("stage_id", nargs="?", help="stage id (optional when unambiguous)")
        command.add_argument("--actor", default="local-human")
        command.add_argument("--rationale", default="")

    show = sub.add_parser("show", help="show bounded stage status")
    show.add_argument("stage_id", nargs="?", help="stage id (optional when unambiguous)")
    show.add_argument("--all", action="store_true", help="show every registered stage")

    for name, aliases, help_text in (
        ("consult", [], "consult ChatGPT in NORMAL mode"),
        ("fresh-review", ["fresh", "fresh_review"], "request an explicitly bounded FRESH review"),
    ):
        command = sub.add_parser(name, aliases=aliases, help=help_text)
        command.add_argument("stage_id", nargs="?", help="stage id (optional when unambiguous)")
        command.add_argument("--question", default=None)
        command.add_argument("--blocker", default=None)
        command.add_argument("--reason", default=None)
        command.add_argument("--evidence", action="append", default=[], help="repo-relative bounded evidence file; repeatable")
        command.add_argument("--dry-run", action="store_true", help="validate and show the plan without contacting ChatGPT")
        command.add_argument("--transport", choices=("project", "homepage_fallback"), default="project",
                             help="explicit browser transport; fallback never claims Project UI scope")
        command.add_argument(
            "--real-run",
            action="store_true",
            default=argparse.SUPPRESS,
            help="explicitly allow the headed bridge to contact ChatGPT",
        )

    artifacts = sub.add_parser("show-artifacts", aliases=["artifacts"], help="show bounded review artifact references")
    artifacts.add_argument("stage_id", nargs="?", help="stage id (optional when unambiguous)")

    # Step 11 is deliberately a separate product surface.  These commands
    # only read/write ``.research/PROJECT_BRIEF.json`` and never enter the
    # Stage controller or consultation bridge.
    req_init = sub.add_parser(
        "requirements-init",
        aliases=["requirements_init", "init-requirements"],
        help="create or revise the canonical project brief",
    )
    req_init.add_argument("rough_requirement_arg", nargs="?")
    req_init.add_argument("--mode", default="CODEX_REQUIREMENTS_INTERVIEW")
    req_init.add_argument("--rough-requirement", "--requirement", "--text", default=None)
    req_init.add_argument("--brief", "--brief-file", help="inline JSON object or JSON file containing the user brief")
    req_init.add_argument("--title")
    req_init.add_argument("--problem-statement")
    req_init.add_argument("--goal", "--project-goal")
    req_init.add_argument("--desired-outcome", "--outcome")
    req_init.add_argument("--priority")
    req_init.add_argument("--success-criteria", action="append", default=[])
    req_init.add_argument("--acceptance-criteria", action="append", default=[])
    req_init.add_argument("--constraints", action="append", default=[])
    req_init.add_argument("--non-goals", "--non-goal", action="append", default=[])
    req_init.add_argument("--scope", action="append", default=[])
    req_init.add_argument("--stakeholders", action="append", default=[])

    sub.add_parser(
        "requirements-show",
        aliases=["requirements_show", "show-requirements"],
        help="show the canonical project brief",
    )

    req_answer = sub.add_parser(
        "requirements-answer",
        aliases=["requirements_answer", "answer-requirements"],
        help="answer the one current core question",
    )
    req_answer.add_argument("answer", nargs="?")
    req_answer.add_argument("--answer", dest="answer_option")
    req_answer.add_argument("--question-id", "--question", dest="question_id")

    req_update = sub.add_parser(
        "requirements-update",
        aliases=["requirements_update", "update-requirements"],
        help="apply an explicit project brief revision",
    )
    req_update.add_argument("updates", nargs="?")
    req_update.add_argument("--json", "--update", dest="json_update")
    req_update.add_argument("--brief", "--brief-update", dest="brief_update")
    req_update.add_argument("--field", action="append", default=[])
    req_update.add_argument("--rough-requirement", "--requirement", default=None)

    req_approve = sub.add_parser(
        "requirements-approve",
        aliases=["requirements_approve", "approve-requirements"],
        help="approve a ready project brief",
    )
    req_approve.add_argument("--actor", default="local-human")
    req_approve.add_argument("--rationale", default="")

    req_cancel = sub.add_parser(
        "requirements-cancel",
        aliases=["requirements_cancel", "cancel-requirements"],
        help="cancel intake fail-closed",
    )
    req_cancel.add_argument("--actor", default="local-human")
    req_cancel.add_argument("reason_arg", nargs="?")
    req_cancel.add_argument("--reason", default=None)

    req_intake = sub.add_parser(
        "intake",
        aliases=["natural-language", "requirements"],
        help="recognize a natural-language request and enter intake",
    )
    req_intake.add_argument("text", nargs="?")
    req_intake.add_argument("--text", dest="text_option")
    req_intake.add_argument("--mode", default="CODEX_REQUIREMENTS_INTERVIEW")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Callable[..., Mapping[str, Any]] = subprocess_bridge_runner,
) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.timeout_ms < 1 or args.timeout_ms > MAX_TIMEOUT_MS:
            raise CliError("TIMEOUT_INVALID", f"timeout must be between 1 and {MAX_TIMEOUT_MS} ms")
        repo = _resolve_directory(args.repo, "repository")
        if args.profile_dir is not None:
            args.profile_dir = str(_resolve_directory(args.profile_dir, "profile directory"))
        if args.bridge_root is not None:
            args.bridge_root = str(_resolve_directory(args.bridge_root, "bridge root"))
        if args.command in {
            "requirements-init", "requirements_init", "init-requirements",
            "requirements-show", "requirements_show", "show-requirements",
            "requirements-answer", "requirements_answer", "answer-requirements",
            "requirements-update", "requirements_update", "update-requirements",
            "requirements-approve", "requirements_approve", "approve-requirements",
            "requirements-cancel", "requirements_cancel", "cancel-requirements",
            "intake", "natural-language", "requirements",
        }:
            payload = _requirements_operation(args, repo)
        elif args.command in {"prepare", "create"}:
            payload = _prepare(args, repo, args.state)
        elif args.command in {"plan-stage", "plan_stage", "stage-plan", "planning"}:
            payload = _plan_stage(args, repo, args.state)
        elif args.command in {"start", "stop", "pause", "approve", "reject"}:
            args.operation = args.command
            payload = _lifecycle(args, repo, args.state)
        elif args.command == "show":
            payload = _show(args, repo, args.state)
        elif args.command == "consult":
            payload = _consult(args, repo, args.state, mode="NORMAL", runner=runner)
        elif args.command in {"fresh-review", "fresh", "fresh_review"}:
            payload = _consult(args, repo, args.state, mode="FRESH", runner=runner)
        elif args.command in {"show-artifacts", "artifacts"}:
            payload = _show_artifacts(args, repo, args.state)
        else:  # pragma: no cover - argparse constrains this branch
            raise CliError("COMMAND_INVALID", "unknown command")
        _emit(payload)
        return 0
    except CliError as exc:
        print(f"ERROR {exc.code}: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError) as exc:
        # Keep unexpected local failures bounded and do not print exception
        # details that might contain a path or external payload.
        print(f"ERROR CLI_INTERNAL: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
