"""Planning and lifecycle entry points for the Execution Integration stage.

The original Step 15 planner owns one canonical Bootstrap plan.  Reusing that
single envelope for a later delivery stage would either overwrite the history
of the implementation stage or make the old ``PLANNED`` boundary lie about a
closed stage.  This module keeps the later stage in its own contract and plan
directory while reusing the same :class:`StageController` state file.

The planner only registers a new ``PLANNED`` Stage.  ``start_*`` is a thin
forward-path wrapper around ``StageController.start_stage``; no result is
promoted and no integration is performed here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import (
    ContractValidationError,
    _ensure_relative_path,
    canonical_json,
    sha256_json,
    validate_against_schema,
)
from .project_state import ProjectStateError, resolve_project_root
from .stage_controller import (
    StageController,
    StageControllerError,
    StageState,
    validate_stage_contract_v1,
)


EXECUTION_INTEGRATION_STAGE_ID = "execution-integration-delivery-v1"
EXECUTION_INTEGRATION_STAGE_NAME = "Execution Integration Delivery V1"
EXECUTION_INTEGRATION_PLANNING_SCHEMA_VERSION = "execution_integration_stage_planning.v1"
EXECUTION_INTEGRATION_PLANNING_MARKER = "EXECUTION_INTEGRATION_STAGE_PLANNING_PASS"
EXECUTION_INTEGRATION_NEXT_ACTION = "USER_START_STAGE"
EXECUTION_INTEGRATION_STAGE_PLAN_RELATIVE_PATH = (
    Path(".research") / "stages" / EXECUTION_INTEGRATION_STAGE_ID / "STAGE_PLAN.json"
)
EXECUTION_INTEGRATION_STAGE_STATE_RELATIVE_PATH = Path(".research") / "stage-state.json"
EXECUTION_INTEGRATION_STAGE_CONTRACT_FILENAME = "contract.json"
EXECUTION_INTEGRATION_DELIVERY_ARTIFACT_RELATIVE_PATH = (
    Path(".research") / "integration" / "execution-integration-delivery-v1.receipt.json"
)
MAX_EXECUTION_INTEGRATION_ARTIFACT_BYTES = 180_000
_STAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

_SENSITIVE_KEYS = frozenset(
    {
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
        "transcript",
        "chat_history",
    }
)

_CORE_PROTECTED_PATHS = (
    ".git",
    ".auth",
    ".consultations",
    ".research/PROJECT_BRIEF.json",
    ".research/DESIGN_PACKAGE.json",
    ".research/workflow-state.json",
    ".research/design-review/HUMAN_ACCEPT_RECEIPT.json",
    ".research/execution-handoff/EXECUTION_HANDOFF.json",
    ".research/stage-planning/STAGE_PLAN.json",
    ".research/stage-state.json",
)


class ExecutionIntegrationPlanningError(RuntimeError):
    """Stable, fail-closed error for the later delivery-stage seam."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


