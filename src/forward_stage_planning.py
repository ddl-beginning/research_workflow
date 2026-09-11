"""Forward planning for a separately scoped Stage.

This module is the small seam used when a completed Stage hands work to a
new Stage.  It deliberately keeps authority in the existing
``StageController`` and accepts a caller supplied ``stage_contract.v1``.  The
module only adds a digest bound plan envelope in the new Stage directory; it
does not introduce another lifecycle, registry, or worker loop.

The planner is intentionally strict about provenance.  A source Stage must
already be ``APPROVED`` and its current result binding must be complete.  A
Human ACCEPT document and an explicit digest for that document are required;
text which merely happens to contain the word ``ACCEPT`` is never treated as
authorization.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .contracts import (
    ContractValidationError,
    _ensure_relative_path,
    canonical_json,
    sha256_json,
)
from .project_state import ProjectStateError, resolve_project_root
from .stage_controller import (
    StageController,
    StageControllerError,
    StageState,
    validate_stage_contract_v1,
)


FORWARD_STAGE_PLANNING_SCHEMA_VERSION = "forward_stage_planning.v1"
FORWARD_STAGE_PLANNING_MARKER = "FORWARD_STAGE_PLANNING_PASS"
FORWARD_STAGE_PLANNING_STATUS = "FORWARD_STAGE_PLANNING_COMPLETE"
FORWARD_STAGE_NEXT_ACTION = "USER_START_STAGE"
UNIFIED_WORKFLOW_ENTRY_STAGE_ID = "unified-workflow-entry-v1"
UNIFIED_WORKFLOW_ENTRY_STAGE_NAME = "Unified Workflow Entry V1"
DEFAULT_FORWARD_SOURCE_STAGE_ID = "workflow-contract-integration-v1"
FORWARD_STAGE_STATE_RELATIVE_PATH = Path(".research") / "stage-state.json"
FORWARD_STAGE_CONTRACT_FILENAME = "contract.json"
FORWARD_STAGE_PLAN_FILENAME = "STAGE_PLAN.json"
HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH = (
    Path(".research") / "design-review" / "HUMAN_ACCEPT_RECEIPT.json"
)
MAX_FORWARD_STAGE_ARTIFACT_BYTES = 512 * 1024
_STAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class ForwardStagePlanningError(RuntimeError):
    """A stable, fail closed error raised by the forward planner."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


# A few callers use the product name rather than the implementation name.
UnifiedWorkflowEntryPlanningError = ForwardStagePlanningError
UnifiedWorkflowEntryStagePlanningError = ForwardStagePlanningError


