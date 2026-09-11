"""Content-addressed Git baselines for a bounded research Stage.

The Stage Controller intentionally knows only about a stable baseline digest.
This module keeps the repository-specific work in a small, local adapter.  It
does not commit, checkout, reset, clean, or otherwise mutate the repository.
Only read-only ``git`` commands are used while a baseline is captured.

The persisted representation is deliberately independent from the current
working tree.  A dirty repository is valid: its status and a digest of the
working-tree evidence are recorded, while the user's uncommitted work is left
untouched.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    ContractValidationError,
    canonical_json,
    sha256_json,
    validate_against_schema,
)


class GitBaselineError(ContractValidationError):
    """Raised when a Git baseline cannot be captured or verified safely."""


def _repo_path(value: str | os.PathLike[str]) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        raise GitBaselineError("repository_root must be a path")
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        raise GitBaselineError(f"repository_root is not a directory: {root}")
    return root


def _run_git(root: Path, args: Sequence[str], *, check: bool = True) -> str:
    """Run one read-only Git command and return bounded text output."""

    # Keep the allow-list explicit.  It is easy for a future caller to
    # accidentally turn this adapter into a mutating Git wrapper otherwise.
    if not args or args[0] not in {
        "rev-parse", "status", "diff", "ls-files", "config", "symbolic-ref"
    }:
        raise GitBaselineError("git baseline adapter only permits read-only commands")
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(root),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitBaselineError(f"git command failed: {type(exc).__name__}") from exc
    if check and completed.returncode != 0:
        # Do not expose arbitrary command output in receipts.  The command and
        # exit code are sufficient for the caller to classify the failure.
        raise GitBaselineError(
            f"git {args[0]} failed with exit code {completed.returncode}"
        )
    return completed.stdout


def _git_root(root: Path) -> Path:
    value = _run_git(root, ["rev-parse", "--show-toplevel"]).strip()
    if not value:
        raise GitBaselineError("path is not inside a Git repository")
    resolved = Path(value).resolve()
    try:
        root.relative_to(resolved)
    except ValueError as exc:
        raise GitBaselineError("repository_root is outside Git worktree") from exc
    return resolved


def _relative_path(root: Path, value: str) -> str:
    path = value.replace("\\", "/")
    if "\x00" in path:
        raise GitBaselineError("Git status contained a NUL path")
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise GitBaselineError("Git status path escaped repository") from exc
    return path


def _status_entries(root: Path) -> list[dict[str, str]]:
    # Porcelain v1 is stable and intentionally excludes ignored files.  Use a
    # text form here because the paths are stored only as a bounded summary.
    output = _run_git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    entries: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line:
            continue
        status = line[:2]
        raw_path = line[3:] if len(line) > 3 else ""
        # Rename/copy records use ``old -> new``.  Include both paths in a
        # deterministic summary without attempting to interpret shell syntax.
        if " -> " in raw_path:
            old, new = raw_path.split(" -> ", 1)
            entries.append(
                {
                    "status": status,
                    "path": _relative_path(root, new),
                    "previous_path": _relative_path(root, old),
                }
            )
        else:
            entries.append({"status": status, "path": _relative_path(root, raw_path)})
    entries.sort(key=lambda item: (item.get("path", ""), item.get("status", "")))
    return entries


def _tracked_diff_digest(root: Path) -> str:
    # ``git diff HEAD`` includes staged and unstaged tracked changes.  An
    # unborn repository has no HEAD, so use the empty-tree diff as a fallback.
    try:
        output = _run_git(root, ["diff", "--no-ext-diff", "--binary", "HEAD"])
    except GitBaselineError:
        output = _run_git(root, ["diff", "--no-ext-diff", "--binary"])
    return hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()


def _untracked_file_digests(root: Path, entries: Iterable[Mapping[str, str]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in entries:
        path_text = entry.get("path", "")
        if not path_text or entry.get("status", "")[1:2] != "?":
            continue
        path = (root / Path(*path_text.split("/"))).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise GitBaselineError("untracked path escaped repository") from exc
        if not path.is_file():
            result[path_text] = "missing"
            continue
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise GitBaselineError(f"cannot hash untracked file: {path_text}") from exc
        result[path_text] = digest.hexdigest()
    return result


def _head_sha(root: Path) -> str | None:
    output = _run_git(root, ["rev-parse", "HEAD"], check=False).strip()
    if not output:
        return None
    # A SHA is the only value accepted into the baseline.  This also avoids
    # persisting a surprising symbolic command result in a receipt.
    if len(output) != 40 or any(character not in "0123456789abcdefABCDEF" for character in output):
        raise GitBaselineError("git HEAD is not a SHA-1 object id")
    return output.lower()


def _ref_list(value: Sequence[str] | None, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        raise GitBaselineError(f"{field} must be a sequence of strings")
    checked: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip() or "\x00" in item:
            raise GitBaselineError(f"{field}[{index}] must be a non-empty string")
        normalized = item.strip()
        # Ref names are metadata only.  Do not allow callers to smuggle an
        # authentication/profile reference into a persisted baseline.
        lower = normalized.lower().replace("\\", "/")
        if any(part in {".env", ".auth", "credentials", "credential", "cookie", "cookies", "token", "tokens", "secret", "secrets", "password", "passwords"} for part in lower.split("/")):
            raise GitBaselineError(f"{field}[{index}] refers to sensitive material")
        checked.append(normalized)
    if len(set(checked)) != len(checked):
        raise GitBaselineError(f"{field} must not contain duplicate refs")
    return checked


def capture_git_baseline(
    repository_root: str | os.PathLike[str],
    *,
    stage_id: str | None = None,
    metrics_refs: Sequence[str] | None = None,
    review_refs: Sequence[str] | None = None,
    config_refs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Capture a deterministic, read-only baseline for ``repository_root``.

    The returned digest is stable for the same commit, working-tree state and
    explicit evidence refs.  No timestamp or machine-local transient is part
    of the digest.  A dirty tree is recorded as-is; this function never calls
    ``git commit`` or any other mutating command.
    """

    requested_root = _repo_path(repository_root)
    git_root = _git_root(requested_root)
    entries = _status_entries(git_root)
    untracked = _untracked_file_digests(git_root, entries)
    status_body = {
        "dirty": bool(entries),
        "entries": entries,
        "tracked_diff_digest": _tracked_diff_digest_digest(git_root),
        "untracked_file_digests": untracked,
    }
    refs = {
        "metrics_refs": _ref_list(metrics_refs, "metrics_refs"),
        "review_refs": _ref_list(review_refs, "review_refs"),
        "config_refs": _ref_list(config_refs, "config_refs"),
    }
    body: dict[str, Any] = {
        "baseline_version": "git_baseline.v1",
        "repository_root": str(git_root),
        # Keep the explicit path alias readable for callers that describe the
        # captured repository as ``repository_path``.  Both values point to
        # the same resolved worktree and are covered by the digest.
        "repository_path": str(git_root),
        "git_root": str(git_root),
        "commit_sha": _head_sha(git_root),
        "dirty": status_body["dirty"],
        "dirty_summary": status_body,
        "metrics_refs": refs["metrics_refs"],
        "review_refs": refs["review_refs"],
        "config_refs": refs["config_refs"],
        "baseline_metrics_refs": refs["metrics_refs"],
        "baseline_review_refs": refs["review_refs"],
    }
    if stage_id is not None:
        if not isinstance(stage_id, str) or not stage_id.strip():
            raise GitBaselineError("stage_id must be a non-empty string")
        body["stage_id"] = stage_id.strip()
    return {**body, "digest": sha256_json(body)}