def _fail(code: str, message: str, exc: BaseException | None = None) -> ExecutionIntegrationPlanningError:
    error = ExecutionIntegrationPlanningError(code, message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _assert_safe(value: Any, *, path: str = "$", depth: int = 0) -> None:
    if depth > 14:
        raise _fail("EXECUTION_INTEGRATION_PLAN_TOO_DEEP", f"input nesting is too deep at {path}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).strip().lower().replace("-", "_")
            if key_text in _SENSITIVE_KEYS:
                raise _fail("EXECUTION_INTEGRATION_PLAN_SECRET_REJECTED", f"sensitive field is not allowed at {path}")
            _assert_safe(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise _fail("EXECUTION_INTEGRATION_PLAN_LIMIT", f"list is too large at {path}")
        for index, child in enumerate(value):
            _assert_safe(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str) and "\x00" in value:
        raise _fail("EXECUTION_INTEGRATION_PLAN_INVALID_TEXT", f"NUL character is not allowed at {path}")


def _text(value: Any, field: str, *, maximum: int = 12_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum or "\x00" in value:
        raise _fail("EXECUTION_INTEGRATION_PLAN_INPUT_INVALID", f"{field} must be bounded non-empty text")
    return value.strip()


def _relative_path(value: Any, field: str) -> str:
    try:
        normalized = _ensure_relative_path(_text(value, field, maximum=1024), field)
    except ContractValidationError as exc:
        raise _fail("EXECUTION_INTEGRATION_PLAN_PATH_INVALID", f"{field} must be a safe relative path", exc) from exc
    return normalized


def _digest(value: Any, field: str) -> str:
    candidate = _text(value, field, maximum=64)
    if _HEX64_RE.fullmatch(candidate) is None:
        raise _fail("EXECUTION_INTEGRATION_PLAN_DIGEST_INVALID", f"{field} must be a lowercase SHA-256 digest")
    return candidate


def _resolve_root(project_root: str | os.PathLike[str]) -> Path:
    try:
        return resolve_project_root(project_root)
    except ProjectStateError as exc:
        raise _fail("EXECUTION_INTEGRATION_PROJECT_ROOT_INVALID", "project root could not be resolved", exc) from exc


def _repo_path(root: Path, relative: str, field: str, *, require_file: bool = False) -> Path:
    normalized = _relative_path(relative, field)
    candidate = root.joinpath(*normalized.split("/"))
    if candidate.is_symlink():
        raise _fail("EXECUTION_INTEGRATION_PLAN_PATH_INVALID", f"{field} must not be a symlink")
    try:
        resolved = candidate.resolve(strict=require_file)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _fail("EXECUTION_INTEGRATION_PLAN_PATH_INVALID", f"{field} escaped the project root", exc) from exc
    if require_file and not candidate.is_file():
        raise _fail("EXECUTION_INTEGRATION_PLAN_PATH_INVALID", f"{field} must name a regular file")
    return candidate


def _read_json(path: Path, field: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise _fail("EXECUTION_INTEGRATION_PLAN_NOT_FOUND", f"{field} is not a regular file")
    try:
        if path.stat().st_size > MAX_EXECUTION_INTEGRATION_ARTIFACT_BYTES:
            raise _fail("EXECUTION_INTEGRATION_PLAN_TOO_LARGE", f"{field} exceeds its bounded size")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ExecutionIntegrationPlanningError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("EXECUTION_INTEGRATION_PLAN_INVALID", f"{field} could not be read", exc) from exc
    if not isinstance(value, dict):
        raise _fail("EXECUTION_INTEGRATION_PLAN_INVALID", f"{field} must contain an object")
    _assert_safe(value, path=field)
    return value


def _file_digest(path: Path, field: str) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise _fail("EXECUTION_INTEGRATION_PLAN_INPUT_MISSING", f"{field} is not a regular file")
    try:
        size = path.stat().st_size
        if size > MAX_EXECUTION_INTEGRATION_ARTIFACT_BYTES:
            raise _fail("EXECUTION_INTEGRATION_PLAN_TOO_LARGE", f"{field} exceeds its bounded size")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except ExecutionIntegrationPlanningError:
        raise
    except OSError as exc:
        raise _fail("EXECUTION_INTEGRATION_PLAN_INPUT_UNREADABLE", f"{field} could not be read", exc) from exc
    return digest.hexdigest(), size


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _assert_safe(value, path=str(path))
    encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > MAX_EXECUTION_INTEGRATION_ARTIFACT_BYTES:
        raise _fail("EXECUTION_INTEGRATION_PLAN_TOO_LARGE", f"{path.name} exceeds its bounded size")
    if path.exists() and (path.is_symlink() or path.is_dir()):
        raise _fail("EXECUTION_INTEGRATION_PLAN_WRITE_FAILED", f"{path.name} is not a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise _fail("EXECUTION_INTEGRATION_PLAN_WRITE_FAILED", f"could not write {path.name}", exc) from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _scope_matches(path: str, scope: str) -> bool:
    return path == scope or path.startswith(scope.rstrip("/") + "/")


def _delivery_artifact_relative_path(stage_id: str) -> str:
    """Return the deterministic receipt location owned by this Stage."""

    if stage_id == EXECUTION_INTEGRATION_STAGE_ID:
        return EXECUTION_INTEGRATION_DELIVERY_ARTIFACT_RELATIVE_PATH.as_posix()
    return f".research/integration/{stage_id}.receipt.json"


def _normalise_paths(values: Sequence[str] | None, field: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes, bytearray)):
        raise _fail("EXECUTION_INTEGRATION_PLAN_INPUT_INVALID", f"{field} must be an array")
    result: list[str] = []
    for value in values:
        normalized = _relative_path(value, f"{field} item")
        if normalized not in result:
            result.append(normalized)
    return result


def _source_result(source_stage: Mapping[str, Any], source_stage_id: str) -> tuple[str, dict[str, Any]]:
    if source_stage.get("status") != StageState.APPROVED.value:
        raise _fail(
            "EXECUTION_INTEGRATION_SOURCE_NOT_CLOSED",
            f"source Stage {source_stage_id} must be APPROVED before a delivery Stage is planned",
        )
    binding = source_stage.get("stage_result_binding")
    if not isinstance(binding, Mapping) or binding.get("evidence_complete") is not True:
        raise _fail("EXECUTION_INTEGRATION_SOURCE_EVIDENCE_INCOMPLETE", "source Stage has no complete current result binding")
    if binding.get("stage_id") not in {None, source_stage_id}:
        raise _fail("EXECUTION_INTEGRATION_SOURCE_IDENTITY_MISMATCH", "source result binding names another Stage")
    result_digest = _digest(binding.get("result_digest"), "source_stage_result_digest")
    latest = source_stage.get("latest_stage_result")
    if not isinstance(latest, Mapping):
        latest = source_stage.get("latest_result")
    if not isinstance(latest, Mapping):
        raise _fail("EXECUTION_INTEGRATION_SOURCE_RESULT_MISSING", "source Stage has no reviewed execution result")
    return result_digest, dict(latest)


def resolve_integration_target(
    source_stage: Mapping[str, Any],
    *,
    explicit_target: str | None = None,
) -> dict[str, Any]:
    """Resolve one target from Stage planning identity, never from a guess.

    An explicit value is accepted only at planning time.  If the source
    contract/result already carries a target, a conflicting explicit value is
    rejected as ambiguous.  ``changed_files`` is deliberately not used as a
    target source: execution evidence describes what happened, not what the
    project authoritatively intended to receive the delivery.
    """

    candidates: dict[str, list[str]] = {}

    def add(value: Any, origin: str) -> None:
        if value is None:
            return
        if not isinstance(value, str) or not value.strip():
            raise _fail("EXECUTION_INTEGRATION_TARGET_INVALID", f"{origin} must be a non-empty relative path")
        normalized = _relative_path(value, "integration_target")
        candidates.setdefault(normalized, []).append(origin)

    contract = source_stage.get("contract")
    if isinstance(contract, Mapping):
        add(contract.get("integration_target"), "source.contract.integration_target")
        metadata = contract.get("metadata")
        if isinstance(metadata, Mapping):
            add(metadata.get("integration_target"), "source.contract.metadata.integration_target")
    for key in ("integration_target", "authoritative_integration_target"):
        add(source_stage.get(key), f"source.{key}")
    for result_key in ("latest_stage_result", "latest_result"):
        result = source_stage.get(result_key)
        if isinstance(result, Mapping):
            add(result.get("integration_target"), f"source.{result_key}.integration_target")
            add(result.get("authoritative_integration_target"), f"source.{result_key}.authoritative_integration_target")
    if explicit_target is not None:
        add(explicit_target, "planning.explicit_integration_target")
    if not candidates:
        raise _fail(
            "EXECUTION_INTEGRATION_TARGET_UNRESOLVED",
            "no authoritative integration_target is present in Stage planning identity",
        )
    if len(candidates) != 1:
        details = ", ".join(sorted(candidates))
        raise _fail("EXECUTION_INTEGRATION_TARGET_AMBIGUOUS", f"multiple authoritative targets were found: {details}")
    target, origins = next(iter(candidates.items()))
    if any(_scope_matches(target, protected) for protected in _CORE_PROTECTED_PATHS):
        raise _fail("EXECUTION_INTEGRATION_TARGET_PROTECTED", "integration_target is protected by the workflow")
    return {"path": target, "origins": origins}


def _verification_spec(
    source_stage: Mapping[str, Any],
    source_result: Mapping[str, Any],
    explicit_commands: Sequence[str] | None,
) -> dict[str, Any]:
    commands: list[str] = []

    def add_command(value: Any) -> None:
        if isinstance(value, str) and value.strip() and value.strip() not in commands:
            commands.append(value.strip()[:2048])

    for value in explicit_commands or []:
        add_command(value)
    metadata = source_stage.get("contract", {}).get("metadata") if isinstance(source_stage.get("contract"), Mapping) else None
    if isinstance(metadata, Mapping):
        for key in ("verification_commands", "post_integration_verification_commands", "required_test_command"):
            value = metadata.get(key)
            if isinstance(value, str):
                add_command(value)
            elif isinstance(value, list):
                for item in value:
                    add_command(item)
    tests = source_result.get("tests")
    if isinstance(tests, list):
        for item in tests:
            if isinstance(item, str):
                add_command(item)
            elif isinstance(item, Mapping):
                add_command(item.get("command", item.get("name")))
    contract = source_stage.get("contract")
    source_checks = list(contract.get("required_checks", [])) if isinstance(contract, Mapping) and isinstance(contract.get("required_checks"), list) else []
    return {
        "identity_match_required": True,
        "source_stage_required_checks": [str(item) for item in source_checks if isinstance(item, str)],
        "commands": commands,
        "source": "source_stage_contract_and_reviewed_execution_result",
    }


def _input_descriptors(root: Path, source_stage_id: str, target: str) -> tuple[list[str], dict[str, str], dict[str, int]]:
    candidates = [
        ".research/PROJECT_BRIEF.json",
        ".research/DESIGN_PACKAGE.json",
        ".research/design-review/HUMAN_ACCEPT_RECEIPT.json",
        ".research/execution-handoff/EXECUTION_HANDOFF.json",
        ".research/stage-planning/STAGE_PLAN.json",
        ".research/stage-state.json",
        f".research/stages/{source_stage_id}/contract.json",
        ".research/reviews/STAGE_CLOSEOUT.json",
        ".research/review-evidence/gpt-review-input.json",
        ".research/review-evidence/stage-result.json",
        ".research/review-evidence/tests.json",
        target,
    ]
    paths: list[str] = []
    digests: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for relative in candidates:
        normalized = _relative_path(relative, "input path")
        if normalized in digests:
            continue
        candidate = root.joinpath(*normalized.split("/"))
        if candidate.is_symlink() or not candidate.is_file():
            continue
        digest, size = _file_digest(candidate, f"input {normalized}")
        paths.append(normalized)
        digests[normalized] = digest
        sizes[normalized] = size
    if not paths:
        raise _fail("EXECUTION_INTEGRATION_PLAN_INPUT_MISSING", "no bounded source evidence exists for delivery planning")
    return paths, digests, sizes


def _plan_payload(
    *,
    root: Path,
    contract: Mapping[str, Any],
    source_stage_id: str,
    source_stage_result_digest: str,
    source_state_revision: int,
    target: str,
    target_before_digest: str | None,
    verification: Mapping[str, Any],
    input_paths: Sequence[str],
    input_digests: Mapping[str, str],
    input_sizes: Mapping[str, int],
    stage_view: Mapping[str, Any],
    reused: bool,
) -> dict[str, Any]:
    contract_relative = f".research/stages/{contract['stage_id']}/{EXECUTION_INTEGRATION_STAGE_CONTRACT_FILENAME}"
    plan_relative = EXECUTION_INTEGRATION_STAGE_PLAN_RELATIVE_PATH.as_posix()
    payload: dict[str, Any] = {
        "schema_version": EXECUTION_INTEGRATION_PLANNING_SCHEMA_VERSION,
        "marker": EXECUTION_INTEGRATION_PLANNING_MARKER,
        "planning_status": "EXECUTION_INTEGRATION_STAGE_PLANNING_COMPLETE",
        "status": StageState.PLANNED.value,
        "stage_status": StageState.PLANNED.value,
        "next_action": EXECUTION_INTEGRATION_NEXT_ACTION,
        "stage_created": True,
        "stage_started": False,
        "project_id": contract["project_id"],
        "repository_root": root.as_posix(),
        "plan_id": contract["plan_id"],
        "stage_id": contract["stage_id"],
        "stage_name": contract["stage_name"],
        "stage_plan_path": plan_relative,
        "stage_contract_path": contract_relative,
        "stage_state_path": EXECUTION_INTEGRATION_STAGE_STATE_RELATIVE_PATH.as_posix(),
        "source_stage_id": source_stage_id,
        "source_stage_status": StageState.APPROVED.value,
        "source_stage_result_digest": source_stage_result_digest,
        "source_stage_state_revision": source_state_revision,
        "integration_target": target,
        "target_before_digest": target_before_digest,
        "delivery_artifact_path": contract.get("delivery_artifact_path"),
        "verification_spec": copy.deepcopy(dict(verification)),
        "input_paths": list(input_paths),
        "input_digests": dict(input_digests),
        "input_sizes": {key: int(value) for key, value in input_sizes.items()},
        "baseline_digest": sha256_json(contract["baseline"]),
        "contract_digest": sha256_json(contract),
        "idempotent_reuse": bool(reused),
        "stage": copy.deepcopy(dict(stage_view)),
    }
    _assert_safe(payload, path="execution_integration_stage_plan")
    try:
        validate_against_schema(payload, "execution_integration_stage_planning.v1")
    except ContractValidationError as exc:
        raise _fail("EXECUTION_INTEGRATION_PLAN_SCHEMA_INVALID", "delivery Stage plan failed schema validation", exc) from exc
    return payload


def _load_plan(root: Path) -> dict[str, Any]:
    path = root / EXECUTION_INTEGRATION_STAGE_PLAN_RELATIVE_PATH
    value = _read_json(path, "Execution Integration Stage plan")
    try:
        validate_against_schema(value, "execution_integration_stage_planning.v1")
    except ContractValidationError as exc:
        raise _fail("EXECUTION_INTEGRATION_PLAN_INVALID", "delivery Stage plan failed schema validation", exc) from exc
    return value


def plan_execution_integration_stage(
    project_root: str | os.PathLike[str] = ".",
    *,
    source_stage_id: str = "implementation",
    stage_id: str = EXECUTION_INTEGRATION_STAGE_ID,
    stage_name: str = EXECUTION_INTEGRATION_STAGE_NAME,
    integration_target: str | None = None,
    allowed_paths: Sequence[str] | None = None,
    protected_paths: Sequence[str] | None = None,
    required_checks: Sequence[str] | None = None,
    review_artifact_requirements: Sequence[str] | None = None,
    verification_commands: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Register a delivery Stage from a completed source Stage.

    The source Stage must already be ``APPROVED`` with a complete current
    result binding.  The source record is never changed.  Re-running with the
    same source result, target, and evidence returns the existing plan without
    appending another registration event.
    """

    root = _resolve_root(project_root)
    source_stage_id = _text(source_stage_id, "source_stage_id", maximum=80)
    stage_id = _text(stage_id, "stage_id", maximum=80)
    if _STAGE_ID_RE.fullmatch(stage_id) is None:
        raise _fail("EXECUTION_INTEGRATION_STAGE_ID_INVALID", "stage_id contains unsafe characters")
    stage_name = _text(stage_name, "stage_name", maximum=512)
    if source_stage_id == stage_id:
        raise _fail("EXECUTION_INTEGRATION_STAGE_ID_INVALID", "delivery Stage must have a distinct identity")

    state_path = root / EXECUTION_INTEGRATION_STAGE_STATE_RELATIVE_PATH
    if state_path.is_symlink() or not state_path.is_file():
        raise _fail("EXECUTION_INTEGRATION_STAGE_STATE_NOT_FOUND", "a current StageController state is required")
    try:
        controller = StageController.from_state(state_path)
        source_stage = controller.show_stage(source_stage_id)
    except (StageControllerError, ContractValidationError, OSError) as exc:
        raise _fail("EXECUTION_INTEGRATION_SOURCE_STATE_INVALID", "source Stage state could not be loaded", exc) from exc
    result_digest, source_result = _source_result(source_stage, source_stage_id)
    target_info = resolve_integration_target(source_stage, explicit_target=integration_target)
    target = str(target_info["path"])
    target_path = _repo_path(root, target, "integration_target")
    target_before_digest: str | None = None
    if target_path.exists() or target_path.is_symlink():
        target_before_digest, _ = _file_digest(target_path, "integration_target")

    # Resolve the receipt location as part of Stage planning.  Keeping this
    # beside the target in the plan gives the execution seam a stable final
    # artifact path without guessing from the implementation directory.
    delivery_artifact = _delivery_artifact_relative_path(stage_id)
    delivery_artifact_path = _repo_path(root, delivery_artifact, "delivery_artifact_path")

    # A re-run sees the controller revision after the delivery Stage was
    # registered.  That revision is runtime bookkeeping, not a new source
    # identity, so compare the existing plan before rebuilding its contract.
    # This also preserves the original input digest for stage-state.json and
    # makes resume idempotent without rewriting the historical plan/receipt.
    plan_path = root / EXECUTION_INTEGRATION_STAGE_PLAN_RELATIVE_PATH
    contract_path = root / ".research" / "stages" / stage_id / EXECUTION_INTEGRATION_STAGE_CONTRACT_FILENAME
    if plan_path.exists() or plan_path.is_symlink():
        existing_plan = _load_plan(root)
        if existing_plan.get("stage_id") != stage_id or existing_plan.get("source_stage_id") != source_stage_id:
            raise _fail("EXECUTION_INTEGRATION_PLAN_IDENTITY_MISMATCH", "existing delivery plan belongs to another Stage")
        if (
            existing_plan.get("source_stage_result_digest") != result_digest
            or existing_plan.get("integration_target") != target
            or existing_plan.get("target_before_digest") != target_before_digest
        ):
            raise _fail("EXECUTION_INTEGRATION_PLAN_STALE", "existing delivery plan does not match current source evidence")
        existing_artifact = existing_plan.get("delivery_artifact_path")
        if existing_artifact is not None and existing_artifact != delivery_artifact:
            raise _fail("EXECUTION_INTEGRATION_PLAN_STALE", "existing delivery plan has a different delivery artifact path")
        if not contract_path.is_file() or contract_path.is_symlink():
            raise _fail("EXECUTION_INTEGRATION_CONTRACT_MISSING", "existing delivery plan has no contract file")
        existing_contract = _read_json(contract_path, "delivery Stage contract")
        if existing_plan.get("contract_digest") != sha256_json(existing_contract):
            raise _fail("EXECUTION_INTEGRATION_CONTRACT_STALE", "existing delivery contract digest is stale")
        try:
            stage = controller.show_stage(stage_id)
        except StageControllerError as exc:
            raise _fail("EXECUTION_INTEGRATION_STATE_INVALID", "existing delivery plan has no controller Stage", exc) from exc
        if not isinstance(stage, Mapping) or stage.get("status") != StageState.PLANNED.value:
            raise _fail("EXECUTION_INTEGRATION_STATE_INVALID", "existing delivery Stage is no longer PLANNED")
        result = copy.deepcopy(existing_plan)
        result["idempotent_reuse"] = True
        result["stage"] = copy.deepcopy(stage)
        return result

    source_contract = source_stage.get("contract")
    if not isinstance(source_contract, Mapping):
        raise _fail("EXECUTION_INTEGRATION_SOURCE_STATE_INVALID", "source Stage contract is missing")
    try:
        source_contract = validate_stage_contract_v1(source_contract)
    except ContractValidationError as exc:
        raise _fail("EXECUTION_INTEGRATION_SOURCE_STATE_INVALID", "source Stage contract is invalid", exc) from exc

    inputs, input_digests, input_sizes = _input_descriptors(root, source_stage_id, target)
    verification = _verification_spec(source_stage, source_result, verification_commands)

    allowed = _normalise_paths(allowed_paths, "allowed_paths")
    if not allowed:
        # The delivery target and its receipt are the two files this Stage
        # may write.  Keep the default narrow instead of opening the whole
        # implementation tree.
        allowed = [target, delivery_artifact]
    if not any(_scope_matches(target, scope) for scope in allowed):
        raise _fail("EXECUTION_INTEGRATION_TARGET_OUTSIDE_SCOPE", "integration_target is outside allowed_paths")
    if not any(_scope_matches(delivery_artifact, scope) for scope in allowed):
        raise _fail("EXECUTION_INTEGRATION_ARTIFACT_OUTSIDE_SCOPE", "delivery_artifact_path is outside allowed_paths")
    protected = list(dict.fromkeys([*_CORE_PROTECTED_PATHS, *(_normalise_paths(protected_paths, "protected_paths"))]))
    protected.extend(
        [
            f".research/stages/{source_stage_id}/contract.json",
            f".research/stages/{stage_id}/contract.json",
            f".research/stages/{stage_id}/STAGE_PLAN.json",
        ]
    )
    protected = list(dict.fromkeys(protected))
    if any(_scope_matches(target, item) for item in protected):
        raise _fail("EXECUTION_INTEGRATION_TARGET_PROTECTED", "integration_target is protected by the delivery contract")

    checks = _normalise_paths(required_checks, "required_checks")
    if not checks:
        checks = ["integrated_result_identity", "post_integration_verification"]
    artifacts = _normalise_paths(review_artifact_requirements, "review_artifact_requirements")
    if not artifacts:
        artifacts = ["delivery_receipt", "post_integration_verification"]
    baseline_source = source_contract.get("baseline") if isinstance(source_contract.get("baseline"), Mapping) else {}
    baseline: dict[str, Any] = {
        "schema_version": "execution_integration_baseline.v1",
        "project_id": source_contract["project_id"],
        "requirement_digest": baseline_source.get("brief_digest"),
        "design_digest": baseline_source.get("design_digest"),
        "design_package_digest": baseline_source.get("design_package_digest"),
        "source_stage_id": source_stage_id,
        "source_stage_result_digest": result_digest,
        "source_stage_state_revision": controller.state.get("revision"),
        "integration_target": target,
        "target_before_digest": target_before_digest,
        "delivery_artifact_path": delivery_artifact,
        "source_paths": list(inputs),
        "source_digests": dict(input_digests),
        "source_sizes": dict(input_sizes),
    }
    _assert_safe(baseline, path="execution_integration_baseline")
    plan_id = "plan-execution-integration-" + sha256_json(
        {
            "project_id": source_contract["project_id"],
            "stage_id": stage_id,
            "source_stage_id": source_stage_id,
            "source_stage_result_digest": result_digest,
            "integration_target": target,
            "target_before_digest": target_before_digest,
            "input_digests": input_digests,
        }
    )[:20]
    contract: dict[str, Any] = {
        "schema_version": "stage_contract.v1",
        "plan_id": plan_id,
        "project_id": source_contract["project_id"],
        "repository_root": root.as_posix(),
        "stage_id": stage_id,
        "stage_name": stage_name,
        "project_goal": source_contract["project_goal"],
        "stage_goal": "Integrate the reviewed execution result into its authoritative target and verify the integrated result.",
        "user_visible_goal": "Deliver the reviewed execution result as an authoritative, verified project result.",
        "inputs": list(inputs),
        "protected_paths": protected,
        "allowed_paths": allowed,
        "acceptance_description": "The reviewed result identity matches the integrated result, the delivery receipt is complete, and post-integration verification passes.",
        "required_checks": checks,
        "review_artifact_requirements": artifacts,
        "baseline": baseline,
        "status": StageState.PLANNED.value,
        "lane_mode": "PRIMARY",
        "comparison_mode": "PRIMARY",
        "critic_mode": "NONE",
        "max_iterations": 1,
        "metadata": {
            "stage_kind": "execution_integration_delivery",
            "source_stage_id": source_stage_id,
            "source_stage_result_digest": result_digest,
            "source_stage_state_revision": controller.state.get("revision"),
            "integration_target": target,
            "integration_target_origins": list(target_info["origins"]),
            "delivery_artifact_path": delivery_artifact,
            "verification_spec": copy.deepcopy(verification),
            "planning_mode": "approved_stage_to_delivery_stage",
            "no_automatic_promotion": True,
        },
        "source_stage_id": source_stage_id,
        "source_stage_result_digest": result_digest,
        "integration_target": target,
        "delivery_artifact_path": delivery_artifact,
        "verification_spec": copy.deepcopy(verification),
    }
    if not checks or not artifacts:
        raise _fail("EXECUTION_INTEGRATION_CONTRACT_INVALID", "delivery checks and artifacts must be non-empty")
    try:
        contract = validate_stage_contract_v1(contract)
    except ContractValidationError as exc:
        raise _fail("EXECUTION_INTEGRATION_CONTRACT_INVALID", "delivery Stage contract failed validation", exc) from exc

    if contract_path.exists() or contract_path.is_symlink():
        existing_contract = _read_json(contract_path, "delivery Stage contract")
        if canonical_json(existing_contract) != canonical_json(contract):
            raise _fail("EXECUTION_INTEGRATION_CONTRACT_STALE", "existing delivery contract differs from current evidence")
    else:
        _atomic_json(contract_path, contract)

    try:
        registered = controller.prepare_stage(contract)
    except (StageControllerError, ContractValidationError) as exc:
        raise _fail("EXECUTION_INTEGRATION_REGISTRATION_REJECTED", "StageController rejected the delivery contract", exc) from exc
    stage = registered.get("stage") if isinstance(registered, Mapping) else None
    if not isinstance(stage, Mapping) or stage.get("status") != StageState.PLANNED.value:
        raise _fail("EXECUTION_INTEGRATION_REGISTRATION_INVALID", "delivery Stage registration did not result in PLANNED")
    plan = _plan_payload(
        root=root,
        contract=contract,
        source_stage_id=source_stage_id,
        source_stage_result_digest=result_digest,
        source_state_revision=int(controller.state["revision"]),
        target=target,
        target_before_digest=target_before_digest,
        verification=verification,
        input_paths=inputs,
        input_digests=input_digests,
        input_sizes=input_sizes,
        stage_view=stage,
        reused=False,
    )
    _atomic_json(plan_path, plan)
    return plan


def load_execution_integration_stage_plan(project_root: str | os.PathLike[str] = ".") -> dict[str, Any]:
    return _load_plan(_resolve_root(project_root))


def verify_execution_integration_stage_plan(project_root: str | os.PathLike[str] = ".") -> dict[str, Any]:
    """Reload the plan and prove source/result identity through the controller."""

    root = _resolve_root(project_root)
    plan = _load_plan(root)
    state_path = root / EXECUTION_INTEGRATION_STAGE_STATE_RELATIVE_PATH
    try:
        controller = StageController.from_state(state_path)
        stage = controller.show_stage(str(plan["stage_id"]))
        source = controller.show_stage(str(plan["source_stage_id"]))
    except (StageControllerError, ContractValidationError, OSError) as exc:
        raise _fail("EXECUTION_INTEGRATION_STATE_INVALID", "delivery Stage state could not be reloaded", exc) from exc
    if source.get("status") != StageState.APPROVED.value:
        raise _fail("EXECUTION_INTEGRATION_SOURCE_NOT_CLOSED", "source Stage is no longer APPROVED")
    binding = source.get("stage_result_binding")
    if not isinstance(binding, Mapping) or binding.get("result_digest") != plan.get("source_stage_result_digest"):
        raise _fail("EXECUTION_INTEGRATION_SOURCE_RESULT_MISMATCH", "source result binding no longer matches the delivery plan")
    contract_path = _repo_path(root, str(plan["stage_contract_path"]), "stage_contract_path", require_file=True)
    contract = _read_json(contract_path, "delivery Stage contract")
    if sha256_json(contract) != plan.get("contract_digest"):
        raise _fail("EXECUTION_INTEGRATION_CONTRACT_STALE", "delivery contract digest no longer matches the plan")
    if stage.get("contract_digest") != plan.get("contract_digest"):
        raise _fail("EXECUTION_INTEGRATION_STATE_INVALID", "controller Stage contract differs from the delivery plan")
    return {
        "passed": True,
        "marker": EXECUTION_INTEGRATION_PLANNING_MARKER,
        "stage_id": plan["stage_id"],
        "stage_status": stage["status"],
        "source_stage_id": plan["source_stage_id"],
        "source_stage_status": source["status"],
        "source_stage_result_digest": plan["source_stage_result_digest"],
        "integration_target": plan["integration_target"],
        "contract_digest": plan["contract_digest"],
        "plan_id": plan["plan_id"],
        "state_revision": controller.state["revision"],
    }


def start_execution_integration_stage(
    project_root: str | os.PathLike[str] = ".",
    *,
    actor: str = "local-workflow",
    rationale: str = "start the approved Execution Integration Delivery V1 Stage",
) -> dict[str, Any]:
    """Start the separately registered delivery Stage through StageController."""

    root = _resolve_root(project_root)
    plan = _load_plan(root)
    state_path = root / EXECUTION_INTEGRATION_STAGE_STATE_RELATIVE_PATH
    try:
        controller = StageController.from_state(state_path)
        result = controller.start_stage(str(plan["stage_id"]), actor=actor, rationale=rationale)
    except (StageControllerError, ContractValidationError) as exc:
        raise _fail("EXECUTION_INTEGRATION_START_REJECTED", "StageController rejected delivery Stage start", exc) from exc
    return {"plan": plan, "event": result.get("event"), "stage": result.get("stage")}


# Friendly aliases for callers that name the Stage after its delivery role.
plan_delivery_stage = plan_execution_integration_stage
plan_execution_integration_delivery = plan_execution_integration_stage
prepare_execution_integration_stage = plan_execution_integration_stage
start_delivery_stage = start_execution_integration_stage
start_execution_integration_delivery = start_execution_integration_stage


__all__ = [
    "EXECUTION_INTEGRATION_NEXT_ACTION",
    "EXECUTION_INTEGRATION_PLANNING_MARKER",
    "EXECUTION_INTEGRATION_PLANNING_SCHEMA_VERSION",
    "EXECUTION_INTEGRATION_STAGE_CONTRACT_FILENAME",
    "EXECUTION_INTEGRATION_STAGE_ID",
    "EXECUTION_INTEGRATION_STAGE_NAME",
    "EXECUTION_INTEGRATION_STAGE_PLAN_RELATIVE_PATH",
    "EXECUTION_INTEGRATION_STAGE_STATE_RELATIVE_PATH",
    "ExecutionIntegrationPlanningError",
    "load_execution_integration_stage_plan",
    "plan_delivery_stage",
    "plan_execution_integration_delivery",
    "plan_execution_integration_stage",
    "prepare_execution_integration_stage",
    "resolve_integration_target",
    "start_delivery_stage",
    "start_execution_integration_delivery",
    "start_execution_integration_stage",
    "verify_execution_integration_stage_plan",
]
