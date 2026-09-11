"""Bounded Stage context and deterministic evidence selection.

This is the Step 8 adapter around the already-passing Step 5 context-pack
contract.  It adds the Stage-specific human-readable ``STAGE_CONTEXT.md``, an
explicit resource-selection record, NORMAL delta reuse information, and a
content manifest that can be verified after being written to disk.

The module is intentionally data-only: it never calls a browser, ChatGPT,
Codex, or the Stage Controller.  Callers provide the latest local facts and
artifacts and decide when a consultation is appropriate.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractValidationError, canonical_json, validate_against_schema


MAX_RESOURCE_CLASSES = 9
MAX_CONTEXT_MARKDOWN_BYTES = 120_000
MAX_CONTEXT_JSON_BYTES = 256_000
CONTEXT_FIELDS = (
    "project_goal",
    "stage",
    "user_visible_goal",
    "established_facts",
    "current_method",
    "baseline_summary",
    "latest_result",
    "current_blocker",
    "rejected_directions",
    "protected_constraints",
    "recent_decisions",
    "artifact_refs",
    "git_baseline",
)
NORMAL_PRIORITY = (
    "stage_context",
    "latest_result",
    "metrics",
    "code_or_diff",
    "review_artifacts",
    "baseline",
    "stage_contract",
    "decision_history",
    "safety",
)
FRESH_PRIORITY = (
    "stage_context",
    "latest_result",
    "metrics",
    "review_artifacts",
    "baseline",
    "stage_contract",
    "architecture",
    "prior_closeout",
    "rejected_directions",
)
FRESH_FORBIDDEN = {
    "previous_recommendation",
    "previous_recommendations",
    "gpt_recommendation",
    "gpt_recommendations",
    "recommendation",
    "recommendations",
    "route_justification",
    "current_route_justification",
    "sunk_cost",
    "sunk_cost_narrative",
}
_SENSITIVE_KEYS = {
    "cookie", "cookies", "token", "tokens", "authorization", "session",
    "session_storage", "storage_state", "raw_dom", "dom", "prompt", "raw_response",
}


class StageContextError(ContractValidationError):
    """Raised when context is unsafe, unbounded, or tampered."""


def _text(value: Any, field: str, *, required: bool = False, max_length: int = 12000) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise StageContextError(f"{field} must be a string")
    value = value.strip()
    if required and not value:
        raise StageContextError(f"{field} must be non-empty")
    if len(value) > max_length or "\x00" in value:
        raise StageContextError(f"{field} is invalid or too long")
    return value


def _safe_value(value: Any, field: str = "value", *, max_depth: int = 8) -> Any:
    """Detach data and reject sensitive keys instead of silently redacting."""

    if max_depth < 0:
        raise StageContextError(f"{field} is too deeply nested")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.lower() in _SENSITIVE_KEYS:
                raise StageContextError(f"sensitive context field is not allowed: {field}.{key}")
            result[key] = _safe_value(child, f"{field}.{key}", max_depth=max_depth - 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(child, f"{field}[]", max_depth=max_depth - 1) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > 12000:
            raise StageContextError(f"{field} contains an unbounded string")
        return value
    raise StageContextError(f"unsupported context value at {field}")


def _fresh_value(value: Any, field: str, excluded: list[str], *, max_depth: int = 8) -> Any:
    """Copy a value while removing explicitly named prior-route narrative.

    FRESH is an independent review packet.  Nested recommendation fields are
    accounted for by path in ``fresh_excluded_classes`` so their omission is
    observable rather than a silent truncation.
    """

    if max_depth < 0:
        raise StageContextError(f"{field} is too deeply nested")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.lower() in FRESH_FORBIDDEN:
                excluded.append(f"{field}.{key}")
                continue
            result[key] = _fresh_value(child, f"{field}.{key}", excluded, max_depth=max_depth - 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_fresh_value(child, f"{field}[]", excluded, max_depth=max_depth - 1) for child in value]
    return _safe_value(value, field, max_depth=max_depth)


def _as_list(value: Any, field: str) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [_safe_value(value, field)]
    if isinstance(value, Mapping):
        return [_safe_value(value, field)]
    if not isinstance(value, (list, tuple)):
        raise StageContextError(f"{field} must be an array, object, or string")
    return [_safe_value(item, f"{field}[]") for item in value]


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _resource_files(value: Any) -> dict[str, str]:
    """Extract explicit file refs for NORMAL delta reuse reporting."""

    refs: dict[str, str] = {}
    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            path = item.get("path", item.get("file", item.get("filename")))
            if isinstance(path, str) and path.strip():
                candidate = path.replace("\\", "/").strip()
                file_digest = item.get("sha256", item.get("digest"))
                refs[candidate] = str(file_digest) if isinstance(file_digest, str) and file_digest else _digest(item)
            files = item.get("files")
            if files is not None:
                walk(files)
            for key, child in item.items():
                if key not in {"path", "file", "filename", "sha256", "digest", "files"}:
                    walk(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                walk(child)
    walk(value)
    return refs


def _manifest_for_resources(resources: Mapping[str, Any]) -> dict[str, Any]:
    resource_digests: dict[str, str] = {}
    file_digests: dict[str, str] = {}
    for name, value in resources.items():
        resource_digests[name] = _digest(value)
        for path, digest in _resource_files(value).items():
            file_digests[path] = digest
    body = {"resource_digests": resource_digests, "file_digests": file_digests}
    return {**body, "manifest_digest": _digest(body)}


def _delta(previous: Mapping[str, Any] | None, current_manifest: Mapping[str, Any], *, mode: str) -> dict[str, Any]:
    if mode != "NORMAL" or not isinstance(previous, Mapping):
        return {
            "mode": mode,
            "changed_resources": sorted(current_manifest.get("resource_digests", {})),
            "added_resources": sorted(current_manifest.get("resource_digests", {})),
            "removed_resources": [],
            "reused_files": [],
        }
    previous_manifest = previous.get("manifest", previous.get("resource_manifest", {}))
    if not isinstance(previous_manifest, Mapping):
        previous_manifest = {}
    old_resources = previous_manifest.get("resource_digests", {})
    old_files = previous_manifest.get("file_digests", {})
    old_reused = previous_manifest.get("reused_files", [])
    new_resources = current_manifest.get("resource_digests", {})
    new_files = current_manifest.get("file_digests", {})
    if not isinstance(old_resources, Mapping):
        old_resources = {}
    if not isinstance(old_files, Mapping):
        old_files = {}
    reused_files = sorted(
        path for path, digest in new_files.items()
        if isinstance(old_files.get(path), str) and old_files.get(path) == digest
    )
    # Interoperate with the Step 5 manifest shape, whose reuse records carry
    # a logical name rather than a ``file_digests`` map.  A logical name is
    # reused only when it is present in the current file set as well; this
    # prevents stale evidence from being reported as reusable.
    if isinstance(old_reused, list):
        for item in old_reused:
            if isinstance(item, Mapping):
                name = item.get("logical_name", item.get("path", item.get("file")))
            else:
                name = item
            if isinstance(name, str) and name in new_files and name not in reused_files:
                reused_files.append(name)
    reused_files.sort()
    unchanged_classes = sorted(
        name for name, digest in new_resources.items()
        if old_resources.get(name) == digest
    )
    changed_classes = sorted(
        name for name, digest in new_resources.items()
        if old_resources.get(name) != digest
    )
    return {
        "mode": mode,
        "changed_resources": changed_classes,
        "added_resources": sorted(set(new_resources) - set(old_resources)),
        "removed_resources": sorted(set(old_resources) - set(new_resources)),
        "unchanged_resources": unchanged_classes,
        "reused_files": reused_files,
    }


def select_evidence(
    resources: Mapping[str, Any],
    *,
    mode: str = "NORMAL",
    max_resource_classes: int = MAX_RESOURCE_CLASSES,
    overflow: str = "compress",
) -> dict[str, Any]:
    """Select at most nine classes and explicitly account for any overflow.

    ``compress`` keeps all overflow values in one named resource rather than
    silently dropping them.  ``reject`` raises with the exact class names.
    """

    if not isinstance(resources, Mapping):
        raise StageContextError("resources must be an object")
    if not isinstance(max_resource_classes, int) or isinstance(max_resource_classes, bool):
        raise StageContextError("max_resource_classes must be an integer")
    if max_resource_classes < 1 or max_resource_classes > MAX_RESOURCE_CLASSES:
        raise StageContextError(f"max_resource_classes must be between 1 and {MAX_RESOURCE_CLASSES}")
    mode = _text(mode, "mode", required=True, max_length=32).upper()
    if mode not in {"NORMAL", "FRESH"}:
        raise StageContextError("mode must be NORMAL or FRESH")
    overflow = _text(overflow, "overflow", required=True, max_length=32).lower()
    if overflow not in {"compress", "reject"}:
        raise StageContextError("overflow must be compress or reject")
    cleaned: dict[str, Any] = {}
    fresh_excluded: list[str] = []
    for raw_name, value in resources.items():
        name = _text(raw_name, "resource class", required=True, max_length=128)
        if name.lower() in FRESH_FORBIDDEN and mode == "FRESH":
            fresh_excluded.append(name)
            continue
        if mode == "FRESH":
            cleaned[name] = _fresh_value(value, f"resources.{name}", fresh_excluded)
        else:
            cleaned[name] = _safe_value(value, f"resources.{name}")
    if len(cleaned) <= max_resource_classes:
        selected = cleaned
        compressed: dict[str, Any] = {}
    else:
        ordered_names = list(cleaned)
        priority = FRESH_PRIORITY if mode == "FRESH" else NORMAL_PRIORITY
        ordered_names.sort(key=lambda name: (priority.index(name) if name in priority else len(priority), name))
        if overflow == "reject":
            raise StageContextError(
                "resource class limit exceeded; explicit classes require compression or refusal: "
                + ", ".join(ordered_names)
            )
        keep_count = max_resource_classes - 1
        keep_names = ordered_names[:keep_count]
        selected = {name: cleaned[name] for name in keep_names}
        overflow_names = [name for name in ordered_names if name not in selected]
        compressed = {
            "source_classes": overflow_names,
            "values": {name: cleaned[name] for name in overflow_names},
            "compression": "explicit_nested_resource_class",
        }
        selected["compressed_evidence"] = compressed
    selection = {
        "mode": mode,
        "max_resource_classes": max_resource_classes,
        "overflow": overflow,
        "selected_classes": list(selected),
        "compressed_classes": list(compressed.get("source_classes", [])),
        "fresh_excluded_classes": sorted(fresh_excluded),
    }
    return {"resources": selected, "selection": selection}


def build_stage_context(
    contract: Mapping[str, Any] | None = None,
    state: Mapping[str, Any] | None = None,
    *,
    plan_id: str | None = None,
    stage_id: str | None = None,
    project_goal: str | None = None,
    stage: Any = None,
    user_visible_goal: str | None = None,
    established_facts: Any = None,
    current_method: Any = None,
    baseline_summary: Any = None,
    latest_result: Any = None,
    current_blocker: Any = None,
    rejected_directions: Any = None,
    protected_constraints: Any = None,
    recent_decisions: Any = None,
    artifact_refs: Any = None,
    git_baseline: Mapping[str, Any] | None = None,
    resources: Mapping[str, Any] | None = None,
    mode: str = "NORMAL",
    previous_context: Mapping[str, Any] | None = None,
    max_resource_classes: int = MAX_RESOURCE_CLASSES,
    overflow: str = "compress",
    task_id: str | None = None,
    iteration_index: int = 0,
) -> dict[str, Any]:
    """Build the bounded machine context and its Markdown source fields."""

    contract = dict(contract or {})
    state = dict(state or {})
    mode = _text(mode, "mode", required=True, max_length=32).upper()
    if mode not in {"NORMAL", "FRESH"}:
        raise StageContextError("mode must be NORMAL or FRESH")
    # Contract/state are convenience inputs; explicit arguments take priority.
    project_goal = project_goal if project_goal is not None else contract.get("project_goal", state.get("project_goal", ""))
    stage = stage if stage is not None else {
        "stage_id": contract.get("stage_id", state.get("stage_id", "")),
        "stage_name": contract.get("stage_name", state.get("stage_name", "")),
        "status": state.get("status", contract.get("status", "")),
    }
    user_visible_goal = user_visible_goal if user_visible_goal is not None else contract.get("user_visible_goal", state.get("user_visible_goal", ""))
    baseline_summary = baseline_summary if baseline_summary is not None else state.get("baseline", contract.get("baseline", {}))
    latest_result = latest_result if latest_result is not None else state.get("latest_result", {})
    protected_constraints = protected_constraints if protected_constraints is not None else {
        "protected_paths": contract.get("protected_paths", []),
        "allowed_paths": contract.get("allowed_paths", []),
    }
    current_method = current_method if current_method is not None else state.get("current_method", contract.get("stage_scope", ""))
    established_facts = established_facts if established_facts is not None else state.get("established_facts", [])
    current_blocker = current_blocker if current_blocker is not None else state.get("current_blocker", "")
    rejected_directions = rejected_directions if rejected_directions is not None else state.get("rejected_directions", [])
    recent_decisions = recent_decisions if recent_decisions is not None else state.get("decisions", state.get("recent_decisions", []))
    artifact_refs = artifact_refs if artifact_refs is not None else state.get("artifact_refs", state.get("review_artifacts", []))
    git_baseline = git_baseline if git_baseline is not None else state.get("git_baseline", {})
    if mode == "FRESH":
        # Never inherit recommendation/route narrative from a previous pack.
        if isinstance(previous_context, Mapping):
            old_selection = previous_context.get("selection", {})
            if isinstance(old_selection, Mapping):
                old_excluded = old_selection.get("fresh_excluded_classes", [])
                _ = old_excluded  # no previous values are copied into FRESH
        recent_decisions = []
        rejected_directions = _as_list(rejected_directions, "rejected_directions")
    fresh_excluded_fields: list[str] = []
    fields: dict[str, Any] = {
        "project_goal": _text(project_goal, "project_goal", required=True),
        "stage": _safe_value(stage, "stage"),
        "user_visible_goal": _text(user_visible_goal, "user_visible_goal", required=True),
        "established_facts": _as_list(established_facts, "established_facts"),
        "current_method": _safe_value(current_method, "current_method"),
        "baseline_summary": _safe_value(baseline_summary, "baseline_summary"),
        "latest_result": _safe_value(latest_result, "latest_result"),
        "current_blocker": _safe_value(current_blocker, "current_blocker"),
        "rejected_directions": _as_list(rejected_directions, "rejected_directions"),
        "protected_constraints": _safe_value(protected_constraints, "protected_constraints"),
        "recent_decisions": _as_list(recent_decisions, "recent_decisions"),
        "artifact_refs": _as_list(artifact_refs, "artifact_refs"),
        "git_baseline": _safe_value(git_baseline or {}, "git_baseline"),
    }
    if mode == "FRESH":
        for field_name, value in list(fields.items()):
            fields[field_name] = _fresh_value(value, field_name, fresh_excluded_fields)
    default_resources: dict[str, Any] = {
        "stage_context": fields,
        "latest_result": fields["latest_result"],
        "metrics": state.get("metrics", {}),
        "code_or_diff": state.get("code_or_diff", state.get("changed_files", [])),
        "review_artifacts": fields["artifact_refs"],
        "baseline": fields["baseline_summary"],
        "stage_contract": contract,
        "decision_history": fields["recent_decisions"],
        "safety": fields["protected_constraints"],
    }
    supplied_resources = default_resources if resources is None else dict(resources)
    if not isinstance(iteration_index, int) or isinstance(iteration_index, bool) or iteration_index < 0:
        raise StageContextError("iteration_index must be a non-negative integer")
    selected = select_evidence(
        supplied_resources,
        mode=mode,
        max_resource_classes=max_resource_classes,
        overflow=overflow,
    )
    if fresh_excluded_fields:
        selected["selection"]["fresh_excluded_classes"].extend(fresh_excluded_fields)
        selected["selection"]["fresh_excluded_classes"] = sorted(
            set(selected["selection"]["fresh_excluded_classes"])
        )
    manifest = _manifest_for_resources(selected["resources"])
    delta = _delta(previous_context, manifest, mode=mode)
    resolved_plan_id = plan_id if plan_id is not None else contract.get("plan_id", state.get("plan_id", "stage-plan"))
    resolved_stage_id = stage_id if stage_id is not None else contract.get("stage_id", state.get("stage_id", stage.get("stage_id", "stage")) or "stage")
    body: dict[str, Any] = {
        "context_version": "stage_context.v1",
        "pack_version": "stage_context_pack.v2",
        "plan_id": _text(resolved_plan_id, "plan_id", required=True, max_length=256),
        "stage_id": _text(resolved_stage_id, "stage_id", required=True, max_length=256),
        "task_id": _text(task_id or state.get("task_id", "stage-context"), "task_id", required=True, max_length=256),
        "iteration_index": iteration_index,
        "mode": mode,
        **fields,
        "resources": selected["resources"],
        # Canonical JSON sorts object keys.  Store the class list in that same
        # deterministic order so a persisted context verifies byte-for-byte
        # after a load/restart.
        "resource_classes": sorted(selected["resources"]),
        "resource_count": len(selected["resources"]),
        "selection": selected["selection"],
        "manifest": manifest,
        "delta": delta,
        "fresh_isolation_verified": mode == "FRESH" and not any(
            key in selected["resources"] for key in FRESH_FORBIDDEN
        ),
    }
    body["digest"] = _digest(body)
    if len(canonical_json(body).encode("utf-8")) > MAX_CONTEXT_JSON_BYTES:
        raise StageContextError("stage context JSON exceeds the bounded size")
    validate_against_schema(body, "stage_context")
    return body


def render_stage_context_markdown(context: Mapping[str, Any]) -> str:
    """Render a bounded, secret-free human-readable context document."""

    checked = verify_stage_context(context)
    lines = [
        "# STAGE_CONTEXT",
        "",
        f"- Context version: `{checked['context_version']}`",
        f"- Mode: `{checked['mode']}`",
        f"- Plan: `{checked['plan_id']}`",
        f"- Stage: `{checked['stage_id']}`",
        f"- Iteration: `{checked['iteration_index']}`",
        "",
    ]
    labels = {
        "project_goal": "Project goal",
        "stage": "Current Stage",
        "user_visible_goal": "User-visible goal",
        "established_facts": "Established facts",
        "current_method": "Current method",
        "baseline_summary": "Baseline summary",
        "latest_result": "Latest result",
        "current_blocker": "Current blocker",
        "rejected_directions": "Rejected directions",
        "protected_constraints": "Protected constraints",
        "recent_decisions": "Recent important decisions",
        "artifact_refs": "Artifact refs",
        "git_baseline": "Git baseline",
    }
    for key in CONTEXT_FIELDS:
        lines.append(f"## {labels[key]}")
        lines.append("")
        value = checked[key]
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)
        # Keep the document bounded even if a caller supplied many small facts.
        if len(rendered) > 12000:
            raise StageContextError(f"rendered context field exceeds bound: {key}")
        lines.extend(["```json", rendered, "```", ""])
    lines.extend(
        [
            "## Evidence selection",
            "",
            f"Selected resource classes: {', '.join(checked['resource_classes'])}",
            f"Compressed resource classes: {', '.join(checked['selection']['compressed_classes']) or 'none'}",
            f"NORMAL reused files: {', '.join(checked['delta']['reused_files']) or 'none'}",
            "",
            f"Context digest: `{checked['digest']}`",
            f"Manifest digest: `{checked['manifest']['manifest_digest']}`",
            "",
        ]
    )
    rendered = "\n".join(lines)
    if len(rendered.encode("utf-8")) > MAX_CONTEXT_MARKDOWN_BYTES:
        raise StageContextError("STAGE_CONTEXT.md exceeds the bounded size")
    return rendered


def verify_stage_context(context: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(context, Mapping):
        raise StageContextError("stage context must be an object")
    checked = copy.deepcopy(dict(context))
    validate_against_schema(checked, "stage_context")
    supplied_digest = checked.pop("digest", None)
    manifest = checked.get("manifest")
    if not isinstance(manifest, Mapping):
        raise StageContextError("stage context manifest is missing")
    manifest_body = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    if manifest.get("manifest_digest") != _digest(manifest_body):
        raise StageContextError("stage context manifest digest mismatch")
    expected = _digest(checked)
    if not isinstance(supplied_digest, str) or supplied_digest != expected:
        raise StageContextError("stage context digest mismatch")
    selected = checked.get("resources", {})
    if checked.get("resource_count") != len(selected) or checked.get("resource_classes") != sorted(selected):
        raise StageContextError("resource class count/list mismatch")
    if len(selected) > MAX_RESOURCE_CLASSES:
        raise StageContextError("stage context exceeds the nine resource-class bound")
    actual_manifest = _manifest_for_resources(selected)
    if actual_manifest != manifest:
        raise StageContextError("stage context resource manifest mismatch")
    if checked.get("mode") == "FRESH" and not checked.get("fresh_isolation_verified"):
        raise StageContextError("FRESH context is not isolated")
    return {**checked, "digest": expected}


def write_stage_context(
    context: Mapping[str, Any],
    destination: str | os.PathLike[str],
    *,
    overwrite: bool = False,
) -> dict[str, str]:
    """Write JSON, Markdown and manifest files without replacing history."""

    checked = verify_stage_context(context)
    target = Path(destination).expanduser().resolve()
    if target.suffix.lower() == ".md":
        directory = target.parent
        markdown_path = target
    else:
        directory = target
        markdown_path = directory / "STAGE_CONTEXT.md"
    directory.mkdir(parents=True, exist_ok=True)
    files = {
        "markdown": markdown_path,
        "json": directory / "stage_context.json",
        "manifest": directory / "stage_context.manifest.json",
    }
    payloads = {
        files["markdown"]: render_stage_context_markdown(checked),
        files["json"]: canonical_json(checked) + "\n",
        files["manifest"]: canonical_json(checked["manifest"]) + "\n",
    }
    for path, payload in payloads.items():
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise StageContextError(f"cannot read existing context file: {path.name}") from exc
            if existing != payload:
                raise StageContextError(
                    f"refusing to overwrite existing context iteration: {path.name}"
                )
            continue
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except OSError as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise StageContextError(f"cannot write context file: {path.name}") from exc
    return {key: str(path) for key, path in files.items()}


def load_stage_context(
    source: str | os.PathLike[str],
    *,
    check_markdown: bool = True,
) -> dict[str, Any]:
    """Load and fail closed on JSON/manifest (and optionally Markdown) tamper."""

    path = Path(source).expanduser().resolve()
    directory = path.parent if path.suffix.lower() == ".json" else path
    json_path = directory / "stage_context.json"
    manifest_path = directory / "stage_context.manifest.json"
    markdown_path = directory / "STAGE_CONTEXT.md"
    try:
        context = json.loads(json_path.read_text(encoding="utf-8"))
        persisted_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StageContextError("stage context files are unreadable") from exc
    checked = verify_stage_context(context)
    if persisted_manifest != checked["manifest"]:
        raise StageContextError("persisted stage context manifest mismatch")
    if check_markdown:
        try:
            if markdown_path.read_text(encoding="utf-8") != render_stage_context_markdown(checked):
                raise StageContextError("persisted STAGE_CONTEXT.md does not match context JSON")
        except OSError as exc:
            raise StageContextError("STAGE_CONTEXT.md is unreadable") from exc
    return checked


# Naming aliases make the adapter easy to use alongside the Step 5 builder.
build_context_pack = build_stage_context
build_stage_context_pack = build_stage_context
verify_context_pack = verify_stage_context


__all__ = [
    "CONTEXT_FIELDS",
    "MAX_CONTEXT_JSON_BYTES",
    "MAX_CONTEXT_MARKDOWN_BYTES",
    "MAX_RESOURCE_CLASSES",
    "StageContextError",
    "build_context_pack",
    "build_stage_context",
    "build_stage_context_pack",
    "load_stage_context",
    "render_stage_context_markdown",
    "select_evidence",
    "verify_context_pack",
    "verify_stage_context",
    "write_stage_context",
]