def _tracked_diff_digest_digest(root: Path) -> str:
    """Compatibility wrapper kept separate for easier test monkeypatching."""

    return _tracked_diff_digest(root)


def verify_git_baseline(baseline: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a baseline's schema and content digest without touching Git."""

    if not isinstance(baseline, Mapping):
        raise GitBaselineError("git baseline must be an object")
    checked = copy.deepcopy(dict(baseline))
    validate_against_schema(checked, "git_baseline")
    supplied = checked.pop("digest", None)
    expected = sha256_json(checked)
    if not isinstance(supplied, str) or supplied != expected:
        raise GitBaselineError("git baseline digest mismatch")
    return {**checked, "digest": expected}


def compare_git_baseline(
    baseline: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare a candidate snapshot against an immutable baseline."""

    checked = verify_git_baseline(baseline)
    candidate_checked = verify_git_baseline(candidate)
    candidate_digest = candidate_checked["digest"]
    return {
        "baseline_digest": checked["digest"],
        "candidate_digest": candidate_digest,
        "same": candidate_digest == checked["digest"],
        "dirty_changed": candidate_checked.get("dirty_summary") != checked.get("dirty_summary"),
        "commit_changed": candidate_checked.get("commit_sha") != checked.get("commit_sha"),
    }


def persist_git_baseline(
    baseline: Mapping[str, Any],
    path: str | os.PathLike[str],
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Persist one baseline atomically, refusing accidental replacement.

    ``overwrite`` is intentionally opt-in and still refuses to overwrite a
    different baseline.  In normal Stage operation a second call simply reads
    and returns the same content-addressed baseline.
    """

    checked = verify_git_baseline(baseline)
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GitBaselineError("persisted Git baseline is unreadable") from exc
        existing_checked = verify_git_baseline(existing)
        if existing_checked["digest"] != checked["digest"]:
            raise GitBaselineError("refusing to overwrite a different Git baseline")
        return existing_checked
    payload = canonical_json(checked) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # ``overwrite`` cannot weaken the immutable-first-write policy.  Keep
        # the check unconditional so even a concurrent writer cannot cause a
        # different iteration to replace the first baseline.
        if destination.exists():
            raise GitBaselineError("baseline destination appeared during write")
        os.replace(temp_name, destination)
    except OSError as exc:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise GitBaselineError("cannot persist Git baseline") from exc
    return checked


def freeze_git_baseline(
    repository_root: str | os.PathLike[str],
    path: str | os.PathLike[str],
    **kwargs: Any,
) -> dict[str, Any]:
    """Freeze once at ``path``; ordinary iterations reuse the stored value."""

    destination = Path(path).expanduser().resolve()
    if destination.exists():
        try:
            return verify_git_baseline(json.loads(destination.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise GitBaselineError("persisted Git baseline is unreadable") from exc
    baseline = capture_git_baseline(repository_root, **kwargs)
    return persist_git_baseline(baseline, destination)


__all__ = [
    "GitBaselineError",
    "capture_git_baseline",
    "compare_git_baseline",
    "freeze_git_baseline",
    "persist_git_baseline",
    "verify_git_baseline",
]
