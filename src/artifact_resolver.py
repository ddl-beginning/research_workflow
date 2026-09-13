"""Explicit placement and ownership resolver for Workflow V2 Product.

The resolver is deliberately independent of the V2 reducer.  It maps new
artifacts to one owner root, validates the path before every write, and emits a
small manifest suitable for durable evidence.  It never moves, renames,
deletes, or dual-writes an artifact.
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


RESOLVER_VERSION = "artifact-resolver.v1"
OWNERSHIPS = frozenset({"ENGINE", "PROJECT", "HISTORY", "TEMP"})
_ENGINE_TOP_LEVELS = frozenset({"src", "schemas", "templates", "scripts", "tests", "examples", "docs", "skills"})
_GUIDANCE_KINDS = frozenset({"constitution", "spec", "plan", "tasks", "closeout"})
_HISTORY_PREFIXES = {
    "planning": ".research/planning",
    "planning_evidence": ".research/planning",
    "gpt_review": ".research/reviews",
    "gpt_review_receipt": ".research/reviews",
    "integration": ".research/integration",
    "integration_receipt": ".research/integration",
    "verification": ".research/verification",
    "verification_receipt": ".research/verification",
    "stage_evidence": ".research/evidence",
}
_GLOB_CHARS = frozenset("*?[]{}")


class ArtifactResolverError(ValueError):
    """Fail-closed resolver error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _root(value: str | os.PathLike[str], field: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not os.fspath(value):
        raise ArtifactResolverError("ROOT_INVALID", f"{field} is required")
    path = Path(os.fspath(value)).expanduser()
    if not path.is_absolute():
        raise ArtifactResolverError("ROOT_MUST_BE_ABSOLUTE", f"{field} must be absolute")
    resolved = path.resolve(strict=False)
    if resolved.exists() and not resolved.is_dir():
        raise ArtifactResolverError("ROOT_NOT_DIRECTORY", f"{field} must be a directory")
    return resolved


def _identity(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x00" in value:
        raise ArtifactResolverError("IDENTITY_INVALID", f"{field} must be bounded text")
    return value.strip()


def _safe_relative(value: Any, field: str = "relative_path") -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ArtifactResolverError("PATH_INVALID", f"{field} must be non-empty text")
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or raw.startswith("//") or re.match(r"^[A-Za-z]:", raw):
        raise ArtifactResolverError("PATH_ABSOLUTE", f"{field} must be relative")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts) or any(ch in raw for ch in _GLOB_CHARS):
        raise ArtifactResolverError("PATH_TRAVERSAL", f"{field} contains unsafe segments")
    return "/".join(parts)


def _stage_id(value: Any) -> str:
    checked = _safe_relative(value, "stage_id")
    if "/" in checked or "\\" in checked:
        raise ArtifactResolverError("STAGE_ID_INVALID", "stage_id must be one path segment")
    return checked


def _operation_id(value: Any) -> str:
    checked = _safe_relative(value, "operation_id")
    if "/" in checked or "\\" in checked:
        raise ArtifactResolverError("OPERATION_ID_INVALID", "operation_id must be one path segment")
    return checked


def _inside(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _guard_existing_links(root: Path, target: Path) -> None:
    """Reject symlink/reparse components instead of following them."""
    if not _inside(root, target):
        raise ArtifactResolverError("PATH_OUTSIDE_ROOT", "resolved artifact is outside its owner root")
    current = root
    relative = target.relative_to(root)
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                raise ArtifactResolverError("SYMLINK_ESCAPE", "artifact path contains a symlink/reparse point")
            attributes = getattr(current.stat(follow_symlinks=False), "st_file_attributes", 0)
            is_reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            if is_reparse:
                raise ArtifactResolverError("SYMLINK_ESCAPE", "artifact path contains a symlink/reparse point")
        except FileNotFoundError:
            # New artifact parents are inspected when they are created by the
            # atomic writer; a not-yet-existing leaf is safe to resolve.
            continue
        except OSError as exc:
            raise ArtifactResolverError("PATH_UNREADABLE", "artifact path could not be inspected") from exc


@dataclass(frozen=True)
class WorkspaceIdentity:
    workspace_id: str
    project_id: str
    project_root: Path
    engine_root: Path
    machine_runtime_root: Path

    @classmethod
    def from_roots(
        cls,
        *,
        workspace_id: str,
        project_id: str,
        project_root: str | os.PathLike[str],
        engine_root: str | os.PathLike[str],
        machine_runtime_root: str | os.PathLike[str] | None = None,
    ) -> "WorkspaceIdentity":
        if machine_runtime_root is None:
            if os.name == "nt":
                base = os.environ.get("LOCALAPPDATA")
                if not base:
                    raise ArtifactResolverError("MACHINE_RUNTIME_ROOT_REQUIRED", "LOCALAPPDATA is unavailable")
                machine_runtime_root = Path(base) / "ResearchWorkflow"
            else:
                base = os.environ.get("XDG_STATE_HOME") or os.environ.get("XDG_CACHE_HOME")
                if not base:
                    base = str(Path.home() / ".local" / "state")
                machine_runtime_root = Path(base) / "research-workflow"
        return cls(
            workspace_id=_identity(workspace_id, "workspace_id"),
            project_id=_identity(project_id, "project_id"),
            project_root=_root(project_root, "project_root"),
            engine_root=_root(engine_root, "engine_root"),
            machine_runtime_root=_root(machine_runtime_root, "machine_runtime_root"),
        )


@dataclass(frozen=True)
class Placement:
    artifact_kind: str
    ownership: str
    source_operation: str
    workspace_id: str
    project_id: str
    resolved_root: str
    relative_path: str
    absolute_path: Path
    resolver_version: str = RESOLVER_VERSION
    durable: bool = True

    def manifest(self) -> dict[str, Any]:
        return {
            "artifact_kind": self.artifact_kind,
            "ownership": self.ownership,
            "source_operation": self.source_operation,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "resolved_root": self.resolved_root,
            "relative_path": self.relative_path,
            "resolver_version": self.resolver_version,
            "durable": self.durable,
        }


class ArtifactResolver:
    def __init__(self, identity: WorkspaceIdentity) -> None:
        self.identity = identity

    @classmethod
    def from_roots(cls, **kwargs: Any) -> "ArtifactResolver":
        return cls(WorkspaceIdentity.from_roots(**kwargs))

    def _check_identity(self, workspace_id: str | None, project_id: str | None) -> None:
        if workspace_id is not None and workspace_id != self.identity.workspace_id:
            raise ArtifactResolverError("WORKSPACE_IDENTITY_MISMATCH", "workspace_id does not match resolver identity")
        if project_id is not None and project_id != self.identity.project_id:
            raise ArtifactResolverError("PROJECT_IDENTITY_MISMATCH", "project_id does not match resolver identity")

    def resolve(
        self,
        artifact_kind: str,
        ownership: str,
        *,
        source_operation: str,
        relative_path: str | None = None,
        stage_id: str | None = None,
        operation_id: str | None = None,
        workspace_id: str | None = None,
        project_id: str | None = None,
    ) -> Placement:
        kind = _identity(artifact_kind, "artifact_kind")
        owner = _identity(ownership, "ownership").upper()
        if owner not in OWNERSHIPS:
            raise ArtifactResolverError("OWNERSHIP_INVALID", "ownership must be ENGINE, PROJECT, HISTORY, or TEMP")
        operation = _identity(source_operation, "source_operation")
        self._check_identity(workspace_id, project_id)
        checked_stage = _stage_id(stage_id) if stage_id is not None else None
        checked_operation = _operation_id(operation_id) if operation_id is not None else None
        if owner == "ENGINE":
            rel = _safe_relative(relative_path, "relative_path")
            if rel.split("/", 1)[0] not in _ENGINE_TOP_LEVELS:
                raise ArtifactResolverError("ENGINE_PATH_INVALID", "engine artifact is outside the reusable engine tree")
            root, durable = self.identity.engine_root, True
        elif owner == "PROJECT":
            if kind in _GUIDANCE_KINDS:
                if kind == "constitution":
                    rel = ".specify/memory/constitution.md"
                else:
                    if checked_stage is None:
                        raise ArtifactResolverError("STAGE_ID_REQUIRED", "guidance artifact requires stage_id")
                    rel = f"specs/{checked_stage}/{kind}.md"
            elif kind in {"project_identity", "project_brief"}:
                rel = ".research/PROJECT_BRIEF.json"
            else:
                rel = _safe_relative(relative_path, "relative_path")
            root, durable = self.identity.project_root, True
        elif owner == "HISTORY":
            if checked_stage is None and kind in {"stage_evidence", "planning_evidence"}:
                raise ArtifactResolverError("STAGE_ID_REQUIRED", "stage evidence requires stage_id")
            if checked_operation is None and kind in {"gpt_review_receipt", "integration_receipt", "verification_receipt"}:
                raise ArtifactResolverError("OPERATION_ID_REQUIRED", "receipt requires operation_id")
            prefix = _HISTORY_PREFIXES.get(kind)
            if prefix is None:
                rel = _safe_relative(relative_path, "relative_path")
                if not rel.startswith(".research/"):
                    rel = ".research/history/" + rel
            else:
                ident = checked_operation or checked_stage or "evidence"
                suffix = _safe_relative(relative_path or "receipt.json", "relative_path")
                rel = f"{prefix}/{ident}/{suffix}"
            root, durable = self.identity.project_root, True
        else:
            if checked_operation is None:
                raise ArtifactResolverError("OPERATION_ID_REQUIRED", "TEMP artifact requires operation_id")
            suffix = _safe_relative(relative_path or "payload", "relative_path")
            rel = f"workspaces/{self.identity.workspace_id}/runs/{checked_operation}/{kind}/{suffix}"
            root, durable = self.identity.machine_runtime_root, False
        absolute = (root / Path(*rel.split("/"))).resolve(strict=False)
        _guard_existing_links(root, absolute)
        if not _inside(root, absolute):
            raise ArtifactResolverError("PATH_OUTSIDE_ROOT", "resolved artifact is outside its owner root")
        return Placement(kind, owner, operation, self.identity.workspace_id, self.identity.project_id,
                         str(root), rel, absolute, durable=durable)

    def write_bytes(self, placement: Placement, data: bytes, *, overwrite: bool = False) -> Path:
        if not isinstance(placement, Placement) or placement.workspace_id != self.identity.workspace_id:
            raise ArtifactResolverError("PLACEMENT_INVALID", "placement does not belong to this resolver")
        if not isinstance(data, (bytes, bytearray)):
            raise ArtifactResolverError("WRITE_INVALID", "artifact content must be bytes")
        root = {"ENGINE": self.identity.engine_root, "PROJECT": self.identity.project_root,
                "HISTORY": self.identity.project_root, "TEMP": self.identity.machine_runtime_root}[placement.ownership]
        _guard_existing_links(root, placement.absolute_path)
        if placement.absolute_path.exists() and not overwrite:
            raise ArtifactResolverError("TARGET_EXISTS", "resolver refuses to overwrite an existing artifact")
        placement.absolute_path.parent.mkdir(parents=True, exist_ok=True)
        _guard_existing_links(root, placement.absolute_path.parent)
        fd, temporary = tempfile.mkstemp(prefix=f".{placement.absolute_path.name}-", suffix=".tmp", dir=str(placement.absolute_path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(bytes(data))
                handle.flush()
                os.fsync(handle.fileno())
            if placement.absolute_path.exists() and not overwrite:
                raise ArtifactResolverError("TARGET_EXISTS", "resolver refuses to overwrite an existing artifact")
            os.replace(temporary, placement.absolute_path)
        except ArtifactResolverError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        except OSError as exc:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise ArtifactResolverError("WRITE_FAILED", "artifact write failed") from exc
        return placement.absolute_path

    def write_text(self, placement: Placement, text: str, *, overwrite: bool = False) -> Path:
        if not isinstance(text, str):
            raise ArtifactResolverError("WRITE_INVALID", "artifact text must be text")
        return self.write_bytes(placement, text.encode("utf-8"), overwrite=overwrite)

    def write_json(self, placement: Placement, value: Mapping[str, Any], *, overwrite: bool = False) -> Path:
        if not isinstance(value, Mapping):
            raise ArtifactResolverError("WRITE_INVALID", "artifact JSON must be an object")
        return self.write_text(placement, json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n", overwrite=overwrite)

    def legacy_read(self, path: str | os.PathLike[str], *, expected_ownership: str) -> bytes:
        owner = _identity(expected_ownership, "expected_ownership").upper()
        if owner not in {"ENGINE", "PROJECT", "HISTORY"}:
            raise ArtifactResolverError("LEGACY_READ_INVALID", "legacy reads must target a durable owner")
        candidate = Path(os.fspath(path)).expanduser()
        if not candidate.is_absolute():
            raise ArtifactResolverError("LEGACY_READ_INVALID", "legacy read path must be absolute")
        candidate = candidate.resolve(strict=False)
        root = self.identity.engine_root if owner == "ENGINE" else self.identity.project_root
        _guard_existing_links(root, candidate)
        if not _inside(root, candidate) or not candidate.is_file():
            raise ArtifactResolverError("LEGACY_READ_INVALID", "legacy path is outside the expected owner or not a file")
        try:
            return candidate.read_bytes()
        except OSError as exc:
            raise ArtifactResolverError("LEGACY_READ_FAILED", "legacy artifact could not be read") from exc


__all__ = ["ArtifactResolver", "ArtifactResolverError", "Placement", "RESOLVER_VERSION", "WorkspaceIdentity"]