def _fail(code: str, message: str, exc: BaseException | None = None) -> ForwardStagePlanningError:
    error = ForwardStagePlanningError(code, message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _text(value: Any, field: str, *, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum or "\x00" in value:
        raise _fail("FORWARD_STAGE_INPUT_INVALID", f"{field} must be bounded non-empty text")
    return value.strip()


def _digest(value: Any, field: str) -> str:
    candidate = _text(value, field, maximum=64)
    if _HEX64_RE.fullmatch(candidate) is None:
        raise _fail("FORWARD_STAGE_DIGEST_INVALID", f"{field} must be a lowercase SHA-256 digest")
    return candidate


def _resolve_root(value: str | os.PathLike[str]) -> Path:
    try:
        return resolve_project_root(value)
    except ProjectStateError as exc:
        raise _fail("FORWARD_STAGE_PROJECT_ROOT_INVALID", "project root could not be resolved", exc) from exc


def _relative(value: Any, field: str) -> str:
    try:
        return _ensure_relative_path(_text(value, field, maximum=1024), field)
    except ContractValidationError as exc:
        raise _fail("FORWARD_STAGE_PATH_INVALID", f"{field} must be a safe relative path", exc) from exc


def _repo_path(root: Path, value: Any, field: str, *, require_file: bool = False) -> Path:
    relative = _relative(value, field)
    candidate = root.joinpath(*relative.split("/"))
    if candidate.is_symlink():
        raise _fail("FORWARD_STAGE_PATH_INVALID", f"{field} must not be a symlink")
    try:
        resolved = candidate.resolve(strict=require_file)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _fail("FORWARD_STAGE_PATH_INVALID", f"{field} escaped the project root", exc) from exc
    if require_file and not candidate.is_file():
        raise _fail("FORWARD_STAGE_PATH_INVALID", f"{field} must name a regular file")
    return candidate


def _read_json(path: Path, field: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise _fail("FORWARD_STAGE_ARTIFACT_NOT_FOUND", f"{field} is not a regular file")
    try:
        if path.stat().st_size > MAX_FORWARD_STAGE_ARTIFACT_BYTES:
            raise _fail("FORWARD_STAGE_ARTIFACT_TOO_LARGE", f"{field} exceeds its bounded size")
        value = json.loads(path.read_text(encoding="utf-8"))
    except ForwardStagePlanningError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("FORWARD_STAGE_ARTIFACT_INVALID", f"{field} cannot be read", exc) from exc
    if not isinstance(value, dict):
        raise _fail("FORWARD_STAGE_ARTIFACT_INVALID", f"{field} must contain an object")
    return value


def _file_digest(path: Path, field: str) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise _fail("FORWARD_STAGE_ARTIFACT_NOT_FOUND", f"{field} is not a regular file")
    try:
        size = path.stat().st_size
        if size > MAX_FORWARD_STAGE_ARTIFACT_BYTES:
            raise _fail("FORWARD_STAGE_ARTIFACT_TOO_LARGE", f"{field} exceeds its bounded size")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except ForwardStagePlanningError:
        raise
    except OSError as exc:
        raise _fail("FORWARD_STAGE_ARTIFACT_UNREADABLE", f"{field} cannot be read", exc) from exc
    return digest.hexdigest(), size


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > MAX_FORWARD_STAGE_ARTIFACT_BYTES:
        raise _fail("FORWARD_STAGE_ARTIFACT_TOO_LARGE", f"{path.name} exceeds its bounded size")
    if path.exists() and (path.is_symlink() or path.is_dir()):
        raise _fail("FORWARD_STAGE_WRITE_FAILED", f"{path.name} is not a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise _fail("FORWARD_STAGE_WRITE_FAILED", f"could not write {path.name}", exc) from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _safe_metadata(value: Any, *, depth: int = 0) -> Any:
    """Detach bounded contract metadata without accepting transport material."""

    if depth > 12:
        raise _fail("FORWARD_STAGE_CONTRACT_INVALID", "contract metadata is too deeply nested")
    sensitive = {
        "api_key", "access_token", "authorization", "cookie", "cookies", "dom",
        "password", "prompt", "raw_dom", "raw_response", "secret", "session",
        "session_storage", "storage_state", "token", "tokens", "transcript",
    }
    if isinstance(value, Mapping):
        return {
            str(key): _safe_metadata(child, depth=depth + 1)
            for key, child in value.items()
            if str(key).strip().casefold().replace("-", "_") not in sensitive
        }
    if isinstance(value, list):
        if len(value) > 512:
            raise _fail("FORWARD_STAGE_CONTRACT_INVALID", "contract metadata list is too large")
        return [_safe_metadata(child, depth=depth + 1) for child in value]
    if isinstance(value, tuple):
        return [_safe_metadata(child, depth=depth + 1) for child in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, str) and ("\x00" in value or len(value) > 32_000):
            raise _fail("FORWARD_STAGE_CONTRACT_INVALID", "contract metadata text is outside the bounded range")
        return copy.deepcopy(value)
    raise _fail("FORWARD_STAGE_CONTRACT_INVALID", "contract contains unsupported metadata")


def _load_acceptance_source(
    root: Path,
    *,
    acceptance_source_path: str | os.PathLike[str],
    acceptance_source_hash: str,
    project_id: str,
    baseline: Mapping[str, Any],
) -> dict[str, Any]:
    """Verify an explicit Human ACCEPT source and bind it by hash.

    The raw file digest is always recorded.  For the canonical JSON receipt,
    its self-binding ``receipt_digest`` is also accepted as the explicit hash
    so formatting changes do not turn a valid decision into a new decision.
    """

    supplied_hash = _digest(acceptance_source_hash, "acceptance_source_hash")
    if isinstance(acceptance_source_path, os.PathLike):
        acceptance_source_path = os.fspath(acceptance_source_path)
    relative = _relative(acceptance_source_path, "acceptance_source_path")
    path = _repo_path(root, relative, "acceptance_source_path", require_file=True)
    raw_hash, size = _file_digest(path, "acceptance source")
    logical_hash: str | None = None
    source_kind = "file"
    if path.suffix.casefold() == ".json":
        receipt = _read_json(path, "Human ACCEPT source")
        if receipt.get("decision") != "ACCEPT":
            raise _fail("FORWARD_STAGE_ACCEPTANCE_INVALID", "Human ACCEPT source does not record decision=ACCEPT")
        if receipt.get("project_id") is not None and receipt.get("project_id") != project_id:
            raise _fail("FORWARD_STAGE_ACCEPTANCE_BINDING_MISMATCH", "Human ACCEPT source project identity differs")
        recorded = receipt.get("receipt_digest")
        if isinstance(recorded, str) and _HEX64_RE.fullmatch(recorded) is not None:
            detached = copy.deepcopy(receipt)
            detached.pop("receipt_digest", None)
            expected = sha256_json(detached)
            if recorded != expected:
                raise _fail("FORWARD_STAGE_ACCEPTANCE_INVALID", "Human ACCEPT source receipt digest is inconsistent")
            logical_hash = recorded
        # If these fields are present, bind them to the frozen contract
        # baseline.  Their absence is tolerated for older receipt envelopes;
        # the explicit file/hash pair remains mandatory.
        requirement_digest = baseline.get("requirement_digest", baseline.get("brief_digest"))
        design_digest = baseline.get("design_digest")
        if receipt.get("brief_digest") is not None and requirement_digest is not None and receipt.get("brief_digest") != requirement_digest:
            raise _fail("FORWARD_STAGE_ACCEPTANCE_BINDING_MISMATCH", "Human ACCEPT source requirement digest is stale")
        if receipt.get("design_digest") is not None and design_digest is not None and receipt.get("design_digest") != design_digest:
            raise _fail("FORWARD_STAGE_ACCEPTANCE_BINDING_MISMATCH", "Human ACCEPT source design digest is stale")
    if supplied_hash != raw_hash and supplied_hash != logical_hash:
        raise _fail("FORWARD_STAGE_ACCEPTANCE_DIGEST_MISMATCH", "acceptance_source_hash does not match the source document")
    if supplied_hash == logical_hash:
        source_kind = "receipt"
    return {
        "path": relative,
        "hash": supplied_hash,
        "raw_file_digest": raw_hash,
        "logical_receipt_digest": logical_hash,
        "hash_kind": source_kind,
        "size": size,
    }


def _source_provenance(
    controller: StageController,
    *,
    source_stage_id: str,
    stage_contract: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        source_stage = controller.show_stage(source_stage_id)
    except (StageControllerError, ContractValidationError) as exc:
        raise _fail("FORWARD_STAGE_SOURCE_STATE_INVALID", "source Stage could not be loaded", exc) from exc
    if source_stage.get("status") != StageState.APPROVED.value:
        raise _fail(
            "FORWARD_STAGE_SOURCE_NOT_CLOSED",
            f"source Stage {source_stage_id} must be APPROVED before forward planning",
        )
    binding = source_stage.get("stage_result_binding")
    if not isinstance(binding, Mapping) or binding.get("evidence_complete") is not True:
        raise _fail("FORWARD_STAGE_SOURCE_EVIDENCE_INCOMPLETE", "source Stage has no complete result binding")
    if binding.get("stage_id") not in (None, source_stage_id):
        raise _fail("FORWARD_STAGE_SOURCE_IDENTITY_MISMATCH", "source result binding names another Stage")
    result_digest = binding.get("result_digest")
    result_digest = _digest(result_digest, "source_stage_result_digest")
    source_contract = source_stage.get("contract")
    if not isinstance(source_contract, Mapping):
        raise _fail("FORWARD_STAGE_SOURCE_CONTRACT_INVALID", "source Stage has no contract")
    try:
        checked_source = validate_stage_contract_v1(source_contract)
    except ContractValidationError as exc:
        raise _fail("FORWARD_STAGE_SOURCE_CONTRACT_INVALID", "source Stage contract is invalid", exc) from exc
    source_contract_digest = sha256_json(checked_source)
    target_contract = stage_contract
    if target_contract.get("project_id") != checked_source.get("project_id"):
        raise _fail("FORWARD_STAGE_PROJECT_IDENTITY_MISMATCH", "forward contract project_id differs from source Stage")
    for field, expected in (("source_stage_id", source_stage_id), ("source_stage_result_digest", result_digest)):
        observed = target_contract.get(field)
        if observed is not None and observed != expected:
            raise _fail("FORWARD_STAGE_PROVENANCE_MISMATCH", f"forward contract {field} differs from source evidence")
    baseline = target_contract.get("baseline")
    if isinstance(baseline, Mapping):
        for field, expected in (("source_stage_id", source_stage_id), ("source_stage_result_digest", result_digest), ("source_stage_status", StageState.APPROVED.value)):
            observed = baseline.get(field)
            if observed is not None and observed != expected:
                raise _fail("FORWARD_STAGE_PROVENANCE_MISMATCH", f"forward baseline {field} differs from source evidence")
    metadata = target_contract.get("metadata")
    if isinstance(metadata, Mapping):
        for field, expected in (("source_stage_id", source_stage_id), ("source_stage_result_digest", result_digest)):
            observed = metadata.get(field)
            if observed is not None and observed != expected:
                raise _fail("FORWARD_STAGE_PROVENANCE_MISMATCH", f"forward metadata {field} differs from source evidence")
    return {
        "source_stage_id": source_stage_id,
        "source_stage_status": StageState.APPROVED.value,
        "source_stage_result_digest": result_digest,
        "source_stage_contract_digest": source_contract_digest,
        "source_stage_state_revision": controller.state.get("revision"),
        "source_stage": source_stage,
    }


def _paths_for_stage(stage_id: str) -> tuple[str, str]:
    if _STAGE_ID_RE.fullmatch(stage_id) is None:
        raise _fail("FORWARD_STAGE_ID_INVALID", "stage_id contains unsafe characters")
    base = f".research/stages/{stage_id}"
    return f"{base}/{FORWARD_STAGE_PLAN_FILENAME}", f"{base}/{FORWARD_STAGE_CONTRACT_FILENAME}"


def _load_plan(root: Path, plan_relative: str) -> dict[str, Any]:
    return _read_json(_repo_path(root, plan_relative, "stage_plan_path", require_file=True), "forward Stage plan")


def plan_forward_stage(
    project_root: str | os.PathLike[str] = ".",
    *,
    stage_contract: Mapping[str, Any] | None = None,
    acceptance_source_hash: str | None = None,
    acceptance_source_path: str | os.PathLike[str] = HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix(),
    source_stage_id: str = DEFAULT_FORWARD_SOURCE_STAGE_ID,
    contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Register one caller-authored forward Stage through StageController.

    ``stage_contract`` and ``acceptance_source_hash`` are deliberately
    explicit.  The planner never constructs a contract from a prose request
    and never treats a path, cwd, Git root, or an unbound ACCEPT string as
    authority.
    """

    root = _resolve_root(project_root)
    if stage_contract is None:
        stage_contract = contract
    if not isinstance(stage_contract, Mapping):
        raise _fail("FORWARD_STAGE_CONTRACT_REQUIRED", "a caller-supplied stage_contract.v1 is required")
    if acceptance_source_hash is None:
        raise _fail("FORWARD_STAGE_ACCEPTANCE_REQUIRED", "an explicit acceptance_source_hash is required")
    source_id = _text(source_stage_id, "source_stage_id", maximum=80)
    try:
        checked = validate_stage_contract_v1(stage_contract)
    except ContractValidationError as exc:
        raise _fail("FORWARD_STAGE_CONTRACT_INVALID", "stage_contract.v1 validation failed", exc) from exc
    checked = _safe_metadata(checked)
    stage_id = _text(checked.get("stage_id"), "stage_id", maximum=80)
    if stage_id == source_id:
        raise _fail("FORWARD_STAGE_ID_INVALID", "forward Stage must have a distinct source Stage identity")
    try:
        repository_root = Path(str(checked.get("repository_root"))).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _fail("FORWARD_STAGE_ROOT_INVALID", "stage contract repository_root cannot be resolved", exc) from exc
    if repository_root != root:
        raise _fail("FORWARD_STAGE_ROOT_INVALID", "stage contract repository_root differs from project root")

    state_path = _repo_path(root, FORWARD_STAGE_STATE_RELATIVE_PATH.as_posix(), "stage_state_path", require_file=True)
    try:
        controller = StageController.from_state(state_path)
    except (StageControllerError, ContractValidationError, OSError) as exc:
        raise _fail("FORWARD_STAGE_STATE_INVALID", "current StageController state could not be loaded", exc) from exc
    provenance = _source_provenance(controller, source_stage_id=source_id, stage_contract=checked)
    baseline = checked.get("baseline") if isinstance(checked.get("baseline"), Mapping) else {}
    acceptance = _load_acceptance_source(
        root,
        acceptance_source_path=acceptance_source_path,
        acceptance_source_hash=acceptance_source_hash,
        project_id=str(checked["project_id"]),
        baseline=baseline,
    )
    contract_digest = sha256_json(checked)
    stage_plan_path, stage_contract_path = _paths_for_stage(stage_id)
    plan_path = _repo_path(root, stage_plan_path, "stage_plan_path")
    contract_path = _repo_path(root, stage_contract_path, "stage_contract_path")

    # Existing artifacts are immutable evidence.  A re-run may reuse the
    # exact same plan, but it may never silently replace a different plan or
    # contract.
    existing_plan: dict[str, Any] | None = None
    if plan_path.exists() or plan_path.is_symlink():
        existing_plan = _load_plan(root, stage_plan_path)
        if existing_plan.get("stage_id") != stage_id or existing_plan.get("source_stage_id") != source_id:
            raise _fail("FORWARD_STAGE_PLAN_IDENTITY_MISMATCH", "existing forward plan belongs to another Stage")
        if existing_plan.get("contract_digest") != contract_digest:
            raise _fail("FORWARD_STAGE_PLAN_STALE", "existing forward plan has a different contract")
        if existing_plan.get("acceptance_source_hash") != acceptance["hash"] or existing_plan.get("acceptance_source_path") != acceptance["path"]:
            raise _fail("FORWARD_STAGE_PLAN_STALE", "existing forward plan has a different acceptance source")
    if contract_path.exists() or contract_path.is_symlink():
        existing_contract = _read_json(contract_path, "forward Stage contract")
        try:
            checked_existing = validate_stage_contract_v1(existing_contract)
        except ContractValidationError as exc:
            raise _fail("FORWARD_STAGE_CONTRACT_STALE", "existing forward contract is invalid", exc) from exc
        if canonical_json(checked_existing) != canonical_json(checked):
            raise _fail("FORWARD_STAGE_CONTRACT_STALE", "existing forward contract differs from caller contract")

    try:
        stage = controller.show_stage(stage_id)
    except StageControllerError:
        stage = None
    if stage is not None:
        if stage.get("contract_digest") != contract_digest:
            raise _fail("FORWARD_STAGE_STATE_INVALID", "existing Stage contract differs from caller contract")
        if stage.get("status") not in {StageState.PLANNED.value, StageState.ACTIVE.value}:
            raise _fail("FORWARD_STAGE_STATE_INVALID", "existing forward Stage is no longer re-plannable")
        if existing_plan is None:
            raise _fail("FORWARD_STAGE_PLAN_MISSING", "existing controller Stage has no forward plan")
        reused = True
    else:
        reused = False
        try:
            registered = controller.prepare_stage(checked)
        except (StageControllerError, ContractValidationError) as exc:
            raise _fail("FORWARD_STAGE_REGISTRATION_REJECTED", "StageController rejected the forward contract", exc) from exc
        stage = registered.get("stage") if isinstance(registered, Mapping) else None
        if not isinstance(stage, Mapping) or stage.get("status") != StageState.PLANNED.value:
            raise _fail("FORWARD_STAGE_REGISTRATION_INVALID", "forward Stage registration did not result in PLANNED")
        if not contract_path.exists():
            _atomic_json(contract_path, checked)

    assert isinstance(stage, Mapping)
    if existing_plan is not None:
        result = copy.deepcopy(existing_plan)
        result["idempotent_reuse"] = True
        result["stage"] = copy.deepcopy(dict(stage))
        result["stage_status"] = stage.get("status")
        result["stage_started"] = stage.get("status") != StageState.PLANNED.value
        return result

    plan_id = checked.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        plan_id = "plan-forward-" + contract_digest[:20]
    plan: dict[str, Any] = {
        "schema_version": FORWARD_STAGE_PLANNING_SCHEMA_VERSION,
        "marker": FORWARD_STAGE_PLANNING_MARKER,
        "planning_status": FORWARD_STAGE_PLANNING_STATUS,
        "status": StageState.PLANNED.value,
        "stage_status": stage.get("status"),
        "next_action": FORWARD_STAGE_NEXT_ACTION,
        "stage_created": True,
        "stage_started": stage.get("status") != StageState.PLANNED.value,
        "project_id": checked["project_id"],
        "repository_root": root.as_posix(),
        "plan_id": plan_id,
        "stage_id": stage_id,
        "stage_name": checked["stage_name"],
        "stage_plan_path": stage_plan_path,
        "stage_contract_path": stage_contract_path,
        "stage_state_path": FORWARD_STAGE_STATE_RELATIVE_PATH.as_posix(),
        "source_stage_id": provenance["source_stage_id"],
        "source_stage_status": provenance["source_stage_status"],
        "source_stage_result_digest": provenance["source_stage_result_digest"],
        "source_stage_contract_digest": provenance["source_stage_contract_digest"],
        "source_stage_state_revision": provenance["source_stage_state_revision"],
        "acceptance_source_path": acceptance["path"],
        "acceptance_source_hash": acceptance["hash"],
        "acceptance_source_hash_kind": acceptance["hash_kind"],
        "acceptance_source_file_digest": acceptance["raw_file_digest"],
        "contract_digest": contract_digest,
        "baseline_digest": sha256_json(checked["baseline"]),
        "idempotent_reuse": reused,
        "planning_mode": "caller_supplied_contract_forward_stage",
        "authority": {
            "existing_stage_controller": True,
            "identity_before_path": True,
            "resolve_before_create": True,
            "derive_dont_duplicate": True,
            "no_second_state_machine": True,
            "no_second_registry": True,
            "one_shot_dispatch": True,
        },
        "stage": copy.deepcopy(dict(stage)),
    }
    _atomic_json(plan_path, plan)
    return plan


def load_forward_stage_plan(project_root: str | os.PathLike[str] = ".", *, stage_id: str = UNIFIED_WORKFLOW_ENTRY_STAGE_ID) -> dict[str, Any]:
    root = _resolve_root(project_root)
    stage_plan_path, _ = _paths_for_stage(stage_id)
    return _load_plan(root, stage_plan_path)


def verify_forward_stage_plan(project_root: str | os.PathLike[str] = ".", *, stage_id: str = UNIFIED_WORKFLOW_ENTRY_STAGE_ID) -> dict[str, Any]:
    """Reload plan, contract, source Stage and acceptance evidence together."""

    root = _resolve_root(project_root)
    plan = load_forward_stage_plan(root, stage_id=stage_id)
    state_path = _repo_path(root, plan.get("stage_state_path"), "stage_state_path", require_file=True)
    try:
        controller = StageController.from_state(state_path)
        stage = controller.show_stage(str(plan["stage_id"]))
        source = controller.show_stage(str(plan["source_stage_id"]))
    except (StageControllerError, ContractValidationError, OSError) as exc:
        raise _fail("FORWARD_STAGE_STATE_INVALID", "forward Stage state could not be reloaded", exc) from exc
    if source.get("status") != StageState.APPROVED.value:
        raise _fail("FORWARD_STAGE_SOURCE_NOT_CLOSED", "source Stage is no longer APPROVED")
    contract_path = _repo_path(root, plan.get("stage_contract_path"), "stage_contract_path", require_file=True)
    contract = _read_json(contract_path, "forward Stage contract")
    try:
        checked = validate_stage_contract_v1(contract)
    except ContractValidationError as exc:
        raise _fail("FORWARD_STAGE_CONTRACT_INVALID", "forward Stage contract is invalid", exc) from exc
    if sha256_json(checked) != plan.get("contract_digest") or stage.get("contract_digest") != plan.get("contract_digest"):
        raise _fail("FORWARD_STAGE_CONTRACT_STALE", "forward Stage contract digest no longer matches plan/state")
    acceptance = _load_acceptance_source(
        root,
        acceptance_source_path=plan.get("acceptance_source_path"),
        acceptance_source_hash=plan.get("acceptance_source_hash"),
        project_id=str(checked["project_id"]),
        baseline=checked.get("baseline") if isinstance(checked.get("baseline"), Mapping) else {},
    )
    if acceptance["raw_file_digest"] != plan.get("acceptance_source_file_digest"):
        raise _fail("FORWARD_STAGE_ACCEPTANCE_STALE", "acceptance source changed after planning")
    binding = source.get("stage_result_binding")
    if not isinstance(binding, Mapping) or binding.get("result_digest") != plan.get("source_stage_result_digest"):
        raise _fail("FORWARD_STAGE_SOURCE_RESULT_MISMATCH", "source result binding no longer matches plan")
    return {
        "passed": True,
        "marker": FORWARD_STAGE_PLANNING_MARKER,
        "schema_version": FORWARD_STAGE_PLANNING_SCHEMA_VERSION,
        "stage_id": plan["stage_id"],
        "stage_status": stage.get("status"),
        "source_stage_id": plan["source_stage_id"],
        "source_stage_status": source.get("status"),
        "source_stage_result_digest": plan["source_stage_result_digest"],
        "acceptance_source_path": plan["acceptance_source_path"],
        "acceptance_source_hash": plan["acceptance_source_hash"],
        "contract_digest": plan["contract_digest"],
        "plan_id": plan["plan_id"],
        "state_revision": controller.state.get("revision"),
    }


def start_forward_stage(
    project_root: str | os.PathLike[str] = ".",
    *,
    stage_id: str = UNIFIED_WORKFLOW_ENTRY_STAGE_ID,
    actor: str = "local-workflow",
    rationale: str = "start the planned Unified Workflow Entry V1 Stage",
) -> dict[str, Any]:
    """Start the planned Stage through the existing StageController."""

    root = _resolve_root(project_root)
    plan = load_forward_stage_plan(root, stage_id=stage_id)
    verify_forward_stage_plan(root, stage_id=stage_id)
    state_path = _repo_path(root, plan.get("stage_state_path"), "stage_state_path", require_file=True)
    try:
        controller = StageController.from_state(state_path)
        result = controller.start_stage(str(plan["stage_id"]), actor=actor, rationale=rationale)
    except (StageControllerError, ContractValidationError) as exc:
        raise _fail("FORWARD_STAGE_START_REJECTED", "StageController rejected forward Stage start", exc) from exc
    return {"plan": plan, "event": result.get("event"), "stage": result.get("stage")}


# Friendly aliases.  They all target the same implementation and therefore
# do not create another planning or lifecycle surface.
def plan_unified_workflow_entry_stage(project_root=".", **kwargs):
    contract = kwargs.get("stage_contract", kwargs.get("contract", {}))
    if not contract:
        raise _fail("FORWARD_STAGE_CONTRACT_REQUIRED", "a caller-supplied stage contract is required")
    if contract.get("stage_id") != UNIFIED_WORKFLOW_ENTRY_STAGE_ID:
        raise _fail("FORWARD_STAGE_ID_INVALID", "Unified Entry wrapper requires its canonical Stage id")
    if contract.get("stage_name") != UNIFIED_WORKFLOW_ENTRY_STAGE_NAME:
        raise _fail("FORWARD_STAGE_NAME_INVALID", "Unified Entry wrapper requires its canonical name")
    return plan_forward_stage(project_root, **kwargs)

prepare_forward_stage = plan_forward_stage
prepare_unified_workflow_entry_stage = plan_unified_workflow_entry_stage
start_unified_workflow_entry_stage = start_forward_stage
verify_unified_workflow_entry_stage_plan = verify_forward_stage_plan
load_unified_workflow_entry_stage_plan = load_forward_stage_plan


__all__ = [
    "DEFAULT_FORWARD_SOURCE_STAGE_ID",
    "FORWARD_STAGE_CONTRACT_FILENAME",
    "FORWARD_STAGE_NEXT_ACTION",
    "FORWARD_STAGE_PLANNING_MARKER",
    "FORWARD_STAGE_PLANNING_SCHEMA_VERSION",
    "FORWARD_STAGE_PLANNING_STATUS",
    "FORWARD_STAGE_PLAN_FILENAME",
    "FORWARD_STAGE_STATE_RELATIVE_PATH",
    "ForwardStagePlanningError",
    "HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH",
    "UNIFIED_WORKFLOW_ENTRY_STAGE_ID",
    "UNIFIED_WORKFLOW_ENTRY_STAGE_NAME",
    "UnifiedWorkflowEntryPlanningError",
    "UnifiedWorkflowEntryStagePlanningError",
    "load_forward_stage_plan",
    "load_unified_workflow_entry_stage_plan",
    "plan_forward_stage",
    "plan_unified_workflow_entry_stage",
    "prepare_forward_stage",
    "prepare_unified_workflow_entry_stage",
    "start_forward_stage",
    "start_unified_workflow_entry_stage",
    "verify_forward_stage_plan",
    "verify_unified_workflow_entry_stage_plan",
]
