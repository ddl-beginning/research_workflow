"""Deterministic, bounded project context compaction (Step 12).

Only an explicitly approved ``PROJECT_BRIEF.json`` can produce
``.research/PROJECT_CONTEXT.md``.  The context is a compact, machine-readable
view of user-owned goals and explicitly selected references; it is not a
transcript, repository dump, or project/workflow manager.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .artifact_retention import (
    ARTIFACT_CLASSES,
    ARTIFACT_RETENTION_MANIFEST_RELATIVE_PATH,
    ArtifactClass,
    ArtifactRetentionError,
    build_retention_manifest,
    classify_artifact,
    load_retention_manifest,
    save_retention_manifest,
)
from .contracts import ContractValidationError, canonical_json, load_schema, sha256_json, validate_instance
from .project_intake import ProjectIntakeError, validate_project_brief
from .project_state import PROJECT_BRIEF_RELATIVE_PATH, ProjectStateError, load_project_brief, resolve_project_root


PROJECT_CONTEXT_SCHEMA_VERSION = "project_context.v1"
PROJECT_CONTEXT_RELATIVE_PATH = Path(".research") / "PROJECT_CONTEXT.md"
PROJECT_CONTEXT_FILENAME = PROJECT_CONTEXT_RELATIVE_PATH.name
PROJECT_CONTEXT_MARKER = "PROJECT_CONTEXT_COMPACTION_PASS"
MAX_MAJOR_RESOURCES = 9
MAX_RESOURCE_BYTES = 256 * 1024


class ProjectContextError(RuntimeError):
    """A fail-closed context generation/validation error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


