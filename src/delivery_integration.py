"""Fail-closed delivery integration for a reviewed Stage result.

This module is deliberately a small seam around the existing
``StageController``.  It does not create another lifecycle or try to infer a
target from an implementation directory.  A caller supplies the current
Stage Definition, contract, controller snapshot and integration artifact;
the resolver requires those sources to agree on one target and one
verification command before any target is written.

The delivery operation is intentionally two phase:

* validate a current ``STAGE_READY`` result and its evidence/identity;
* atomically copy the reviewed source artifact, run post-integration
  verification, write one bounded delivery receipt, and optionally close the
  Stage through ``StageController.approve_stage``.

Provider retries remain metadata on the reviewed result.  This seam never
increments or rewrites the controller's attempt/iteration coordinates.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


DELIVERY_RECEIPT_SCHEMA = "stage_delivery_receipt.v1"
ABSENT_DIGEST = "ABSENT"
_MISSING = object()


class DeliveryIntegrationError(RuntimeError):
    """A bounded, fail-closed delivery error."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.details = copy.deepcopy(dict(details or {}))
        super().__init__(message)


@dataclass(frozen=True)
class ResolvedDeliveryTarget:
    """The unique target and policy selected from the Stage sources."""

    repository_root: Path
    target_path: Path
    target_relative_path: str
    allowed_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    verification: Mapping[str, Any]
    delivery_artifact_path: Path
    delivery_artifact_relative_path: str


@dataclass(frozen=True)
class PromotionValidation:
    """Validated identity and digests used by one delivery attempt."""

    stage_id: str
    stage_iteration_index: int
    attempt_index: int
    request_id: str
    result_digest: str
    requirement_digest: str
    design_digest: str
    gpt_review_consultation_id: str
    source_artifact_path: Path
    source_artifact_digest: str
    target: ResolvedDeliveryTarget
    controller_before: Mapping[str, Any]


@dataclass(frozen=True)
class DeliveryResult:
    """The bounded result of one delivery attempt."""

    status: str
    receipt_path: Path
    receipt: Mapping[str, Any]
    reused: bool = False


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def file_digest(path: str | os.PathLike[str]) -> str:
    """Return the SHA-256 digest of a file's bytes."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(value: Any, field: str, *, required: bool = True) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if required:
        raise DeliveryIntegrationError("IDENTITY_MISSING", f"{field} must be non-empty text")
    return None


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeliveryIntegrationError("INPUT_INVALID", f"{field} must be an object")
    return value


def _safe_copy(value: Any, *, depth: int = 0) -> Any:
    """Copy bounded JSON-like values without retaining arbitrary objects."""

    if depth > 8:
        return "<depth-limited>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _safe_copy(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_copy(item, depth=depth + 1) for item in value]
    return str(value)


def _relative(root: Path, path: Path, field: str) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise DeliveryIntegrationError(
            "PATH_ESCAPE",
            f"{field} must stay within the authoritative repository root",
            details={"field": field, "path": str(path), "root": str(root)},
        ) from exc
    if str(relative) in {"", "."}:
        raise DeliveryIntegrationError("PATH_INVALID", f"{field} cannot be the repository root")
    return relative.as_posix()


def _path_from_candidate(root: Path, value: Any, field: str) -> Path:
    raw = _text(value, field)
    assert raw is not None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate.resolve()


def _flatten_path(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, Mapping):
        for key in ("path", "target_path", "relative_path", "value"):
            if key in value:
                return _flatten_path(value[key])
        return []
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            result.extend(_flatten_path(item))
        return result
    return []


def _source_sections(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return a bounded set of nested source sections with delivery data."""

    sections = [source]
    for key in ("integration", "delivery", "promotion", "authoritative_target", "delivery_target"):
        value = source.get(key)
        if isinstance(value, Mapping):
            sections.append(value)
    return sections


