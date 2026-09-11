"""Small, project-level persistence helpers for the Step 11 intake.

The Stage controller deliberately owns Stage state.  This module owns only
the one canonical project brief file used by requirements intake.  Keeping
the path/identity helpers separate makes it difficult for intake code to
accidentally create Stage, discovery, or browser state.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


PROJECT_BRIEF_RELATIVE_PATH = Path(".research") / "PROJECT_BRIEF.json"
PROJECT_BRIEF_FILENAME = PROJECT_BRIEF_RELATIVE_PATH.name


class ProjectStateError(RuntimeError):
    """Raised when the canonical project brief cannot be loaded or written."""


class ProjectState:
    """Tiny path-bound facade for callers that prefer an object API."""

    def __init__(self, project_root: str | os.PathLike[str] = ".") -> None:
        self.root = resolve_project_root(project_root)
        self.brief_path = self.root / PROJECT_BRIEF_RELATIVE_PATH

    def load(self) -> dict[str, Any] | None:
        return load_project_brief(self.root)

    def save(self, payload: Mapping[str, Any]) -> Path:
        return save_project_brief(self.root, payload)


def resolve_project_root(value: str | os.PathLike[str] = ".") -> Path:
    """Resolve and validate a project directory without changing it."""

    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectStateError("project root does not exist") from exc
    if not resolved.is_dir():
        raise ProjectStateError("project root must be a directory")
    return resolved


def project_brief_path(project_root: str | os.PathLike[str] = ".") -> Path:
    """Return the only canonical intake state path for ``project_root``."""

    root = resolve_project_root(project_root)
    return root / PROJECT_BRIEF_RELATIVE_PATH


def load_project_brief(project_root: str | os.PathLike[str] = ".") -> dict[str, Any] | None:
    """Read the canonical brief, returning ``None`` when it does not exist.

    Structural and semantic validation lives in :mod:`src.project_intake` so
    this low-level helper remains free of an import cycle.  It still rejects
    malformed JSON and non-object roots before returning data to callers.
    """

    path = project_brief_path(project_root)
    if not path.exists():
        return None
    if not path.is_file():
        raise ProjectStateError("canonical project brief path is not a file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectStateError("canonical project brief is unreadable") from exc
    if not isinstance(value, dict):
        raise ProjectStateError("canonical project brief must contain an object")
    return value


def save_project_brief(
    project_root: str | os.PathLike[str],
    payload: Mapping[str, Any],
) -> Path:
    """Atomically write the canonical project brief and no sibling copy."""

    root = resolve_project_root(project_root)
    path = root / PROJECT_BRIEF_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    fd: int | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=".PROJECT_BRIEF-",
            suffix=".tmp",
            dir=str(path.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
        temporary = None
    except (OSError, TypeError, ValueError) as exc:
        raise ProjectStateError("could not persist canonical project brief") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return path


__all__ = [
    "PROJECT_BRIEF_FILENAME",
    "PROJECT_BRIEF_RELATIVE_PATH",
    "ProjectState",
    "ProjectStateError",
    "load_project_brief",
    "project_brief_path",
    "resolve_project_root",
    "save_project_brief",
]