_SENSITIVE_KEYS = frozenset(
    {
        "cookie", "cookies", "token", "tokens", "authorization", "password", "passwd",
        "secret", "api_key", "apikey", "access_token", "auth_token", "client_secret",
        "session", "session_id", "session_storage", "storage_state", "raw_dom", "dom",
        "prompt", "raw_response",
        "transcript", "conversation", "messages", "turns", "chat_history", "raw_history",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_FORBIDDEN_PATH_PARTS = frozenset({".auth", "credentials", "secrets", "secret", "tokens"})

# The context is a bounded hand-off record, not a transcript.  These keys are
# rejected even when nested so a caller cannot accidentally persist a chat log
# under an innocuous-looking extension field.
_TRANSCRIPT_KEYS = frozenset({"transcript", "conversation", "messages", "turns", "chat_history", "raw_history"})
_SEMANTIC_FIELDS = (
    "target",
    "inputs",
    "outputs",
    "user_visible_success",
    "constraints",
    "non_goals",
    "preferences",
    "decisions",
    "status",
    "selected_route",
    "rejected_routes",
    "unresolved_questions",
    "next_action",
    "artifact_refs",
    "git_identity",
)
_SEMANTIC_ALIASES = {
    "true_target": "target",
    "real_target": "target",
    "project_target": "target",
    "input_output": "inputs",
    "input_output_refs": "inputs",
    "user_visible_success_criteria": "user_visible_success",
    "rejected_route": "rejected_routes",
    "rejected_direction": "rejected_routes",
    "selected_direction": "selected_route",
    "route_selected": "selected_route",
    "unresolved": "unresolved_questions",
    "question": "unresolved_questions",
    "next": "next_action",
    "artifact_references": "artifact_refs",
}


def _error(code: str, message: str, cause: BaseException | None = None) -> ProjectContextError:
    result = ProjectContextError(code, message)
    if cause is not None:
        result.__cause__ = cause
    return result


def _safe_value(value: Any, path: str = "$", depth: int = 0) -> None:
    if depth > 12:
        raise _error("CONTEXT_INPUT_TOO_DEEP", "context input is too deeply nested")
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_name = str(key).strip().lower().replace("-", "_")
            if key_name in _SENSITIVE_KEYS:
                raise _error("CONTEXT_SECRET_REJECTED", f"sensitive field at {path}")
            _safe_value(child, f"{path}.{key}", depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _safe_value(child, f"{path}[{index}]", depth + 1)
        return
    if isinstance(value, str):
        if "\x00" in value or len(value) > MAX_RESOURCE_BYTES:
            raise _error("CONTEXT_INPUT_INVALID", f"unsafe text at {path}")
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise _error("CONTEXT_SECRET_REJECTED", f"secret-like value at {path}")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise _error("CONTEXT_INPUT_INVALID", f"unsupported value at {path}")


def _relative_path(value: str | os.PathLike[str], field: str = "path") -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise _error("CONTEXT_PATH_INVALID", f"{field} must be a relative path")
    raw = os.fspath(value).replace("\\", "/")
    if not raw or "\x00" in raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise _error("CONTEXT_PATH_INVALID", f"{field} must be relative")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _error("CONTEXT_PATH_INVALID", f"{field} contains traversal or empty components")
    if any(
        part.casefold() in (_FORBIDDEN_PATH_PARTS | {".env"}) or part.casefold().startswith(".env.")
        for part in parts
    ):
        raise _error("CONTEXT_SENSITIVE_PATH", f"{field} points to a protected path")
    return "/".join(parts)


def _read_resource(root: Path, path: str) -> bytes:
    candidate = (root / Path(*path.split("/"))).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise _error("CONTEXT_PATH_OUTSIDE_PROJECT", "resource path escapes project root", exc)
    if not candidate.exists():
        raise _error("CONTEXT_RESOURCE_NOT_FOUND", f"selected resource is not present: {path}")
    if not candidate.is_file() or candidate.is_symlink():
        raise _error("CONTEXT_RESOURCE_INVALID", f"selected resource is not a regular file: {path}")
    try:
        size = candidate.stat().st_size
        if size > MAX_RESOURCE_BYTES:
            raise _error("CONTEXT_RESOURCE_TOO_LARGE", f"selected resource exceeds bounded size: {path}")
        data = candidate.read_bytes()
    except ProjectContextError:
        raise
    except OSError as exc:
        raise _error("CONTEXT_RESOURCE_UNREADABLE", f"selected resource cannot be read: {path}", exc)
    if any(pattern.search(data.decode("utf-8", errors="replace")) for pattern in _SECRET_PATTERNS):
        raise _error("CONTEXT_SECRET_REJECTED", f"secret-like content in selected resource: {path}")
    return data


def _brief_digest(brief: Mapping[str, Any]) -> str:
    # Keep this independent of the filesystem and timestamps outside the
    # canonical brief content.  The brief validator already rejects secrets.
    return sha256_json(dict(brief))


def _resource_input(raw: Mapping[str, Any] | str, index: int) -> tuple[str, str | None, str | None, str | None]:
    if isinstance(raw, str):
        path = raw
        return _relative_path(path, f"resources[{index}].path"), None, None, None
    if not isinstance(raw, Mapping):
        raise _error("CONTEXT_RESOURCE_INVALID", f"resources[{index}] must be an object or path")
    raw_path = raw.get("path", raw.get("relative_path"))
    if not isinstance(raw_path, str):
        raise _error("CONTEXT_PATH_INVALID", f"resources[{index}] needs a relative path")
    path = _relative_path(raw_path, f"resources[{index}].path")
    name = raw.get("name", raw.get("logical_name"))
    summary = raw.get("summary", raw.get("description"))
    content = raw.get("content")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise _error("CONTEXT_RESOURCE_INVALID", f"resources[{index}].name must be non-empty text")
    if summary is not None and not isinstance(summary, str):
        raise _error("CONTEXT_RESOURCE_INVALID", f"resources[{index}].summary must be text")
    if content is not None and not isinstance(content, str):
        raise _error("CONTEXT_RESOURCE_INVALID", f"resources[{index}].content must be text")
    artifact_class = raw.get("class", raw.get("artifact_class"))
    if artifact_class is not None and not isinstance(artifact_class, str):
        raise _error("CONTEXT_RESOURCE_INVALID", f"resources[{index}].class must be text")
    return path, name.strip() if isinstance(name, str) else None, summary.strip() if isinstance(summary, str) else None, artifact_class


def _normalise_resources(root: Path, resources: Sequence[Mapping[str, Any] | str] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    descriptors: list[dict[str, Any]] = []
    retention_descriptors: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    seen_names: set[str] = set()
    for index, raw in enumerate(resources or []):
        path, name, summary, artifact_class = _resource_input(raw, index)
        if path in seen_paths:
            raise _error("CONTEXT_DUPLICATE_RESOURCE", f"resource path is duplicated: {path}")
        selected_name = name or Path(path).name
        if selected_name in seen_names:
            raise _error("CONTEXT_DUPLICATE_RESOURCE", f"resource name is duplicated: {selected_name}")
        seen_paths.add(path)
        seen_names.add(selected_name)
        if len(descriptors) >= MAX_MAJOR_RESOURCES:
            raise _error("CONTEXT_RESOURCE_LIMIT", f"at most {MAX_MAJOR_RESOURCES} major resources are allowed")
        if isinstance(raw, Mapping) and raw.get("content") is not None:
            content = str(raw["content"]).encode("utf-8")
            if len(content) > MAX_RESOURCE_BYTES:
                raise _error("CONTEXT_RESOURCE_TOO_LARGE", f"resource exceeds bounded size: {path}")
            text = content.decode("utf-8")
            _safe_value(text, f"resources[{index}].content")
        else:
            content = _read_resource(root, path)
            text = content.decode("utf-8", errors="replace")
        # Resource references may intentionally point at project source/data;
        # only workflow retention entries are restricted to ``.research``.
        # Keep the same class vocabulary without pretending source files are
        # workflow-owned cleanup targets.
        try:
            selected_class = classify_artifact(path, artifact_class=artifact_class)
        except ArtifactRetentionError as exc:
            raise _error(exc.code, str(exc), exc)
        descriptor: dict[str, Any] = {
            "name": selected_name,
            "class": selected_class,
            "path": path,
            "digest": hashlib.sha256(content).hexdigest(),
        }
        if summary:
            _safe_value(summary, f"resources[{index}].summary")
            descriptor["summary"] = summary[:1200]
        descriptors.append(descriptor)
        # All explicit references are retained in the manifest.  The retention
        # module still treats source/data as ACTIVE_REFERENCE and cleanup can
        # only act on workflow-owned EPHEMERAL paths.
        retention_descriptors.append({"path": path, "class": selected_class, "name": selected_name})
    descriptors.sort(key=lambda item: (item["name"], item["path"]))
    retention_descriptors.sort(key=lambda item: item["path"])
    return descriptors, retention_descriptors


def _brief_view(brief: Mapping[str, Any]) -> dict[str, Any]:
    value = brief.get("brief") if isinstance(brief.get("brief"), Mapping) else {}
    # Keep only user-goal material and bounded extension fields.  The full
    # canonical brief remains on disk; this view is what is safe to hand to a
    # downstream bounded consultation.
    result: dict[str, Any] = {}
    allowed = (
        "title", "problem_statement", "goal", "desired_outcome", "priority",
        "success_criteria", "acceptance_criteria", "constraints", "non_goals",
        "scope", "stakeholders", "preferences", "chatgpt_project_url",
        "chatgpt_project_binding",
    )
    for key in allowed:
        if key in value:
            result[key] = copy.deepcopy(value[key])
    return result


def _render_value(value: Any) -> str:
    """Render a value without relying on Python mapping insertion order."""

    if isinstance(value, str):
        return value.replace("\r", " ").replace("\n", " ").strip()
    return canonical_json(value)


def _brief_value(brief: Mapping[str, Any], key: str) -> Any:
    value = brief.get("brief") if isinstance(brief.get("brief"), Mapping) else {}
    if key in value:
        return value[key]
    # A few callers keep Step 11-compatible extension fields at the document
    # root.  Read them without copying the entire intake document.
    return brief.get(key)


def _normalise_semantic_context(
    brief: Mapping[str, Any],
    resources: Sequence[Mapping[str, Any]],
    supplied: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the small, explicit hand-off record retained by Step 12.

    Missing values remain present as empty values so consumers can distinguish
    "not supplied" from an accidentally omitted schema field.  No transcript,
    repository scan, or route inference is performed here.
    """

    supplied_value = dict(supplied or {})
    aliases = {
        **_SEMANTIC_ALIASES,
        "goal": "target",
        "project_goal": "target",
        "project_target": "target",
        "input": "inputs",
        "input_refs": "inputs",
        "output": "outputs",
        "output_refs": "outputs",
        "success": "user_visible_success",
        "user_visible_outcome": "user_visible_success",
        "rejected_route": "rejected_routes",
        "rejected_directions": "rejected_routes",
        "selected_direction": "selected_route",
        "next_step": "next_action",
        "artifacts": "artifact_refs",
        "git_baseline": "git_identity",
        "git_identity_ref": "git_identity",
    }
    for alias, canonical in aliases.items():
        if alias in supplied_value and canonical not in supplied_value:
            supplied_value[canonical] = supplied_value[alias]
    if "rejected_routes" not in supplied_value:
        reason = supplied_value.get("rejected_route_short_reason", supplied_value.get("rejected_route_reason"))
        evidence = supplied_value.get("rejected_route_evidence_ref", supplied_value.get("evidence_ref"))
        if reason is not None or evidence is not None:
            supplied_value["rejected_routes"] = {
                key: value for key, value in (("short_reason", reason), ("evidence_ref", evidence))
                if value is not None
            }
    if supplied_value:
        _safe_value(supplied_value, "context")

    brief_fields = brief.get("brief") if isinstance(brief.get("brief"), Mapping) else {}
    defaults: dict[str, Any] = {
        "target": _brief_value(brief, "goal") or _brief_value(brief, "desired_outcome") or "",
        "inputs": [],
        "outputs": [],
        "user_visible_success": _brief_value(brief, "success_criteria")
        or _brief_value(brief, "acceptance_criteria")
        or [],
        "constraints": _brief_value(brief, "constraints") or [],
        "non_goals": _brief_value(brief, "non_goals") or [],
        "preferences": _brief_value(brief, "preferences") or [],
        "decisions": [],
        "status": brief.get("status", brief.get("state", "APPROVED")),
        "selected_route": {},
        "rejected_routes": [],
        "unresolved_questions": [],
        "next_action": brief.get("next_action", "APPROVED"),
        "artifact_refs": [copy.deepcopy(item) for item in resources],
        "git_identity": {},
    }
    # Preserve explicitly authored Step 11 extension fields when they already
    # use one of the Step 12 semantic names.
    for key in _SEMANTIC_FIELDS:
        if key in brief:
            defaults[key] = copy.deepcopy(brief[key])
        elif isinstance(brief_fields, Mapping) and key in brief_fields:
            defaults[key] = copy.deepcopy(brief_fields[key])
    explicit_identity = supplied_value.get("git_identity")
    if explicit_identity is None:
        explicit_identity = brief.get("git_identity") or brief.get("git_baseline")
    if explicit_identity is None:
        identity = brief.get("project_identity")
        if isinstance(identity, Mapping):
            # This is a local project identity, not a Git command or a
            # discovery result; it provides stable provenance for this hand-off.
            explicit_identity = {
                key: copy.deepcopy(identity[key])
                for key in ("project_id", "repository_root", "identity_method")
                if key in identity
            }
    if explicit_identity is not None:
        defaults["git_identity"] = copy.deepcopy(explicit_identity)
    for key in _SEMANTIC_FIELDS:
        if key in supplied_value:
            defaults[key] = copy.deepcopy(supplied_value[key])
    _safe_value(defaults, "context")
    return defaults


def _validate_artifact_ref_paths(value: Any, field: str = "artifact_refs") -> None:
    """Reject absolute/traversing local artifact refs while allowing URLs."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            key_name = str(key).strip().lower().replace("-", "_")
            if key_name in {"path", "relative_path", "file", "filename"} and isinstance(child, str):
                if "://" not in child and not child.startswith("data:"):
                    _relative_path(child, f"{field}.{key_name}")
            _validate_artifact_ref_paths(child, f"{field}.{key_name}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_artifact_ref_paths(child, f"{field}[{index}]")
    elif isinstance(value, str) and (value.startswith("/") or re.match(r"^[A-Za-z]:", value)):
        if "://" not in value and not value.startswith("data:"):
            raise _error("CONTEXT_PATH_INVALID", f"{field} must use project-relative paths")


def _markdown(
    *,
    project_id: str,
    brief_revision: int,
    brief: Mapping[str, Any],
    resources: Sequence[Mapping[str, Any]],
    retention: Mapping[str, Sequence[str]],
    semantic: Mapping[str, Any],
) -> str:
    view = _brief_view(brief)
    lines = [
        "# Project Context",
        "",
        f"- schema_version: `{PROJECT_CONTEXT_SCHEMA_VERSION}`",
        f"- project_id: `{project_id}`",
        f"- brief_revision: `{brief_revision}`",
        "- brief_state: `APPROVED`",
        "- context_policy: bounded, deterministic, user-approved brief only",
        "",
        "## Approved project brief",
        "",
    ]
    for key in (
        "title", "problem_statement", "goal", "desired_outcome", "priority",
        "success_criteria", "acceptance_criteria", "constraints", "non_goals",
        "scope", "stakeholders", "preferences",
    ):
        if key not in view:
            continue
        value = view[key]
        if isinstance(value, list):
            lines.append(f"- {key}:")
            for item in value:
                lines.append(f"  - {_render_value(item)}")
        else:
            text = _render_value(value)
            if text:
                lines.append(f"- {key}: {text}")
    for key in ("chatgpt_project_url", "chatgpt_project_binding"):
        if key in view:
            # These optional Step 11 compatibility fields are copied only when
            # explicitly present; no ChatGPT connector is contacted here.
            lines.append(f"- {key}: {_render_value(view[key])}")
    if not any(line.startswith("- ") for line in lines[lines.index("## Approved project brief") + 2:]):
        lines.append("- (no additional brief fields)")
    lines.extend(["", "## Selected bounded resources", ""])
    if resources:
        for item in resources:
            summary = f" — {item['summary']}" if item.get("summary") else ""
            lines.append(
                f"- `{item['name']}` [{item['class']}] `{item['path']}` "
                f"(sha256 `{item['digest']}`){summary}"
            )
    else:
        lines.append("- (none; no repository files were implicitly scanned)")
    lines.extend(["", "## Bounded project hand-off", ""])
    for key in _SEMANTIC_FIELDS:
        value = semantic.get(key)
        lines.append(f"- {key}: {_render_value(value)}")
    lines.extend(["", "## Retention semantics", ""])
    for key, label in (
        ("canonical", "CANONICAL"),
        ("milestone_evidence", "MILESTONE_EVIDENCE"),
        ("active_reference", "ACTIVE_REFERENCE"),
        ("ephemeral", "EPHEMERAL"),
    ):
        values = list(retention.get(key, []))
        lines.append(f"- {label}: {', '.join(f'`{item}`' for item in values) if values else '(none)'}")
    lines.extend(
        [
            "",
            "## Safety boundary",
            "",
            "Only workflow-owned EPHEMERAL paths may be cleaned. Source, data, "
            "business files, Git history, canonical records, and milestone evidence "
            "are retained.",
            "",
            f"<!-- marker: {PROJECT_CONTEXT_MARKER} -->",
            "",
        ]
    )
    return "\n".join(lines)


def project_context_path(project_root: str | os.PathLike[str] = ".") -> Path:
    root = resolve_project_root(project_root)
    return root / PROJECT_CONTEXT_RELATIVE_PATH


def _atomic_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return path
    except (OSError, UnicodeError):
        # Fall through to the atomic write and classify any failure below.
        pass
    fd: int | None = None
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            handle.write(text)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise _error("CONTEXT_WRITE_FAILED", "could not atomically write project context", exc)
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
    return path


def _approved_brief(root: Path, brief: Mapping[str, Any] | None) -> dict[str, Any]:
    if brief is None:
        try:
            loaded = load_project_brief(root)
        except ProjectStateError as exc:
            raise _error("BRIEF_UNREADABLE", "canonical project brief could not be loaded", exc)
        if loaded is None:
            raise _error("BRIEF_NOT_FOUND", "an approved project brief is required")
        brief = loaded
    if not isinstance(brief, Mapping):
        raise _error("BRIEF_INVALID", "project brief must be an object")
    try:
        checked = validate_project_brief(brief, root)
    except ProjectIntakeError as exc:
        if getattr(exc, "code", "") in {"PROJECT_IDENTITY_MISMATCH", "BRIEF_INVALID"}:
            raise _error("BRIEF_INVALID", "project brief failed validation", exc)
        raise _error("BRIEF_INVALID", "project brief failed validation", exc)
    if checked.get("state") != "APPROVED" or checked.get("status") != "APPROVED":
        raise _error("BRIEF_NOT_APPROVED", "project context requires state=APPROVED")
    return checked


def build_project_context(
    project_root: str | os.PathLike[str] = ".",
    *,
    brief: Mapping[str, Any] | None = None,
    resources: Sequence[Mapping[str, Any] | str] | None = None,
    active_references: Sequence[Mapping[str, Any] | str] | None = None,
    milestone_evidence: Sequence[Mapping[str, Any] | str] | None = None,
    ephemeral: Sequence[Mapping[str, Any] | str] | None = None,
    context: Mapping[str, Any] | None = None,
    snapshot: Mapping[str, Any] | None = None,
    **semantic_fields: Any,
) -> dict[str, Any]:
    """Build and persist one deterministic ``PROJECT_CONTEXT.md``.

    The named class arguments are convenience aliases for callers that want to
    make retention intent explicit.  They do not trigger repository scanning.
    ``resources`` is the complete selected list; class-specific arguments are
    appended to it and are still subject to the nine-resource bound.
    """

    root = resolve_project_root(project_root)
    checked_brief = _approved_brief(root, brief)
    selected: list[Mapping[str, Any] | str] = list(resources or [])
    for item, artifact_class in (
        *((item, ArtifactClass.ACTIVE_REFERENCE.value) for item in (active_references or [])),
        *((item, ArtifactClass.MILESTONE_EVIDENCE.value) for item in (milestone_evidence or [])),
        *((item, ArtifactClass.EPHEMERAL.value) for item in (ephemeral or [])),
    ):
        if isinstance(item, str):
            selected.append({"path": item, "class": artifact_class})
        elif isinstance(item, Mapping):
            enriched = dict(item)
            enriched.setdefault("class", artifact_class)
            selected.append(enriched)
        else:
            raise _error("CONTEXT_RESOURCE_INVALID", "selected resource must be a path or object")
    if len(selected) > MAX_MAJOR_RESOURCES:
        raise _error("CONTEXT_RESOURCE_LIMIT", f"at most {MAX_MAJOR_RESOURCES} major resources are allowed")
    try:
        resource_descriptors, retention_descriptors = _normalise_resources(root, selected)
    except ArtifactRetentionError as exc:
        raise _error(exc.code, str(exc), exc)
    # Canonical records are always retained and are not counted as selected
    # resources sent to GPT.  The context itself is a canonical artifact.
    retention_items: list[dict[str, Any]] = [
        {"path": PROJECT_BRIEF_RELATIVE_PATH.as_posix(), "class": ArtifactClass.CANONICAL.value},
        {"path": PROJECT_CONTEXT_RELATIVE_PATH.as_posix(), "class": ArtifactClass.CANONICAL.value},
        {"path": ARTIFACT_RETENTION_MANIFEST_RELATIVE_PATH.as_posix(), "class": ArtifactClass.CANONICAL.value},
        *retention_descriptors,
    ]
    supplied_semantic: dict[str, Any] = {}
    for source in (snapshot, context):
        if source is not None:
            if not isinstance(source, Mapping):
                raise _error("CONTEXT_INPUT_INVALID", "context/snapshot must be an object")
            supplied_semantic.update(copy.deepcopy(dict(source)))
    supplied_semantic.update(semantic_fields)
    # Accept a single ``data``/``fields`` wrapper used by a few small callers,
    # while keeping arbitrary unknown keys fail-closed rather than persisting
    # unbounded metadata.
    for wrapper_name in ("data", "fields", "semantic"):
        wrapper = supplied_semantic.pop(wrapper_name, None)
        if wrapper is not None:
            if not isinstance(wrapper, Mapping):
                raise _error("CONTEXT_INPUT_INVALID", f"{wrapper_name} must be an object")
            merged_wrapper = dict(wrapper)
            merged_wrapper.update(supplied_semantic)
            supplied_semantic = merged_wrapper
    known_semantic_keys = set(_SEMANTIC_FIELDS) | set(_SEMANTIC_ALIASES) | {
        "goal", "project_goal", "project_target", "input", "input_refs", "output",
        "output_refs", "success", "user_visible_outcome", "rejected_route",
        "rejected_directions", "selected_direction", "next_step", "artifacts",
        "git_baseline", "git_identity_ref", "rejected_route_short_reason",
        "rejected_route_reason", "rejected_route_evidence_ref", "evidence_ref",
    }
    unknown = sorted(set(supplied_semantic) - known_semantic_keys)
    if unknown:
        raise _error("CONTEXT_FIELD_INVALID", f"unsupported context fields: {', '.join(unknown)}")
    semantic = _normalise_semantic_context(checked_brief, resource_descriptors, supplied_semantic)
    _validate_artifact_ref_paths(semantic.get("artifact_refs"))
    try:
        retention_manifest = build_retention_manifest(
            retention_items,
            project_id=str(checked_brief["project_id"]),
            brief_digest=_brief_digest(checked_brief),
            context_resource_count=len(resource_descriptors),
        )
    except ArtifactRetentionError as exc:
        raise _error(exc.code, str(exc), exc)
    retention_by_class = {name.lower(): [] for name in ARTIFACT_CLASSES}
    for item in retention_manifest["artifacts"]:
        retention_by_class[item["class"].lower()].append(item["path"])
    markdown = _markdown(
        project_id=str(checked_brief["project_id"]),
        brief_revision=int(checked_brief.get("revision", 1)),
        brief=checked_brief,
        resources=resource_descriptors,
        retention=retention_by_class,
        semantic=semantic,
    )
    body: dict[str, Any] = {
        "schema_version": PROJECT_CONTEXT_SCHEMA_VERSION,
        "project_id": str(checked_brief["project_id"]),
        "brief_digest": _brief_digest(checked_brief),
        "brief_revision": int(checked_brief.get("revision", 1)),
        "status": "PROJECT_CONTEXT_READY",
        "resources": resource_descriptors,
        "brief": _brief_view(checked_brief),
        "retention": retention_by_class,
        "context": copy.deepcopy(semantic),
        "content": copy.deepcopy(semantic),
        "context_digest": "",
        "markdown": markdown,
    }
    # Keep semantic fields discoverable at the top level for lightweight
    # consumers, except ``status`` which is reserved for the context envelope.
    for field_name, field_value in semantic.items():
        if field_name != "status":
            body[field_name] = copy.deepcopy(field_value)
    # The digest is the semantic digest of the canonical Markdown payload.  It
    # is stable across equivalent invocations and can be verified without a
    # second context-version file.
    body["context_digest"] = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    try:
        validate_instance(body, load_schema("project_context"))
    except ContractValidationError as exc:
        raise _error("CONTEXT_SCHEMA_INVALID", "generated project context failed its schema", exc)
    # Bind the side manifest to the canonical Markdown so a one-file edit is
    # detected on the next load.  This is not a second context version file.
    try:
        retention_manifest = build_retention_manifest(
            retention_items,
            project_id=str(checked_brief["project_id"]),
            brief_digest=_brief_digest(checked_brief),
            context_digest=body["context_digest"],
            context_resource_count=len(resource_descriptors),
        )
    except ArtifactRetentionError as exc:
        raise _error(exc.code, str(exc), exc)
    # Write both canonical outputs atomically.  Rewriting equal bytes is
    # intentional and idempotent; no versioned sibling files are created.
    _atomic_text(project_context_path(root), markdown)
    try:
        save_retention_manifest(root, retention_manifest)
    except ArtifactRetentionError as exc:
        raise _error(exc.code, str(exc), exc)
    return {
        "schema_version": body["schema_version"],
        "project_id": body["project_id"],
        "brief_digest": body["brief_digest"],
        "brief_revision": body["brief_revision"],
        "status": body["status"],
        "resources": copy.deepcopy(body["resources"]),
        "brief": copy.deepcopy(body["brief"]),
        "retention": copy.deepcopy(body["retention"]),
        "context": copy.deepcopy(body["context"]),
        "content": copy.deepcopy(body["content"]),
        "context_digest": body["context_digest"],
        "context_resource_count": len(resource_descriptors),
        "markdown": markdown,
        "path": PROJECT_CONTEXT_RELATIVE_PATH.as_posix(),
        "retention_manifest_path": ARTIFACT_RETENTION_MANIFEST_RELATIVE_PATH.as_posix(),
        "retention_manifest_digest": retention_manifest["digest"],
        "marker": PROJECT_CONTEXT_MARKER,
    }


def generate_project_context(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return build_project_context(*args, **kwargs)


def compact_project_context(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return build_project_context(*args, **kwargs)


def write_project_context(project_root: str | os.PathLike[str], context: Mapping[str, Any] | str) -> Path:
    """Write a previously built context after validating its digest/schema."""

    root = resolve_project_root(project_root)
    if isinstance(context, str):
        # Raw markdown cannot prove approval or digest, so reject it rather
        # than creating an unbound canonical context.
        raise _error("CONTEXT_UNBOUND", "write_project_context requires a built context object")
    if not isinstance(context, Mapping):
        raise _error("CONTEXT_INVALID", "context must be an object")
    detached = copy.deepcopy(dict(context))
    detached.pop("path", None)
    detached.pop("retention_manifest_path", None)
    detached.pop("retention_manifest_digest", None)
    detached.pop("marker", None)
    detached.pop("retention_manifest", None)
    try:
        validate_instance(detached, load_schema("project_context"))
    except ContractValidationError as exc:
        raise _error("CONTEXT_SCHEMA_INVALID", "context failed schema validation", exc)
    approved = _approved_brief(root, None)
    if detached.get("project_id") != approved.get("project_id") or detached.get("brief_digest") != _brief_digest(approved):
        raise _error("CONTEXT_BRIEF_DIGEST_MISMATCH", "context is not bound to the current approved brief")
    expected = hashlib.sha256(str(detached["markdown"]).encode("utf-8")).hexdigest()
    if expected != detached.get("context_digest"):
        raise _error("CONTEXT_DIGEST_MISMATCH", "context digest is inconsistent")
    semantic = detached.get("context", detached.get("content", {}))
    if not isinstance(semantic, Mapping):
        raise _error("CONTEXT_INVALID", "context semantic payload must be an object")
    _safe_value(semantic, "context")
    _validate_artifact_ref_paths(semantic.get("artifact_refs"))
    retention = detached.get("retention", {})
    if not isinstance(retention, Mapping):
        raise _error("CONTEXT_INVALID", "retention payload must be an object")
    retention_items: list[dict[str, Any]] = []
    for class_name in ARTIFACT_CLASSES:
        values = retention.get(class_name.lower(), [])
        if not isinstance(values, list):
            raise _error("CONTEXT_INVALID", f"retention.{class_name.lower()} must be an array")
        for value in values:
            retention_items.append({"path": value, "class": class_name})
    try:
        manifest = build_retention_manifest(
            retention_items,
            project_id=str(approved["project_id"]),
            brief_digest=_brief_digest(approved),
            context_digest=expected,
            context_resource_count=int(detached.get("context_resource_count", len(detached.get("resources", [])))),
        )
    except (ArtifactRetentionError, TypeError, ValueError) as exc:
        raise _error("CONTEXT_INVALID", "context retention payload is invalid", exc)
    _atomic_text(project_context_path(root), str(detached["markdown"]))
    try:
        save_retention_manifest(root, manifest)
    except ArtifactRetentionError as exc:
        raise _error(exc.code, str(exc), exc)
    return project_context_path(root)


def load_project_context(project_root: str | os.PathLike[str] = ".") -> dict[str, Any]:
    """Load and verify the canonical markdown context against its side manifest.

    The JSON sidecar is deliberately not a second context version; it is the
    retention manifest that validates the canonical Markdown's policy.
    """

    root = resolve_project_root(project_root)
    path = project_context_path(root)
    if path.is_symlink() or not path.is_file():
        raise _error("CONTEXT_NOT_FOUND", "canonical project context does not exist")
    try:
        markdown = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise _error("CONTEXT_UNREADABLE", "canonical project context cannot be read", exc)
    if PROJECT_CONTEXT_MARKER not in markdown:
        raise _error("CONTEXT_MARKER_MISSING", "canonical project context marker is missing")
    manifest = load_retention_manifest(root)
    if manifest is None:
        raise _error("RETENTION_MANIFEST_NOT_FOUND", "retention manifest is required for context verification")
    brief = _approved_brief(root, None)
    expected_brief_digest = _brief_digest(brief)
    if manifest.get("brief_digest") != expected_brief_digest:
        raise _error("CONTEXT_BRIEF_DIGEST_MISMATCH", "retention manifest is not bound to the approved brief")
    context_digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    if manifest.get("context_digest") != context_digest:
        raise _error("CONTEXT_DIGEST_MISMATCH", "canonical project context does not match its retention manifest")
    project_line = re.search(r"(?m)^- project_id: `([^`]+)`$", markdown)
    if project_line is None or project_line.group(1) != str(brief["project_id"]):
        raise _error("CONTEXT_PROJECT_ID_MISMATCH", "canonical project context is bound to another project")
    revision_line = re.search(r"(?m)^- brief_revision: `([0-9]+)`$", markdown)
    if revision_line is None or int(revision_line.group(1)) != int(brief.get("revision", 1)):
        raise _error("CONTEXT_BRIEF_REVISION_MISMATCH", "canonical project context is stale for the approved brief")
    # Recover the compact metadata from stable lines.  Full reconstruction is
    # unnecessary for consumers; verification mainly ensures the canonical
    # artifact is present, bounded and bound to this approved brief.
    if len(markdown.encode("utf-8")) > 120_000:
        raise _error("CONTEXT_TOO_LARGE", "canonical project context exceeds bounded size")
    return {
        "schema_version": PROJECT_CONTEXT_SCHEMA_VERSION,
        "project_id": str(brief["project_id"]),
        "brief_digest": expected_brief_digest,
        "brief_revision": int(brief.get("revision", 1)),
        "status": "PROJECT_CONTEXT_READY",
        "markdown": markdown,
        "context_digest": context_digest,
        "context_resource_count": manifest.get("context_resource_count", 0),
        "retention_manifest": manifest,
        "path": PROJECT_CONTEXT_RELATIVE_PATH.as_posix(),
        "marker": PROJECT_CONTEXT_MARKER,
    }


def verify_project_context(project_root: str | os.PathLike[str] = ".") -> dict[str, Any]:
    loaded = load_project_context(project_root)
    return {
        "passed": True,
        "marker": PROJECT_CONTEXT_MARKER,
        "path": loaded["path"],
        "context_digest": loaded["context_digest"],
        "brief_digest": loaded["brief_digest"],
        "resource_count": int(
            loaded["retention_manifest"].get(
                "context_resource_count", loaded.get("context_resource_count", 0)
            )
        ),
    }


# Verb-first and short aliases keep the independent layer convenient for
# scripts without coupling it to the Step controller or browser bridge.
build_context = build_project_context
compact_context = compact_project_context
generate_context = generate_project_context
save_context = write_project_context
load_context = load_project_context
verify_context = verify_project_context


# Compatibility spellings for small callers and acceptance scripts.
PROJECT_CONTEXT_PATH = PROJECT_CONTEXT_RELATIVE_PATH
MAX_RESOURCE_CLASSES = MAX_MAJOR_RESOURCES
ProjectContext = build_project_context
ProjectContextBuilder = build_project_context
validate_context = verify_project_context


__all__ = [
    "MAX_MAJOR_RESOURCES",
    "MAX_RESOURCE_CLASSES",
    "PROJECT_CONTEXT_FILENAME",
    "PROJECT_CONTEXT_MARKER",
    "PROJECT_CONTEXT_PATH",
    "PROJECT_CONTEXT_RELATIVE_PATH",
    "PROJECT_CONTEXT_SCHEMA_VERSION",
    "ProjectContext",
    "ProjectContextBuilder",
    "ProjectContextError",
    "build_project_context",
    "build_context",
    "compact_context",
    "compact_project_context",
    "generate_project_context",
    "generate_context",
    "load_context",
    "load_project_context",
    "project_context_path",
    "save_context",
    "verify_context",
    "verify_project_context",
    "validate_context",
    "write_project_context",
]