def _candidates(sources: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> list[str]:
    values: list[str] = []
    for source in sources:
        for section in _source_sections(source):
            for key in keys:
                if key in section:
                    values.extend(_flatten_path(section[key]))
    return values


def _unique_normalized_paths(root: Path, values: Sequence[str], field: str) -> list[tuple[str, Path]]:
    normalized: dict[str, Path] = {}
    for value in values:
        path = _path_from_candidate(root, value, field)
        relative = _relative(root, path, field)
        normalized.setdefault(relative, path)
    return [(relative, normalized[relative]) for relative in sorted(normalized)]


def _list_policy(source: Mapping[str, Any], key: str) -> list[str]:
    value: Any = source.get(key, _MISSING)
    if value is _MISSING and isinstance(source.get("contract"), Mapping):
        value = source["contract"].get(key, _MISSING)
    if value is _MISSING:
        return []
    if not isinstance(value, (list, tuple)):
        raise DeliveryIntegrationError("PATH_POLICY_INVALID", f"{key} must be an array")
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def _policy_values(
    root: Path,
    sources: Sequence[Mapping[str, Any]],
    key: str,
) -> tuple[str, ...]:
    policies: set[tuple[str, ...]] = set()
    for source in sources:
        values = _list_policy(source, key)
        if not values:
            continue
        normalized = tuple(sorted({
            _relative(root, _path_from_candidate(root, value, key), key)
            for value in values
        }))
        policies.add(normalized)
    if len(policies) > 1:
        raise DeliveryIntegrationError(
            "PATH_POLICY_MISMATCH",
            f"{key} differs between Stage sources",
            details={"field": key, "values": [list(item) for item in sorted(policies)]},
        )
    if not policies:
        raise DeliveryIntegrationError("PATH_POLICY_MISSING", f"{key} is required")
    return next(iter(policies))


def _verification_value(source: Mapping[str, Any]) -> list[Any]:
    values: list[Any] = []
    for section in _source_sections(source):
        for key in ("post_integration_verification", "verification_command", "post_verification", "verification_spec"):
            if key in section:
                values.append(section[key])
        value = section.get("verification")
        if value is not None:
            values.append(value)
    return values


def _normalize_verification(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        command = value.get("command", value.get("cmd"))
        if command is None and isinstance(value.get("commands"), (list, tuple)):
            commands = [item for item in value["commands"] if isinstance(item, str) and item.strip()]
            if commands:
                return {"commands": commands, **({"environment": _safe_copy(value["environment"])} if isinstance(value.get("environment"), Mapping) else {})}
        if command is None:
            return {}
        result: dict[str, Any] = {"command": _safe_copy(command)}
        environment = value.get("environment", value.get("env"))
        if isinstance(environment, Mapping):
            result["environment"] = {
                str(key): str(item) for key, item in environment.items()
                if isinstance(key, str) and isinstance(item, (str, int, float, bool))
            }
        return result
    if isinstance(value, (str, list, tuple)):
        return {"command": _safe_copy(value)}
    return {}


def _resolve_verification(sources: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values: list[dict[str, Any]] = []
    for source in sources:
        for value in _verification_value(source):
            normalized = _normalize_verification(value)
            if normalized:
                values.append(normalized)
    unique = {_canonical(item): item for item in values}
    if len(unique) != 1:
        code = "VERIFICATION_COMMAND_MISSING" if not unique else "VERIFICATION_COMMAND_AMBIGUOUS"
        raise DeliveryIntegrationError(code, "exactly one post-integration verification command is required")
    return next(iter(unique.values()))


def _target_policy_match(relative: str, allowed: Sequence[str], protected: Sequence[str]) -> None:
    def under(candidate: str, parent: str) -> bool:
        return candidate == parent or candidate.startswith(parent.rstrip("/") + "/")

    if not any(under(relative, item) for item in allowed):
        raise DeliveryIntegrationError(
            "TARGET_NOT_ALLOWED",
            "authoritative target is outside the Stage allowed paths",
            details={"target_path": relative, "allowed_paths": list(allowed)},
        )
    if any(under(relative, item) for item in protected):
        raise DeliveryIntegrationError(
            "TARGET_PROTECTED",
            "authoritative target is protected by the Stage contract",
            details={"target_path": relative, "protected_paths": list(protected)},
        )


def resolve_authoritative_target(
    *,
    repository_root: str | os.PathLike[str],
    stage_definition: Mapping[str, Any],
    stage_contract: Mapping[str, Any],
    controller_state: Mapping[str, Any],
    integration_artifact: Mapping[str, Any],
) -> ResolvedDeliveryTarget:
    """Resolve one target from all current Stage sources.

    A missing candidate or a disagreement between sources fails closed.  The
    function intentionally does not default to ``.research/stages/...`` or
    any other implementation directory.
    """

    root = Path(repository_root).expanduser().resolve()
    sources = [
        _mapping(stage_definition, "stage_definition"),
        _mapping(stage_contract, "stage_contract"),
        _mapping(controller_state, "controller_state"),
        _mapping(integration_artifact, "integration_artifact"),
    ]
    target_values = _candidates(
        sources,
        ("authoritative_integration_target", "authoritative_target", "integration_target", "target_path"),
    )
    targets = _unique_normalized_paths(root, target_values, "target_path")
    if len(targets) != 1:
        code = "TARGET_UNRESOLVED" if not targets else "TARGET_AMBIGUOUS"
        raise DeliveryIntegrationError(
            code,
            "Stage sources must identify exactly one authoritative integration target",
            details={"candidates": [item[0] for item in targets]},
        )
    relative, target = targets[0]
    allowed = _policy_values(root, sources, "allowed_paths")
    protected = _policy_values(root, sources, "protected_paths")
    _target_policy_match(relative, allowed, protected)

    verification = _resolve_verification(sources)
    artifact_values = _candidates(
        sources,
        ("delivery_artifact_path", "final_artifact_path", "receipt_path"),
    )
    artifacts = _unique_normalized_paths(root, artifact_values, "delivery_artifact_path")
    if not artifacts:
        raise DeliveryIntegrationError(
            "DELIVERY_ARTIFACT_UNRESOLVED",
            "Stage sources must identify the final delivery receipt location",
        )
    if len(artifacts) != 1:
        raise DeliveryIntegrationError(
            "DELIVERY_ARTIFACT_AMBIGUOUS",
            "Stage sources must identify one final delivery receipt location",
            details={"candidates": [item[0] for item in artifacts]},
        )
    artifact_relative, artifact_path = artifacts[0]
    _target_policy_match(artifact_relative, allowed, protected)
    return ResolvedDeliveryTarget(
        repository_root=root,
        target_path=target,
        target_relative_path=relative,
        allowed_paths=allowed,
        protected_paths=protected,
        verification=verification,
        delivery_artifact_path=artifact_path,
        delivery_artifact_relative_path=artifact_relative,
    )


def _nested(source: Mapping[str, Any], *keys: str) -> Any:
    current: Any = source
    for key in keys:
        if not isinstance(current, Mapping):
            return _MISSING
        current = current.get(key, _MISSING)
    return current


def _first_value(sources: Sequence[Mapping[str, Any]], keys: Sequence[str]) -> Any:
    for source in sources:
        for key in keys:
            value = source.get(key, _MISSING)
            if value is not _MISSING:
                return value
            for section_name in ("identity", "scope_checks", "baseline", "stage", "review", "receipt", "provenance"):
                section = source.get(section_name)
                if isinstance(section, Mapping) and key in section:
                    return section[key]
    return _MISSING


def _controller_record(controller: Any, stage_id: str) -> dict[str, Any]:
    try:
        value = controller.show_stage(stage_id)
    except Exception as exc:  # noqa: BLE001 - controller boundary
        raise DeliveryIntegrationError("CONTROLLER_UNAVAILABLE", "StageController state could not be read") from exc
    if not isinstance(value, Mapping):
        raise DeliveryIntegrationError("CONTROLLER_INVALID", "StageController returned an invalid Stage record")
    return copy.deepcopy(dict(value))


def _stage_id_from_sources(
    stage_definition: Mapping[str, Any],
    stage_contract: Mapping[str, Any],
    controller_record: Mapping[str, Any],
    reviewed_result: Mapping[str, Any],
) -> str:
    values: list[str] = []
    for source in (stage_definition, stage_contract, controller_record, reviewed_result):
        value = _first_value([source], ("stage_id",))
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
        contract = source.get("contract")
        if isinstance(contract, Mapping) and isinstance(contract.get("stage_id"), str):
            values.append(str(contract["stage_id"]).strip())
    unique = sorted({item for item in values if item})
    if len(unique) != 1:
        raise DeliveryIntegrationError("STAGE_ID_MISMATCH", "Stage identity is missing or disagrees between sources")
    return unique[0]


def _digest_identity(
    *,
    stage_definition: Mapping[str, Any],
    stage_contract: Mapping[str, Any],
    controller_record: Mapping[str, Any],
    reviewed_result: Mapping[str, Any],
    review_receipt: Mapping[str, Any],
) -> tuple[str, str, str]:
    sources = [stage_definition, stage_contract, controller_record, reviewed_result, review_receipt]
    baseline = stage_contract.get("baseline") if isinstance(stage_contract.get("baseline"), Mapping) else {}
    requirement_values: list[str] = []
    design_values: list[str] = []
    for source in sources:
        for key, bucket in (("requirement_digest", requirement_values), ("brief_digest", requirement_values), ("design_digest", design_values)):
            value = _first_value([source], (key,))
            if isinstance(value, str) and value.strip():
                bucket.append(value.strip())
    if isinstance(baseline, Mapping):
        for key, bucket in (("requirement_digest", requirement_values), ("brief_digest", requirement_values), ("design_digest", design_values)):
            value = baseline.get(key)
            if isinstance(value, str) and value.strip():
                bucket.append(value.strip())
    requirement = sorted(set(requirement_values))
    design = sorted(set(design_values))
    if len(requirement) != 1 or len(design) != 1:
        raise DeliveryIntegrationError(
            "IDENTITY_MISMATCH",
            "Requirement and Design digests must be present and equal across Stage sources",
            details={"requirement_candidates": requirement, "design_candidates": design},
        )
    return requirement[0], design[0], _json_digest({"requirement_digest": requirement[0], "design_digest": design[0]})


def _check_equal_identity(
    name: str,
    value: Any,
    expected: str,
    *,
    required: bool = True,
) -> None:
    if value is _MISSING or value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise DeliveryIntegrationError("IDENTITY_MISSING", f"{name} is required")
        return
    if str(value).strip() != expected:
        raise DeliveryIntegrationError(
            "IDENTITY_MISMATCH",
            f"{name} disagrees with the authoritative Stage identity",
            details={"field": name, "expected": expected, "actual": str(value).strip()},
        )


def _binding(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = record.get("stage_result_binding")
    if not isinstance(value, Mapping):
        raise DeliveryIntegrationError("EXECUTION_BINDING_MISSING", "StageController has no current result binding")
    if value.get("evidence_complete") is not True:
        raise DeliveryIntegrationError("EXECUTION_EVIDENCE_INCOMPLETE", "current result binding is incomplete")
    return value


def validate_promotion_preconditions(
    controller: Any,
    *,
    stage_definition: Mapping[str, Any],
    stage_contract: Mapping[str, Any],
    integration_artifact: Mapping[str, Any],
    reviewed_result: Mapping[str, Any],
    review_receipt: Mapping[str, Any],
    source_artifact_path: str | os.PathLike[str],
    source_artifact_digest: str | None = None,
    controller_state: Mapping[str, Any] | None = None,
) -> PromotionValidation:
    """Validate every promotion identity without mutating the controller."""

    result = _mapping(reviewed_result, "reviewed_result")
    review = _mapping(review_receipt, "review_receipt")
    stage_id_hint = stage_contract.get("stage_id")
    if not isinstance(stage_id_hint, str) or not stage_id_hint.strip():
        stage_id_hint = stage_definition.get("stage_id")
    if not isinstance(stage_id_hint, str) or not stage_id_hint.strip():
        raise DeliveryIntegrationError("STAGE_ID_MISSING", "Stage Definition must identify the Stage")
    record = _controller_record(controller, stage_id_hint)
    stage_id = _stage_id_from_sources(stage_definition, stage_contract, record, result)
    if stage_id != stage_id_hint:
        raise DeliveryIntegrationError("STAGE_ID_MISMATCH", "Stage identity disagrees with the contract")
    if record.get("status") != "STAGE_READY":
        code = "HUMAN_GATE_BLOCKS_PROMOTION" if record.get("pending_human_gate") is not None else "STAGE_NOT_READY"
        raise DeliveryIntegrationError(code, "only a current STAGE_READY Stage may be promoted")
    if record.get("pending_human_gate") is not None:
        raise DeliveryIntegrationError("HUMAN_GATE_BLOCKS_PROMOTION", "a pending HUMAN_GATE blocks promotion")
    if result.get("stage_ready") is not True or str(result.get("status", "")).upper() not in {"SUCCEEDED", "SUCCESS", "COMPLETED"}:
        raise DeliveryIntegrationError("RESULT_NOT_STAGE_READY", "reviewed execution result is not a successful STAGE_READY result")
    review_decision = review.get("workflow_decision", review.get("decision"))
    if str(review_decision or "").upper() == "HUMAN_GATE":
        raise DeliveryIntegrationError("HUMAN_GATE_BLOCKS_PROMOTION", "HUMAN_GATE review results cannot auto-promote")
    if str(review_decision or "").upper() != "STAGE_READY":
        raise DeliveryIntegrationError("REVIEW_DECISION_MISMATCH", "GPT review identity is not STAGE_READY")
    if str(review.get("status", "complete")).lower() not in {"complete", "completed", "success", "succeeded"}:
        raise DeliveryIntegrationError("REVIEW_INVALID", "GPT review receipt is not complete")

    requirement_digest, design_digest, _ = _digest_identity(
        stage_definition=stage_definition,
        stage_contract=stage_contract,
        controller_record=record,
        reviewed_result=result,
        review_receipt=review,
    )
    for source in (stage_definition, stage_contract, record, result, review):
        req = _first_value([source], ("requirement_digest", "brief_digest"))
        des = _first_value([source], ("design_digest",))
        _check_equal_identity("requirement_digest", req, requirement_digest, required=False)
        _check_equal_identity("design_digest", des, design_digest, required=False)

    plan_id = stage_contract.get("plan_id")
    if isinstance(plan_id, str) and plan_id.strip():
        _check_equal_identity("plan_id", _first_value([result], ("plan_id",)), plan_id.strip(), required=False)
    task_id = stage_contract.get("task_id") or stage_definition.get("task_id")
    if isinstance(task_id, str) and task_id.strip():
        _check_equal_identity("task_id", _first_value([result], ("task_id",)), task_id.strip(), required=False)

    consultation_id = _first_value([review], ("consultation_id", "gpt_review_consultation_id"))
    consultation_id = _text(consultation_id, "gpt_review_consultation_id")
    result_consultation = _first_value([result], ("gpt_review_consultation_id", "review_consultation_id", "consultation_id"))
    _check_equal_identity("gpt_review_consultation_id", result_consultation, consultation_id, required=False)

    binding = _binding(record)
    result_digest = _json_digest(dict(result))
    bound_digest = binding.get("result_digest")
    if bound_digest != result_digest:
        bound_result = record.get("latest_stage_result", {}).get("result") if isinstance(record.get("latest_stage_result"), Mapping) else None
        if not isinstance(bound_result, Mapping):
            raise DeliveryIntegrationError("RESULT_BINDING_MISMATCH", "reviewed execution result does not match the current StageController binding")
        normalized = dict(result)
        normalized["stage_ready"] = bound_result.get("stage_ready")
        normalized.pop("baseline_digest", None)
        if _json_digest(normalized) != bound_digest:
            raise DeliveryIntegrationError("RESULT_BINDING_MISMATCH", "reviewed execution result does not match the current StageController binding", details={"binding_digest": bound_digest, "result_digest": result_digest})
        result_digest = bound_digest
    iteration = record.get("iteration_index")
    if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 1:
        raise DeliveryIntegrationError("ITERATION_INVALID", "current Stage iteration is invalid")
    if binding.get("iteration_index") != iteration:
        raise DeliveryIntegrationError("RESULT_BINDING_MISMATCH", "binding iteration is not current")
    attempt_index = binding.get("attempt_index")
    request_id = binding.get("request_id")
    if isinstance(attempt_index, bool) or not isinstance(attempt_index, int) or attempt_index < 1:
        raise DeliveryIntegrationError("ATTEMPT_IDENTITY_MISSING", "current result binding lacks attempt_index")
    request_id = _text(request_id, "request_id")
    source = Path(source_artifact_path).expanduser()
    if not source.is_absolute():
        source = Path(record.get("contract", {}).get("repository_root", Path.cwd())) / source
    source = source.resolve()
    if not source.is_file():
        raise DeliveryIntegrationError("SOURCE_ARTIFACT_MISSING", "reviewed source artifact does not exist", details={"path": str(source)})
    actual_source_digest = file_digest(source)
    expected_source_digest = source_artifact_digest or _first_value(
        [result, review, integration_artifact], ("source_artifact_digest", "artifact_digest")
    )
    expected_source_digest = _text(expected_source_digest, "source_artifact_digest")
    if actual_source_digest != expected_source_digest:
        raise DeliveryIntegrationError(
            "SOURCE_ARTIFACT_MISMATCH",
            "source artifact digest does not match reviewed evidence",
            details={"expected": expected_source_digest, "actual": actual_source_digest},
        )
    target = resolve_authoritative_target(
        repository_root=record.get("contract", {}).get("repository_root", Path.cwd()),
        stage_definition=stage_definition,
        stage_contract=stage_contract,
        controller_state=controller_state or {"stage": record, **record},
        integration_artifact=integration_artifact,
    )
    return PromotionValidation(
        stage_id=stage_id,
        stage_iteration_index=iteration,
        attempt_index=attempt_index,
        request_id=request_id,
        result_digest=result_digest,
        requirement_digest=requirement_digest,
        design_digest=design_digest,
        gpt_review_consultation_id=consultation_id,
        source_artifact_path=source,
        source_artifact_digest=actual_source_digest,
        target=target,
        controller_before=record,
    )


def _git_provenance(root: Path, target_relative: str, *, before: bool) -> dict[str, Any]:
    """Capture metadata-only Git provenance when the root is a repository."""

    prefix = "before" if before else "after"
    try:
        commit = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--no-ext-diff", "--binary", "--", target_relative],
            capture_output=True,
            text=False,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "commit": None, "diff_digest": None, "diff_bytes": None, "phase": prefix}
    commit_value = commit.stdout.strip() if commit.returncode == 0 else None
    diff_bytes = diff.stdout or b""
    return {
        "available": bool(commit_value is not None and diff.returncode == 0),
        "commit": commit_value,
        "diff_digest": hashlib.sha256(diff_bytes).hexdigest() if diff.returncode == 0 else None,
        "diff_bytes": len(diff_bytes) if diff.returncode == 0 else None,
        "phase": prefix,
    }


def _verification_default(target: ResolvedDeliveryTarget) -> dict[str, Any]:
    value = target.verification
    command = value.get("command")
    commands = value.get("commands")
    if command is None and isinstance(commands, (list, tuple)):
        results: list[dict[str, Any]] = []
        for item in commands:
            one = ResolvedDeliveryTarget(
                repository_root=target.repository_root,
                target_path=target.target_path,
                target_relative_path=target.target_relative_path,
                allowed_paths=target.allowed_paths,
                protected_paths=target.protected_paths,
                verification={"command": item, "environment": value.get("environment", {})},
                delivery_artifact_path=target.delivery_artifact_path,
                delivery_artifact_relative_path=target.delivery_artifact_relative_path,
            )
            results.append(_verification_default(one))
        passed = all(item.get("passed") is True for item in results)
        return {
            "status": "PASS" if passed else "FAIL",
            "passed": passed,
            "commands": _safe_copy(list(commands)),
            "results": results,
        }
    if isinstance(command, str):
        import shlex

        command = shlex.split(command, posix=False)
    if not isinstance(command, (list, tuple)) or not command:
        raise DeliveryIntegrationError("VERIFICATION_COMMAND_INVALID", "verification command must be a non-empty argv array or string")
    env = os.environ.copy()
    configured = value.get("environment")
    if isinstance(configured, Mapping):
        env.update({str(key): str(item) for key, item in configured.items()})
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(target.repository_root),
            env=env,
            capture_output=True,
            text=False,
            check=False,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DeliveryIntegrationError("VERIFICATION_FAILED", "post-integration verification could not be executed") from exc
    stdout = completed.stdout or b""
    stderr = completed.stderr or b""
    return {
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "command": _safe_copy(list(command)),
        "stdout_bytes": len(stdout),
        "stdout_digest": hashlib.sha256(stdout).hexdigest(),
        "stderr_bytes": len(stderr),
        "stderr_digest": hashlib.sha256(stderr).hexdigest(),
    }


def _safe_verification(value: Any, target: ResolvedDeliveryTarget) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"status": "PASS" if value else "FAIL", "passed": value, "command": _safe_copy(target.verification.get("command"))}
    if not isinstance(value, Mapping):
        raise DeliveryIntegrationError("VERIFICATION_INVALID", "verification runner must return an object or boolean")
    result = {
        key: _safe_copy(value[key])
        for key in ("status", "passed", "returncode", "command", "stdout_bytes", "stdout_digest", "stderr_bytes", "stderr_digest")
        if key in value
    }
    passed = result.get("passed")
    if passed is not True or str(result.get("status", "")).upper() not in {"PASS", "PASSED", "SUCCESS", "SUCCEEDED"}:
        raise DeliveryIntegrationError("VERIFICATION_FAILED", "post-integration verification did not pass", details={"verification": result})
    return result


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".delivery", dir=str(target.parent))
    os.close(fd)
    temporary_path = Path(temporary)
    try:
        shutil.copyfile(source, temporary_path)
        os.replace(temporary_path, target)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".delivery", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _receipt_reusable(path: Path, validation: PromotionValidation) -> Mapping[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeliveryIntegrationError("DELIVERY_RECEIPT_CONFLICT", "existing delivery artifact is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise DeliveryIntegrationError("DELIVERY_RECEIPT_CONFLICT", "existing delivery artifact is not an object")
    identity = value.get("reviewed_execution_result_identity")
    if (
        value.get("schema_version") == DELIVERY_RECEIPT_SCHEMA
        and value.get("status") in {"DELIVERED", "CLOSED"}
        and isinstance(identity, Mapping)
        and identity.get("result_digest") == validation.result_digest
        and value.get("source_artifact_digest") == validation.source_artifact_digest
        and value.get("target_path") == validation.target.target_relative_path
    ):
        return value
    raise DeliveryIntegrationError("DELIVERY_RECEIPT_CONFLICT", "delivery artifact already belongs to another attempt")


def _resume_closed_delivery(
    controller: Any,
    *,
    stage_definition: Mapping[str, Any],
    stage_contract: Mapping[str, Any],
    integration_artifact: Mapping[str, Any],
    reviewed_result: Mapping[str, Any],
    source_artifact_path: str | os.PathLike[str],
    source_artifact_digest: str | None,
    controller_state: Mapping[str, Any] | None,
) -> DeliveryResult | None:
    """Resume an already closed delivery without replaying the mutation.

    The normal validation path intentionally accepts only ``STAGE_READY``.
    A reloaded controller is ``APPROVED`` after a successful close, so this
    narrow read-only fast path is needed for crash-safe resume.  It returns
    only when the existing receipt proves the exact same result/source/target
    identity; every mismatch remains fail closed.
    """

    stage_id = stage_contract.get("stage_id")
    if not isinstance(stage_id, str) or not stage_id.strip():
        return None
    record = _controller_record(controller, stage_id)
    if record.get("status") != "APPROVED":
        return None
    target = resolve_authoritative_target(
        repository_root=record.get("contract", {}).get("repository_root", Path.cwd()),
        stage_definition=stage_definition,
        stage_contract=stage_contract,
        controller_state=controller_state or {"stage": record, **record},
        integration_artifact=integration_artifact,
    )
    receipt_path = target.delivery_artifact_path
    if not receipt_path.is_file():
        return None
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeliveryIntegrationError("DELIVERY_RECEIPT_CONFLICT", "existing delivery artifact is not valid JSON") from exc
    if not isinstance(receipt, Mapping):
        raise DeliveryIntegrationError("DELIVERY_RECEIPT_CONFLICT", "existing delivery artifact is not an object")
    source = Path(source_artifact_path).expanduser()
    if not source.is_absolute():
        source = Path(record.get("contract", {}).get("repository_root", Path.cwd())) / source
    source = source.resolve()
    expected_source = source_artifact_digest
    if expected_source is None and source.is_file():
        expected_source = file_digest(source)
    identity = receipt.get("reviewed_execution_result_identity")
    if (
        receipt.get("schema_version") == DELIVERY_RECEIPT_SCHEMA
        and receipt.get("status") == "CLOSED"
        and receipt.get("stage_id") == stage_id
        and isinstance(identity, Mapping)
        and identity.get("result_digest") == _json_digest(dict(reviewed_result))
        and receipt.get("source_artifact_digest") == expected_source
        and receipt.get("target_path") == target.target_relative_path
    ):
        return DeliveryResult("CLOSED", receipt_path, receipt, reused=True)
    raise DeliveryIntegrationError("DELIVERY_RECEIPT_CONFLICT", "closed Stage has a mismatched delivery receipt")


def integrate_delivery(
    controller: Any,
    *,
    stage_definition: Mapping[str, Any],
    stage_contract: Mapping[str, Any],
    integration_artifact: Mapping[str, Any],
    reviewed_result: Mapping[str, Any],
    review_receipt: Mapping[str, Any],
    source_artifact_path: str | os.PathLike[str],
    source_artifact_digest: str | None = None,
    controller_state: Mapping[str, Any] | None = None,
    verification_runner: Callable[[ResolvedDeliveryTarget], Mapping[str, Any] | bool] | None = None,
    close_stage: bool = True,
    actor: str = "delivery-integration",
    rationale: str = "verified reviewed execution result delivery",
) -> DeliveryResult:
    """Atomically integrate a reviewed result and close via StageController."""

    resumed = _resume_closed_delivery(
        controller,
        stage_definition=stage_definition,
        stage_contract=stage_contract,
        integration_artifact=integration_artifact,
        reviewed_result=reviewed_result,
        source_artifact_path=source_artifact_path,
        source_artifact_digest=source_artifact_digest,
        controller_state=controller_state,
    )
    if resumed is not None:
        return resumed
    validation = validate_promotion_preconditions(
        controller,
        stage_definition=stage_definition,
        stage_contract=stage_contract,
        integration_artifact=integration_artifact,
        reviewed_result=reviewed_result,
        review_receipt=review_receipt,
        source_artifact_path=source_artifact_path,
        source_artifact_digest=source_artifact_digest,
        controller_state=controller_state,
    )
    target = validation.target
    existing = _receipt_reusable(target.delivery_artifact_path, validation)
    if existing is not None:
        return DeliveryResult("CLOSED" if existing.get("status") == "CLOSED" else "DELIVERED", target.delivery_artifact_path, existing, reused=True)

    before_exists = target.target_path.is_file()
    before_bytes = target.target_path.read_bytes() if before_exists else None
    before_digest = file_digest(target.target_path) if before_exists else ABSENT_DIGEST
    git_before = _git_provenance(target.repository_root, target.target_relative_path, before=True)
    try:
        _atomic_copy(validation.source_artifact_path, target.target_path)
        after_digest = file_digest(target.target_path)
        if after_digest != validation.source_artifact_digest:
            raise DeliveryIntegrationError("TARGET_WRITE_MISMATCH", "integrated target digest differs from source artifact")
        verification = _safe_verification(
            verification_runner(target) if verification_runner is not None else _verification_default(target),
            target,
        )
        git_after = _git_provenance(target.repository_root, target.target_relative_path, before=False)
    except DeliveryIntegrationError:
        if before_bytes is None:
            if target.target_path.exists():
                target.target_path.unlink()
        else:
            target.target_path.write_bytes(before_bytes)
        raise
    except Exception as exc:  # noqa: BLE001 - delivery boundary
        if before_bytes is None:
            if target.target_path.exists():
                target.target_path.unlink()
        else:
            target.target_path.write_bytes(before_bytes)
        raise DeliveryIntegrationError("INTEGRATION_FAILED", "authoritative integration failed") from exc

    # Snapshot again before closing.  A concurrent state mutation invalidates
    # the operation and rolls the target back without touching controller
    # state.
    current = _controller_record(controller, validation.stage_id)
    current_binding = current.get("stage_result_binding")
    if current.get("status") != "STAGE_READY" or not isinstance(current_binding, Mapping) or current_binding.get("result_digest") != validation.result_digest:
        if before_bytes is None:
            if target.target_path.exists():
                target.target_path.unlink()
        else:
            target.target_path.write_bytes(before_bytes)
        raise DeliveryIntegrationError("CONTROLLER_STATE_CHANGED", "StageController changed during delivery")

    status = "DELIVERED"
    controller_after: Mapping[str, Any] = current
    if close_stage:
        approve = getattr(controller, "approve_stage", None)
        if not callable(approve):
            raise DeliveryIntegrationError("CONTROLLER_CLOSE_UNSUPPORTED", "StageController cannot close the Stage")
        try:
            outcome = approve(validation.stage_id, actor=actor, rationale=rationale)
        except Exception as exc:  # noqa: BLE001 - controller boundary
            if before_bytes is None:
                if target.target_path.exists():
                    target.target_path.unlink()
            else:
                target.target_path.write_bytes(before_bytes)
            raise DeliveryIntegrationError("CONTROLLER_CLOSE_FAILED", "StageController refused formal close") from exc
        if not isinstance(outcome, Mapping) or not isinstance(outcome.get("stage"), Mapping) or outcome["stage"].get("status") != "APPROVED":
            raise DeliveryIntegrationError("CONTROLLER_CLOSE_FAILED", "StageController close did not reach APPROVED")
        controller_after = outcome["stage"]
        status = "CLOSED"

    receipt = {
        "schema_version": DELIVERY_RECEIPT_SCHEMA,
        "receipt_id": f"DELIVERY-{uuid.uuid4().hex[:16]}",
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "stage_identity": {
            "stage_id": validation.stage_id,
            "stage_iteration_index": validation.stage_iteration_index,
            "status_before": validation.controller_before.get("status"),
            "status_after": controller_after.get("status"),
        },
        "stage_id": validation.stage_id,
        "requirement_digest": validation.requirement_digest,
        "design_digest": validation.design_digest,
        "reviewed_execution_result_identity": {
            "result_digest": validation.result_digest,
            "stage_iteration_index": validation.stage_iteration_index,
            "attempt_index": validation.attempt_index,
            "request_id": validation.request_id,
        },
        "gpt_review_consultation_id": validation.gpt_review_consultation_id,
        "source_artifact_path": str(validation.source_artifact_path),
        "source_artifact_digest": validation.source_artifact_digest,
        "target_path": validation.target.target_relative_path,
        "target_before_digest": before_digest,
        "target_after_digest": after_digest,
        "verification_result": verification,
        "git_commit_diff_provenance": {
            "before": git_before,
            "after": git_after,
        },
        "retry_attempt_identity": {
            "stage_iteration_index": validation.stage_iteration_index,
            "attempt_index": validation.attempt_index,
            "request_id": validation.request_id,
            "retry_identity": f"{validation.stage_id}:{validation.stage_iteration_index}:{validation.attempt_index}:{validation.request_id}",
        },
        "delivery_artifact_path": target.delivery_artifact_relative_path,
        "controller_before": {
            "status": validation.controller_before.get("status"),
            "iteration_index": validation.controller_before.get("iteration_index"),
            "attempt_count": validation.controller_before.get("attempt_count"),
            "retry_count": validation.controller_before.get("retry_count"),
        },
        "controller_after": {
            "status": controller_after.get("status"),
            "iteration_index": controller_after.get("iteration_index"),
            "attempt_count": controller_after.get("attempt_count"),
            "retry_count": controller_after.get("retry_count"),
        },
    }
    _atomic_json(target.delivery_artifact_path, receipt)
    return DeliveryResult(status, target.delivery_artifact_path, receipt, reused=False)


__all__ = [
    "ABSENT_DIGEST",
    "DELIVERY_RECEIPT_SCHEMA",
    "DeliveryIntegrationError",
    "DeliveryResult",
    "PromotionValidation",
    "ResolvedDeliveryTarget",
    "file_digest",
    "integrate_delivery",
    "resolve_authoritative_target",
    "validate_promotion_preconditions",
]

