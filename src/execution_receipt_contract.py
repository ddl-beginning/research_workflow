"""Shared contract for bounded provider execution receipts.

The provider returns structured execution facts.  The runner owns persistence
of the receipt so ignored temporary paths are not mistaken for a missing
provider change.  Both the native provider adapter and the V2 supervisor use
the resolver in this module; neither layer invents a second path authority.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractValidationError, _ensure_relative_path, path_is_allowed, sha256_json


EXECUTION_RECEIPT_SCHEMA_VERSION = "workflow_v2_stage_execution.v1"
EXECUTION_RECEIPT_OWNER = "RUNNER"
EXECUTION_RECEIPT_AUTHORITY = "PROJECT_ROOT"
_VALID_RECEIPT_STATUSES = {"PASS", "FAIL", "ERROR", "BLOCKED"}


def resolve_execution_receipt_path(
    workspace_root: str | Path,
    relative_path: str,
    allowed_paths: Sequence[str],
    protected_paths: Sequence[str] = (),
) -> Path:
    """Resolve one receipt path under the same workspace/scope authority."""

    root = Path(workspace_root).resolve()
    if not root.is_dir():
        raise ContractValidationError(f"workspace_root is not a directory: {root}")
    normalized = _ensure_relative_path(relative_path, "execution_receipt_path")
    if not path_is_allowed(
        normalized,
        allowed_paths,
        protected_paths=protected_paths,
        workspace_root=root,
    ):
        raise ContractValidationError(
            f"execution receipt path is outside allowed_paths/protected_paths: {normalized!r}"
        )
    candidate = (root / Path(*normalized.split("/"))).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractValidationError("execution receipt path escapes workspace_root") from exc
    return candidate


def build_execution_receipt_contract(
    *,
    workspace_root: str | Path,
    relative_path: str,
    allowed_paths: Sequence[str],
    protected_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Return the explicit receipt contract forwarded to the provider."""

    normalized = _ensure_relative_path(relative_path, "execution_receipt_path")
    absolute = resolve_execution_receipt_path(
        workspace_root,
        normalized,
        allowed_paths,
        protected_paths,
    )
    return {
        "owner": EXECUTION_RECEIPT_OWNER,
        "authority": EXECUTION_RECEIPT_AUTHORITY,
        "schema_version": EXECUTION_RECEIPT_SCHEMA_VERSION,
        "workspace_root": str(Path(workspace_root).resolve()),
        "allowed_paths": list(allowed_paths),
        "protected_paths": list(protected_paths),
        "relative_path": normalized,
        "absolute_path": str(absolute),
        "required_fields": [
            "schema_version",
            "owner",
            "status",
            "operation",
            "receipt_path",
            "result_digest",
        ],
    }


def _receipt_status(result_status: Any) -> str:
    return {
        "SUCCEEDED": "PASS",
        "PASS": "PASS",
        "FAILED": "FAIL",
        "FAIL": "FAIL",
        "ERROR": "ERROR",
        "BLOCKED": "BLOCKED",
    }.get(str(result_status).strip().upper(), "ERROR")


