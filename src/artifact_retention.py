"""Small, fail-closed artifact retention policy for Step 12.

The workflow deliberately does not try to infer ownership from every file in a
repository.  Callers classify files explicitly (or use one of the very small
workflow-owned ephemeral directories) and this module only removes those
ephemeral paths.  Canonical records, milestone evidence, source/data, and Git
metadata are never eligible for cleanup.

This is intentionally a plain module rather than a ProjectManager/database.
The manifest is useful provenance, but is not required for loading a project
brief or for running a Stage.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import ContractValidationError, canonical_json, sha256_json, validate_against_schema
from .project_state import resolve_project_root


ARTIFACT_RETENTION_SCHEMA_VERSION = "artifact_retention.v1"
ARTIFACT_RETENTION_MANIFEST_RELATIVE_PATH = Path(".research") / "ARTIFACT_RETENTION_MANIFEST.json"
ARTIFACT_CLASSES = ("CANONICAL", "MILESTONE_EVIDENCE", "ACTIVE_REFERENCE", "EPHEMERAL")
MAX_ARTIFACTS = 256


class ArtifactClass(str, Enum):
    CANONICAL = "CANONICAL"
    MILESTONE_EVIDENCE = "MILESTONE_EVIDENCE"
    ACTIVE_REFERENCE = "ACTIVE_REFERENCE"
    EPHEMERAL = "EPHEMERAL"


class ArtifactRetentionError(RuntimeError):
    """A bounded artifact policy or file-safety failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


