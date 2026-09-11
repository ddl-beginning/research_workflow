"""Small standard-library contract and JSON Schema helpers.

The PoC uses JSON Schema documents as machine-readable contracts.  A small
validator is included so the checkout remains runnable without installing
``jsonschema``.
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
import math
import posixpath
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


class ContractValidationError(ValueError):
    """Raised when a contract or contract-bound payload is invalid."""


SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
_GLOB_CHARS = set("*?[")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def load_schema(name: str) -> dict[str, Any]:
    requested = Path(name)
    if requested.is_absolute():
        path = requested
    else:
        filename = requested.name if requested.name.endswith(".json") else f"{requested.name}.schema.json"
        path = SCHEMA_DIR / filename
    path = path.resolve()
    try:
        path.relative_to(SCHEMA_DIR.resolve())
    except ValueError as exc:
        raise ContractValidationError(f"schema path escapes local schema directory: {name}") from exc
    if not path.is_file():
        raise ContractValidationError(f"schema not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractValidationError(f"cannot load schema {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractValidationError(f"schema root must be an object: {path}")
    return payload


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _fail(path: str, message: str) -> None:
    raise ContractValidationError(f"{path or '$'}: {message}")


def _matches_schema(instance: Any, schema: Mapping[str, Any], path: str) -> bool:
    try:
        validate_instance(instance, schema, path)
    except ContractValidationError:
        return False
    return True


def validate_instance(instance: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    """Validate the subset used by the local schemas."""

    if "$ref" in schema:
        _fail(path, "$ref is not supported by the local validator")
    if "const" in schema and instance != schema["const"]:
        _fail(path, f"must equal {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        _fail(path, f"must be one of {schema['enum']!r}")
    if "oneOf" in schema:
        matches = sum(_matches_schema(instance, option, path) for option in schema["oneOf"])
        if matches != 1:
            _fail(path, "must match exactly one oneOf option")
    if "anyOf" in schema:
        if not any(_matches_schema(instance, option, path) for option in schema["anyOf"]):
            _fail(path, "must match at least one anyOf option")

    expected_types = schema.get("type")
    if expected_types is not None:
        types = expected_types if isinstance(expected_types, list) else [expected_types]
        if not any(_type_matches(instance, str(item)) for item in types):
            _fail(path, f"has type {type(instance).__name__}, expected {types!r}")
    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            _fail(path, f"length must be >= {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            _fail(path, f"length must be <= {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            _fail(path, "does not match required pattern")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            _fail(path, f"must be >= {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            _fail(path, f"must be <= {schema['maximum']}")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            _fail(path, f"must contain at least {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            _fail(path, f"must contain at most {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            fingerprints = [canonical_json(item) for item in instance]
            if len(fingerprints) != len(set(fingerprints)):
                _fail(path, "items must be unique")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(instance):
                validate_instance(item, item_schema, f"{path}[{index}]")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                _fail(path, f"missing required property {key!r}")
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            for key, child_schema in properties.items():
                if key in instance and isinstance(child_schema, Mapping):
                    validate_instance(instance[key], child_schema, f"{path}.{key}")
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            _fail(path, f"must have at least {schema['minProperties']} properties")
        if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
            _fail(path, f"must have at most {schema['maxProperties']} properties")
        additional = schema.get("additionalProperties", True)
        known = set(properties) if isinstance(properties, Mapping) else set()
        if additional is False:
            extras = sorted(set(instance) - known)
            if extras:
                _fail(path, f"unknown properties: {extras!r}")
        elif isinstance(additional, Mapping):
            for key, value in instance.items():
                if key not in known:
                    validate_instance(value, additional, f"{path}.{key}")


def validate_against_schema(instance: Mapping[str, Any], schema_name: str) -> None:
    validate_instance(instance, load_schema(schema_name))


def _ensure_relative_path(value: str, field: str) -> str:
    """Normalize a path while rejecting every parent traversal component."""

    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field}: path must be a non-empty string")
    path = value.replace("\\", "/")
    if "\x00" in path or path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ContractValidationError(f"{field}: path must be relative: {value!r}")
    # Check before normalization: normpath would otherwise hide a/../b and
    # a/../../outside, which is unsafe for allowed/read/changed scopes.
    raw_parts = path.split("/")
    if any(part == ".." for part in raw_parts):
        raise ContractValidationError(f"{field}: path contains parent traversal: {value!r}")
    normalized = posixpath.normpath(path)
    if normalized in {".", ""} or normalized == ".." or normalized.startswith("../"):
        raise ContractValidationError(f"{field}: path contains parent traversal: {value!r}")
    return normalized


def _check_paths(paths: Iterable[str], field: str, *, allow_empty: bool = True) -> list[str]:
    checked = [_ensure_relative_path(item, field) for item in paths]
    if not allow_empty and not checked:
        raise ContractValidationError(f"{field}: at least one path is required")
    return checked


def _scope_is_within(path: str, scopes: Sequence[str]) -> bool:
    """Return whether a path/scope is bounded by at least one allowed scope."""

    return any(_matches_scope(path, scope) for scope in scopes)


def _scope_overlaps(path: str, scopes: Sequence[str]) -> bool:
    """Conservatively detect a path/scope that could touch a protected scope."""

    for scope in scopes:
        if _matches_scope(path, scope) or _matches_scope(scope, path):
            return True
    return False


def _is_user_viewable_uri(value: Any) -> bool:
    """Recognize local URI/path forms accepted for human review artifacts."""

    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate or any(character.isspace() for character in candidate):
        return False
    if candidate.startswith("data:") or "://" in candidate:
        return True
    return Path(candidate).suffix.lower() in {
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".txt", ".md", ".json", ".html"
    }


def _has_user_viewable_review_artifact(value: Any) -> bool:
    if not isinstance(value, list):
        return False
    for artifact in value:
        if _is_user_viewable_uri(artifact):
            return True
        if isinstance(artifact, Mapping):
            for key in ("uri", "url", "ref", "path"):
                if _is_user_viewable_uri(artifact.get(key)):
                    return True
    return False


def _digest_matches(candidate: str, expected: str) -> bool:
    """Match a full digest or a deliberately short (>=8 char) task prefix."""

    if candidate == expected:
        return True
    if len(candidate) >= 8 and expected.startswith(candidate):
        return True
    if len(expected) >= 8 and candidate.startswith(expected):
        return True
    return False


def validate_stage_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(contract, Mapping):
        raise ContractValidationError("stage contract must be an object")
    detached = copy.deepcopy(dict(contract))
    validate_against_schema(detached, "stage_contract")
    detached["allowed_paths"] = _check_paths(detached["allowed_paths"], "allowed_paths", allow_empty=False)
    detached["read_first"] = _check_paths(detached.get("read_first", []), "read_first")
    detached["protected_paths"] = _check_paths(detached.get("protected_paths", []), "protected_paths")
    return detached


def freeze_baseline(
    stage_id: str,
    metrics: Mapping[str, Any],
    artifact_refs: Sequence[str] | None = None,
    *,
    source: str = "stage_start",
) -> dict[str, Any]:
    if not isinstance(stage_id, str) or not stage_id:
        raise ContractValidationError("stage_id must be a non-empty string")
    if not isinstance(metrics, Mapping):
        raise ContractValidationError("baseline metrics must be an object")
    refs = list(artifact_refs or [])
    if any(not isinstance(ref, str) or not ref for ref in refs):
        raise ContractValidationError("baseline artifact_refs must contain non-empty strings")
    body = {
        "baseline_version": "baseline.v1",
        "stage_id": stage_id,
        "metrics": copy.deepcopy(dict(metrics)),
        "artifact_refs": refs,
        "source": source,
    }
    return {**body, "frozen": True, "digest": sha256_json(body)}


def _candidate_metrics(candidate: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("measurements", "metrics", "candidate_metrics"):
        value = candidate.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def compare_to_baseline(baseline: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(baseline, Mapping) or not baseline.get("frozen"):
        raise ContractValidationError("baseline must be a frozen baseline object")
    if not isinstance(candidate, Mapping):
        raise ContractValidationError("candidate must be an object")
    baseline_metrics = baseline.get("metrics", {})
    candidate_metrics = _candidate_metrics(candidate)
    deltas: dict[str, float | int | None] = {}
    changed: list[str] = []
    for key in sorted(set(baseline_metrics) | set(candidate_metrics)):
        old = baseline_metrics.get(key)
        new = candidate_metrics.get(key)
        if isinstance(old, (int, float)) and not isinstance(old, bool) and isinstance(new, (int, float)) and not isinstance(new, bool):
            deltas[key] = new - old
        else:
            deltas[key] = None
        if old != new:
            changed.append(key)
    candidate_refs = list(candidate.get("evidence_refs", candidate.get("artifact_refs", [])) or [])
    baseline_refs = list(baseline.get("artifact_refs", []))
    return {
        "baseline_digest": baseline.get("digest"),
        "baseline_metrics": copy.deepcopy(dict(baseline_metrics)),
        "candidate_metrics": copy.deepcopy(dict(candidate_metrics)),
        "metric_deltas": deltas,
        "changed_metrics": changed,
        "same_metrics": not changed,
        "artifact_changed": candidate_refs != baseline_refs if candidate_refs else False,
        "candidate_digest": sha256_json({"metrics": dict(candidate_metrics), "artifact_refs": candidate_refs}),
    }


def _matches_scope(path: str, pattern: str) -> bool:
    if _GLOB_CHARS.intersection(pattern):
        return PurePosixPath(path).match(pattern) or fnmatch.fnmatchcase(path, pattern)
    return path == pattern or path.startswith(pattern.rstrip("/") + "/")


def path_is_allowed(
    path: str,
    allowed_paths: Sequence[str],
    *,
    protected_paths: Sequence[str] | None = None,
    workspace_root: str | Path | None = None,
) -> bool:
    """Check relative/glob scope and, when supplied, resolved symlink scope."""

    try:
        normalized = _ensure_relative_path(path, "changed_files")
        allowed = [_ensure_relative_path(item, "allowed_paths") for item in allowed_paths]
        protected = [_ensure_relative_path(item, "protected_paths") for item in (protected_paths or [])]
    except ContractValidationError:
        return False
    if any(_matches_scope(normalized, item) for item in protected):
        return False
    if not any(_matches_scope(normalized, item) for item in allowed):
        return False
    if workspace_root is not None:
        root = Path(workspace_root).resolve()
        candidate = (root / Path(*normalized.split("/"))).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError:
            return False
    return True


def validate_scoped_task(task: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(task, Mapping):
        raise ContractValidationError("Codex task must be an object")
    detached = copy.deepcopy(dict(task))
    validate_against_schema(detached, "codex_task")
    detached["allowed_paths"] = _check_paths(detached["allowed_paths"], "allowed_paths", allow_empty=False)
    detached["read_first"] = _check_paths(detached.get("read_first", []), "read_first")
    detached["protected_paths"] = _check_paths(detached.get("protected_paths", []), "protected_paths")
    tool_policy = detached["tool_policy"]
    tool_policy["write_scope"] = _check_paths(
        tool_policy["write_scope"], "tool_policy.write_scope", allow_empty=False
    )
    if any(not _scope_is_within(scope, detached["allowed_paths"]) for scope in tool_policy["write_scope"]):
        raise ContractValidationError("tool_policy.write_scope must be a subset of allowed_paths")
    if any(_scope_overlaps(scope, detached["protected_paths"]) for scope in tool_policy["write_scope"]):
        raise ContractValidationError("tool_policy.write_scope overlaps protected_paths")
    if detached.get("lane") != "fake-codex":
        raise ContractValidationError("only lane=fake-codex is enabled by this PoC")
    if detached.get("critic_role") not in {"primary", "challenger", "none"}:
        raise ContractValidationError("critic_role must be primary, challenger, or none")
    return detached


def validate_result_report(
    result: Mapping[str, Any],
    task: Mapping[str, Any] | None = None,
    *,
    workspace_root: str | Path | None = None,
) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise ContractValidationError("ResultReport must be an object")
    detached = copy.deepcopy(dict(result))
    validate_against_schema(detached, "codex_result")
    if detached.get("stage_ready"):
        status = str(detached.get("status", "")).upper()
        if status != "SUCCEEDED":
            raise ContractValidationError("stage_ready requires status=SUCCEEDED")
        if not _has_user_viewable_review_artifact(detached.get("review_artifacts")):
            raise ContractValidationError(
                "stage_ready requires at least one user-viewable review_artifacts URI"
            )
    detached["changed_files"] = [_ensure_relative_path(item, "changed_files") for item in detached["changed_files"]]
    if task is not None:
        checked_task = validate_scoped_task(task)
        for field in ("plan_id", "stage_id", "task_id", "iteration_index"):
            if detached[field] != checked_task[field]:
                raise ContractValidationError(f"ResultReport {field} does not match task")
        if detached["abstraction_layer"] != checked_task["abstraction_layer"]:
            raise ContractValidationError("ResultReport abstraction_layer does not match task")
        result_baseline = detached.get("baseline_digest")
        if result_baseline is not None:
            task_baseline = checked_task.get("baseline_digest")
            baseline_refs = [
                ref.rsplit("/", 1)[-1]
                for ref in checked_task.get("context_refs", [])
                if isinstance(ref, str) and "/baseline/" in ref
            ]
            accepted = []
            if isinstance(task_baseline, str) and task_baseline:
                accepted.append(task_baseline)
            accepted.extend(item for item in baseline_refs if item)
            if not accepted or not any(_digest_matches(result_baseline, item) for item in accepted):
                raise ContractValidationError("ResultReport baseline_digest does not match task baseline reference")
        for changed_file in detached["changed_files"]:
            if not path_is_allowed(
                changed_file,
                checked_task["allowed_paths"],
                protected_paths=checked_task.get("protected_paths", []),
                workspace_root=workspace_root,
            ):
                raise ContractValidationError(f"changed file is outside allowed_paths/protected_paths: {changed_file!r}")
    return detached


__all__ = [
    "ContractValidationError",
    "canonical_json",
    "compare_to_baseline",
    "freeze_baseline",
    "load_schema",
    "path_is_allowed",
    "sha256_json",
    "validate_against_schema",
    "validate_instance",
    "validate_result_report",
    "validate_scoped_task",
    "validate_stage_contract",
]