def build_runner_execution_receipt(
    *,
    contract: Mapping[str, Any],
    request: Any,
    result: Mapping[str, Any],
    provider_id: str,
    operation: str = "provider_execution",
    invocation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, bounded receipt from a validated result."""

    if not isinstance(result, Mapping):
        raise ContractValidationError("provider result must be an object")
    relative_path = _ensure_relative_path(str(contract.get("relative_path", "")), "execution_receipt_path")
    metadata = request.metadata if isinstance(getattr(request, "metadata", None), Mapping) else {}
    request_id = metadata.get("request_id") or getattr(request, "task_id", None)
    operation_id = metadata.get("operation_id") or metadata.get("parent_operation_id")
    provider = {"id": str(provider_id)[:128]}
    if isinstance(invocation, Mapping):
        for field in ("model", "request_id", "workdir", "prompt_digest"):
            value = invocation.get(field)
            if isinstance(value, str) and value.strip():
                provider[field] = value.strip()[:2000]
        allowed = invocation.get("allowed_paths")
        if isinstance(allowed, list) and all(isinstance(item, str) for item in allowed):
            provider["allowed_paths"] = list(allowed)[:256]
    provider.setdefault("workdir", str(contract.get("workspace_root", "")))
    provider.setdefault("allowed_paths", list(contract.get("allowed_paths", [])))
    payload: dict[str, Any] = {
        "schema_version": EXECUTION_RECEIPT_SCHEMA_VERSION,
        "owner": EXECUTION_RECEIPT_OWNER,
        "authority": EXECUTION_RECEIPT_AUTHORITY,
        "status": _receipt_status(result.get("status")),
        "operation": str(operation).strip()[:128] or "provider_execution",
        "stage_id": getattr(request, "stage_id", None),
        "task_id": getattr(request, "task_id", None),
        "request_id": request_id,
        "operation_id": operation_id,
        "attempt_index": getattr(request, "attempt_index", None),
        "iteration_index": getattr(request, "iteration_index", None),
        "baseline_digest": getattr(request, "baseline_digest", None),
        "receipt_path": relative_path,
        "receipt_absolute_path": str(contract.get("absolute_path", "")),
        "result_digest": sha256_json(dict(result)),
        "provider": provider,
        "provider_result": {
            "status": result.get("status"),
            "changed_files": list(result.get("changed_files", []))[:256]
            if isinstance(result.get("changed_files", []), list)
            else [],
            "tests": list(result.get("tests", []))[:256]
            if isinstance(result.get("tests", []), list)
            else [],
            "problems_discovered": list(result.get("problems_discovered", []))[:256]
            if isinstance(result.get("problems_discovered", []), list)
            else [],
        },
    }
    validate_execution_receipt(payload, contract=contract)
    return payload


def validate_execution_receipt(
    receipt: Mapping[str, Any],
    *,
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate identity, status, and path without rejecting bounded extras."""

    if not isinstance(receipt, Mapping):
        raise ContractValidationError("execution receipt must be an object")
    detached = dict(receipt)
    if detached.get("schema_version") != EXECUTION_RECEIPT_SCHEMA_VERSION:
        raise ContractValidationError("execution receipt schema_version is invalid")
    if detached.get("owner") != EXECUTION_RECEIPT_OWNER:
        raise ContractValidationError("execution receipt owner must be RUNNER")
    if detached.get("authority") != EXECUTION_RECEIPT_AUTHORITY:
        raise ContractValidationError("execution receipt authority must be PROJECT_ROOT")
    if detached.get("status") not in _VALID_RECEIPT_STATUSES:
        raise ContractValidationError("execution receipt status is invalid")
    operation = detached.get("operation")
    if not isinstance(operation, str) or not operation.strip():
        raise ContractValidationError("execution receipt operation must be non-empty")
    expected_path = _ensure_relative_path(str(contract.get("relative_path", "")), "execution_receipt_path")
    actual_path = _ensure_relative_path(str(detached.get("receipt_path", "")), "receipt_path")
    if actual_path != expected_path:
        raise ContractValidationError(
            f"execution receipt path mismatch: expected {expected_path!r}, got {actual_path!r}"
        )
    expected_absolute = Path(str(contract.get("absolute_path", ""))).resolve(strict=False)
    actual_absolute = Path(str(detached.get("receipt_absolute_path", ""))).resolve(strict=False)
    if actual_absolute != expected_absolute:
        raise ContractValidationError("execution receipt absolute path mismatch")
    digest = detached.get("result_digest")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
        raise ContractValidationError("execution receipt result_digest must be a sha256 digest")
    return detached


def persist_execution_receipt(contract: Mapping[str, Any], receipt: Mapping[str, Any]) -> str:
    """Atomically persist a validated runner-owned receipt in legal scope."""

    validate_execution_receipt(receipt, contract=contract)
    workspace_root = str(contract.get("workspace_root", ""))
    relative_path = str(contract.get("relative_path", ""))
    absolute = Path(str(contract.get("absolute_path", ""))).resolve(strict=False)
    expected = resolve_execution_receipt_path(
        workspace_root,
        relative_path,
        contract.get("allowed_paths", ()),
        contract.get("protected_paths", ()),
    )
    # The contract was resolved before the provider ran.  Requiring the same
    # absolute target here catches accidental path drift before writing.
    if expected != absolute:
        raise ContractValidationError("execution receipt absolute path drifted")
    absolute.parent.mkdir(parents=True, exist_ok=True)
    temporary = absolute.with_name(absolute.name + ".runner-tmp")
    try:
        temporary.write_text(json.dumps(dict(receipt), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, absolute)
    finally:
        if temporary.exists():
            temporary.unlink()
    return str(absolute)


__all__ = [
    "EXECUTION_RECEIPT_AUTHORITY",
    "EXECUTION_RECEIPT_OWNER",
    "EXECUTION_RECEIPT_SCHEMA_VERSION",
    "build_execution_receipt_contract",
    "build_runner_execution_receipt",
    "persist_execution_receipt",
    "resolve_execution_receipt_path",
    "validate_execution_receipt",
]