# These are only directory names owned by this workflow.  A directory called
# ``cache`` elsewhere in a repository is deliberately not touched.
WORKFLOW_EPHEMERAL_DIRECTORIES = frozenset(
    {
        ".research/ephemeral",
        ".research/staging",
        ".research/.staging",
        ".research/tmp",
        ".research/.tmp",
        ".research/discovery/cache",
        ".research/blueprint/staging",
    }
)
_SENSITIVE_KEYS = frozenset(
    {
        "cookie", "cookies", "token", "tokens", "authorization", "password",
        "passwd", "secret", "api_key", "apikey", "access_token", "auth_token",
        "client_secret", "session", "session_id", "session_storage", "storage_state",
        "raw_dom", "dom", "prompt", "raw_response",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_SENSITIVE_PATH_PARTS = frozenset(
    {
        ".auth",
        ".env",
        "credentials",
        "passwords",
        "secrets",
        "secret",
        "tokens",
    }
)
_FORBIDDEN_METADATA_KEYS = _SENSITIVE_KEYS | frozenset(
    {"transcript", "conversation", "messages", "turns", "chat_history", "raw_history"}
)


def _safe_value(value: Any, path: str = "$", depth: int = 0) -> None:
    if depth > 12:
        raise ArtifactRetentionError("RETENTION_INPUT_TOO_DEEP", "artifact metadata is too deeply nested")
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_name = str(key).strip().lower().replace("-", "_")
            if key_name in _FORBIDDEN_METADATA_KEYS:
                raise ArtifactRetentionError("RETENTION_SECRET_REJECTED", f"sensitive field at {path}")
            _safe_value(child, f"{path}.{key}", depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _safe_value(child, f"{path}[{index}]", depth + 1)
        return
    if isinstance(value, str):
        if "\x00" in value or len(value) > 16_000:
            raise ArtifactRetentionError("RETENTION_INPUT_INVALID", f"unsafe text at {path}")
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise ArtifactRetentionError("RETENTION_SECRET_REJECTED", f"secret-like value at {path}")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise ArtifactRetentionError("RETENTION_INPUT_INVALID", f"unsupported value at {path}")


def _relative(value: str | os.PathLike[str], field: str = "path") -> str:
    if not isinstance(value, (str, os.PathLike)):
        raise ArtifactRetentionError("RETENTION_PATH_INVALID", f"{field} must be a relative path")
    raw = os.fspath(value).replace("\\", "/")
    if not raw or "\x00" in raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ArtifactRetentionError("RETENTION_PATH_INVALID", f"{field} must be relative")
    parts = raw.split("/")
    if any(part in {"..", ".", ""} for part in parts):
        raise ArtifactRetentionError("RETENTION_PATH_INVALID", f"{field} contains traversal")
    normalized = "/".join(parts)
    if normalized == "." or normalized == "":
        raise ArtifactRetentionError("RETENTION_PATH_INVALID", f"{field} is empty")
    return normalized


def _under(relative: str, directory: str) -> bool:
    return relative == directory or relative.startswith(directory.rstrip("/") + "/")


def _ensure_research_relative(relative: str) -> str:
    normalized = _relative(relative)
    if not _under(normalized, ".research"):
        raise ArtifactRetentionError(
            "RETENTION_PATH_OUTSIDE_WORKFLOW",
            "artifact retention paths must be inside .research",
        )
    return normalized


def _ensure_project_relative(relative: str) -> str:
    """Validate a repository-relative reference without making it a cleanup
    target.  Source/data and external milestone evidence may be referenced;
    only workflow-owned ``.research`` paths are cleanup candidates.
    """

    normalized = _relative(relative)
    for part in normalized.split("/"):
        lowered = part.casefold()
        if lowered in _SENSITIVE_PATH_PARTS or lowered.startswith(".env."):
            raise ArtifactRetentionError("RETENTION_SENSITIVE_PATH", "artifact path points to protected material")
    return normalized


def artifact_path(root: str | os.PathLike[str], relative: str) -> Path:
    """Resolve a workflow path without following it outside the project."""

    project = resolve_project_root(root)
    checked = _ensure_project_relative(relative)
    candidate = (project / Path(*checked.split("/"))).resolve(strict=False)
    try:
        candidate.relative_to(project / ".research")
    except ValueError as exc:
        raise ArtifactRetentionError("RETENTION_PATH_OUTSIDE_WORKFLOW", "path escapes .research") from exc
    return candidate


def classify_artifact(relative: str, *, artifact_class: str | ArtifactClass | None = None) -> str:
    """Classify a path using explicit class first and conservative defaults."""

    checked = _ensure_project_relative(relative)
    if artifact_class is not None:
        value = artifact_class.value if isinstance(artifact_class, ArtifactClass) else str(artifact_class).strip().upper()
        if value not in ARTIFACT_CLASSES:
            raise ArtifactRetentionError("RETENTION_CLASS_INVALID", "unknown artifact class")
        if value == ArtifactClass.CANONICAL.value and not _under(checked, ".research"):
            raise ArtifactRetentionError("RETENTION_CANONICAL_SCOPE", "canonical artifacts must be inside .research")
        if value == ArtifactClass.EPHEMERAL.value and not is_workflow_ephemeral_path(checked):
            raise ArtifactRetentionError("RETENTION_EPHEMERAL_SCOPE", "only workflow-owned paths may be ephemeral")
        return value
    if is_workflow_ephemeral_path(checked):
        return ArtifactClass.EPHEMERAL.value
    if _under(checked, ".research") and (
        checked.endswith("PROJECT_BRIEF.json") or checked.endswith("PROJECT_CONTEXT.md")
    ):
        return ArtifactClass.CANONICAL.value
    if "/milestones/" in f"/{checked}/" or "/integration/" in f"/{checked}/":
        return ArtifactClass.MILESTONE_EVIDENCE.value
    return ArtifactClass.ACTIVE_REFERENCE.value


def is_workflow_ephemeral_path(relative: str) -> bool:
    checked = _relative(relative)
    return any(_under(checked, directory) for directory in WORKFLOW_EPHEMERAL_DIRECTORIES)


def _descriptor(relative: str, item: Mapping[str, Any] | None = None, *, artifact_class: str | None = None) -> dict[str, Any]:
    checked = _ensure_project_relative(relative)
    selected_class = classify_artifact(checked, artifact_class=artifact_class)
    result: dict[str, Any] = {"path": checked, "class": selected_class}
    if item:
        metadata = {
            str(key): copy.deepcopy(value)
            for key, value in item.items()
            if str(key) not in {"path", "relative_path", "class"}
        }
        _safe_value(metadata, path=f"artifact[{checked}]")
        result.update(metadata)
    return result


def build_retention_manifest(
    artifacts: Sequence[Mapping[str, Any] | str] | None = None,
    *,
    project_id: str | None = None,
    brief_digest: str | None = None,
    context_digest: str | None = None,
    context_resource_count: int | None = None,
) -> dict[str, Any]:
    """Build a deterministic artifact-class manifest.

    The manifest has no timestamp.  Therefore its digest is stable when the
    logical inputs are stable, even when the file is rewritten atomically.
    """

    if project_id is not None and (not isinstance(project_id, str) or len(project_id) > 200):
        raise ArtifactRetentionError("RETENTION_PROJECT_ID_INVALID", "project_id must be bounded text")
    if brief_digest is not None and (not isinstance(brief_digest, str) or len(brief_digest) > 128):
        raise ArtifactRetentionError("RETENTION_BRIEF_DIGEST_INVALID", "brief_digest must be bounded text")
    if context_digest is not None and (
        not isinstance(context_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", context_digest)
    ):
        raise ArtifactRetentionError("RETENTION_CONTEXT_DIGEST_INVALID", "context_digest must be a SHA-256 digest")
    if context_resource_count is not None and (
        not isinstance(context_resource_count, int)
        or isinstance(context_resource_count, bool)
        or context_resource_count < 0
        or context_resource_count > 9
    ):
        raise ArtifactRetentionError("RETENTION_RESOURCE_COUNT_INVALID", "context_resource_count must be 0..9")
    entries: list[dict[str, Any]] = []
    for raw in artifacts or []:
        if isinstance(raw, str):
            entries.append(_descriptor(raw))
        elif isinstance(raw, Mapping):
            path_value = raw.get("path", raw.get("relative_path"))
            if not isinstance(path_value, str):
                raise ArtifactRetentionError("RETENTION_PATH_INVALID", "artifact descriptor needs path")
            entries.append(_descriptor(path_value, raw, artifact_class=raw.get("class")))
        else:
            raise ArtifactRetentionError("RETENTION_INPUT_INVALID", "artifact descriptors must be objects or paths")
    if len(entries) > MAX_ARTIFACTS:
        raise ArtifactRetentionError("RETENTION_ARTIFACT_LIMIT", "too many artifact descriptors")
    entries.sort(key=lambda item: (str(item["class"]), str(item["path"]), canonical_json(item)))
    if len({item["path"] for item in entries}) != len(entries):
        raise ArtifactRetentionError("RETENTION_DUPLICATE_PATH", "an artifact path appears more than once")
    body: dict[str, Any] = {
        "schema_version": ARTIFACT_RETENTION_SCHEMA_VERSION,
        "project_id": project_id or "",
        "brief_digest": brief_digest or "",
        "artifacts": entries,
    }
    if context_digest is not None:
        body["context_digest"] = context_digest
    if context_resource_count is not None:
        body["context_resource_count"] = context_resource_count
    _safe_value(body)
    body["digest"] = sha256_json(body)
    return body


def validate_retention_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise ArtifactRetentionError("RETENTION_MANIFEST_INVALID", "manifest must be an object")
    detached = copy.deepcopy(dict(manifest))
    try:
        validate_against_schema(detached, "artifact_retention")
    except ContractValidationError as exc:
        raise ArtifactRetentionError("RETENTION_MANIFEST_INVALID", "manifest failed schema validation") from exc
    expected = dict(detached)
    digest = expected.pop("digest", None)
    if not isinstance(digest, str) or digest != sha256_json(expected):
        raise ArtifactRetentionError("RETENTION_DIGEST_MISMATCH", "retention manifest digest is inconsistent")
    # Re-run path/class policy, including the ephemeral ownership restriction.
    build = build_retention_manifest(
        detached.get("artifacts", []),
        project_id=detached.get("project_id", ""),
        brief_digest=detached.get("brief_digest", ""),
        context_digest=detached.get("context_digest"),
        context_resource_count=detached.get("context_resource_count"),
    )
    if build["digest"] != digest:
        raise ArtifactRetentionError("RETENTION_DIGEST_MISMATCH", "retention artifact entries are inconsistent")
    return detached


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_text = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == payload_text:
            return path
    except (OSError, UnicodeError):
        # Fall through to the atomic write; the write path provides the stable
        # error classification if the destination is not readable.
        pass
    temporary: str | None = None
    fd: int | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            handle.write(payload_text)
        os.replace(temporary, path)
        temporary = None
    except (OSError, TypeError, ValueError) as exc:
        raise ArtifactRetentionError("RETENTION_WRITE_FAILED", "could not atomically write retention manifest") from exc
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


def retention_manifest_path(root: str | os.PathLike[str]) -> Path:
    project = resolve_project_root(root)
    return project / ARTIFACT_RETENTION_MANIFEST_RELATIVE_PATH


def save_retention_manifest(root: str | os.PathLike[str], manifest: Mapping[str, Any]) -> Path:
    checked = validate_retention_manifest(manifest)
    return _atomic_json(retention_manifest_path(root), checked)


def load_retention_manifest(root: str | os.PathLike[str]) -> dict[str, Any] | None:
    path = retention_manifest_path(root)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ArtifactRetentionError("RETENTION_MANIFEST_UNREADABLE", "retention manifest is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactRetentionError("RETENTION_MANIFEST_UNREADABLE", "retention manifest could not be loaded") from exc
    return validate_retention_manifest(value)


def cleanup_ephemeral(
    root: str | os.PathLike[str],
    paths: Iterable[str] | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove only explicitly named workflow-owned ephemeral paths.

    If ``paths`` is omitted, only the known workflow-owned directories are
    considered.  Symlinks are unlinked as links; their targets are never
    followed or removed.  No path outside ``.research`` is accepted.
    """

    project = resolve_project_root(root)
    if paths is None:
        requested_paths: Iterable[str] = WORKFLOW_EPHEMERAL_DIRECTORIES
    elif isinstance(paths, (str, os.PathLike)):
        requested_paths = [os.fspath(paths)]
    else:
        requested_paths = paths
    selected = sorted({_relative(item, "ephemeral path") for item in requested_paths})
    removed: list[str] = []
    skipped: list[str] = []
    for relative in selected:
        if not is_workflow_ephemeral_path(relative):
            raise ArtifactRetentionError("RETENTION_EPHEMERAL_SCOPE", "cleanup target is not workflow-owned ephemeral")
        # Keep the final component un-followed so a workflow-owned symlink can
        # be unlinked safely even when its target is outside the repository.
        target = project / Path(*relative.split("/"))
        research_root = (project / ".research").resolve()
        try:
            target.parent.resolve(strict=False).relative_to(research_root)
        except ValueError as exc:
            raise ArtifactRetentionError("RETENTION_PATH_OUTSIDE_WORKFLOW", "cleanup target escapes .research") from exc
        if not target.exists() and not target.is_symlink():
            skipped.append(relative)
            continue
        if not dry_run:
            try:
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.is_dir():
                    try:
                        target.resolve(strict=True).relative_to(research_root)
                    except ValueError as exc:
                        raise ArtifactRetentionError("RETENTION_PATH_OUTSIDE_WORKFLOW", "cleanup target escapes .research") from exc
                    shutil.rmtree(target)
                else:
                    raise ArtifactRetentionError("RETENTION_TARGET_INVALID", "cleanup target is not a regular file/directory")
            except OSError as exc:
                raise ArtifactRetentionError("RETENTION_CLEANUP_FAILED", "ephemeral cleanup failed") from exc
        removed.append(relative)
    return {
        "schema_version": "artifact_retention_cleanup.v1",
        "dry_run": bool(dry_run),
        "removed": removed,
        "skipped": skipped,
        "protected": ["CANONICAL", "MILESTONE_EVIDENCE", "ACTIVE_REFERENCE", "source", "data", ".git"],
    }


class ArtifactRetention:
    """Small object facade for callers that prefer a stateful API."""

    def __init__(self, project_root: str | os.PathLike[str] = ".") -> None:
        self.root = resolve_project_root(project_root)

    def classify(self, path: str, artifact_class: str | ArtifactClass | None = None) -> str:
        return classify_artifact(path, artifact_class=artifact_class)

    def build_manifest(self, artifacts: Sequence[Mapping[str, Any] | str] | None = None, **kwargs: Any) -> dict[str, Any]:
        return build_retention_manifest(artifacts, **kwargs)

    def save_manifest(self, manifest: Mapping[str, Any]) -> Path:
        return save_retention_manifest(self.root, manifest)

    def load_manifest(self) -> dict[str, Any] | None:
        return load_retention_manifest(self.root)

    def cleanup(self, paths: Iterable[str] | None = None, *, dry_run: bool = False) -> dict[str, Any]:
        return cleanup_ephemeral(self.root, paths, dry_run=dry_run)


# Friendly aliases for integrations and hidden/older clients.
ArtifactRetentionPolicy = ArtifactRetention
RetentionClass = ArtifactClass
classify = classify_artifact
build_manifest = build_retention_manifest
validate_manifest = validate_retention_manifest
cleanup_workflow_ephemeral = cleanup_ephemeral


__all__ = [
    "ARTIFACT_CLASSES",
    "ARTIFACT_RETENTION_MANIFEST_RELATIVE_PATH",
    "ARTIFACT_RETENTION_SCHEMA_VERSION",
    "ArtifactClass",
    "ArtifactRetention",
    "ArtifactRetentionError",
    "ArtifactRetentionPolicy",
    "MAX_ARTIFACTS",
    "WORKFLOW_EPHEMERAL_DIRECTORIES",
    "artifact_path",
    "build_manifest",
    "build_retention_manifest",
    "classify",
    "classify_artifact",
    "cleanup_ephemeral",
    "cleanup_workflow_ephemeral",
    "is_workflow_ephemeral_path",
    "load_retention_manifest",
    "retention_manifest_path",
    "save_retention_manifest",
    "validate_manifest",
    "validate_retention_manifest",
]
