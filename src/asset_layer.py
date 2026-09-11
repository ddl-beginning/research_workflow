"""Canonical, bounded project asset inventory for Steps 13 and 14.

The asset layer is intentionally small.  It records metadata and local audit
facts in ``.research/AVAILABLE_ASSETS.json`` and, when a real local project is
present, a compact read-only audit in ``.research/LOCAL_PROJECT_PROFILE.md``.
It never copies source trees, uploads files, runs project code, or treats a
candidate as preferred merely because it is local or externally discovered.

All writes are canonical and idempotent.  A repeated audit with unchanged
facts leaves the existing bytes untouched; an evidence change updates the
same canonical file in place rather than creating numbered summaries.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from .contracts import ContractValidationError, canonical_json, load_schema, sha256_json, validate_instance
from .project_intake import ProjectIntakeError, project_identity, validate_project_brief
from .project_state import ProjectStateError, load_project_brief, resolve_project_root


AVAILABLE_ASSETS_SCHEMA_VERSION = "available_assets.v1"
AVAILABLE_ASSETS_RELATIVE_PATH = Path(".research") / "AVAILABLE_ASSETS.json"
AVAILABLE_ASSETS_FILENAME = AVAILABLE_ASSETS_RELATIVE_PATH.name
AVAILABLE_ASSETS_PATH = AVAILABLE_ASSETS_RELATIVE_PATH
LOCAL_PROJECT_PROFILE_RELATIVE_PATH = Path(".research") / "LOCAL_PROJECT_PROFILE.md"
LOCAL_PROJECT_PROFILE_FILENAME = LOCAL_PROJECT_PROFILE_RELATIVE_PATH.name
LOCAL_PROJECT_PROFILE_PATH = LOCAL_PROJECT_PROFILE_RELATIVE_PATH
ASSET_PACK_SCHEMA_VERSION = "asset_pack.v1"
ASSET_LAYER_MARKER = "AVAILABLE_ASSET_LAYER_PASS"
LOCAL_PROJECT_CANDIDATE_MARKER = "LOCAL_PROJECT_CANDIDATE_PASS"

# These are vocabulary, not a workflow state machine.  A composition remains
# one consultant-selected route and is never advanced through these values.
ASSET_KINDS = (
    "requirement",
    "user_repository",
    "cloned_repository",
    "dataset",
    "documentation",
    "baseline_result",
    "review_artifact",
    "script",
    "tool",
    "library",
    "external_repository",
    "other",
)
ASSET_ROLES = ("core_requirement", "candidate_base", "supporting_asset", "baseline", "evidence", "tooling")
UPLOAD_POLICIES = ("never", "canonical_only", "metadata_only", "on_demand_explicit")
COMPOSITION_DECISIONS = (
    "KEEP_EXISTING",
    "KEEP_AND_OPTIMIZE",
    "KEEP_AND_BORROW",
    "REPLACE_BASE",
    "BUILD_FROM_EXISTING_LIBRARIES",
    "BUILD_NEW",
)
CANDIDATE_USER_PROJECT = "CANDIDATE_USER_PROJECT"
CANDIDATE_USER_PROJECT_ASSET_ID = "candidate-user-project"
MAX_ASSETS = 64
MAX_PACK_ASSETS = 32
MAX_OPTIONAL_ARTIFACTS = 4
MAX_TOP_LEVEL_ENTRIES = 80
MAX_SOURCE_REFS = 24
MAX_TEXT_LENGTH = 12_000
MAX_SUMMARY_LENGTH = 2_000
MAX_CANONICAL_BYTES = 180_000
MAX_PROFILE_BYTES = 80_000
MAX_PACK_BYTES = 120_000
MAX_GIT_TIMEOUT_SECONDS = 15
MAX_PACK_RESOURCES = 16
MAX_CONTEXT_BYTES = 120_000
# A brief's list-valued constraints are represented both in the local
# profile and in the canonical requirement asset.  Keep one bound at this
# seam so the two projections cannot disagree.
MAX_BRIEF_LIST_ITEMS = 16

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "client_secret",
        "cookie",
        "cookies",
        "dom",
        "local_storage",
        "password",
        "passwd",
        "prompt",
        "raw_dom",
        "raw_response",
        "secret",
        "session",
        "session_id",
        "session_storage",
        "storage_state",
        "token",
        "tokens",
        "transcript",
        "conversation",
        "messages",
        "turns",
        "chat_history",
        "raw_history",
        "confidence",
        "confidence_score",
        "established_facts_unverified",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_HOUSEKEEPING = frozenset({".research", ".git", ".pytest_cache", "__pycache__", ".venv", "node_modules"})
_CANONICAL_ASSET_NAMES = frozenset(
    {
        AVAILABLE_ASSETS_FILENAME.casefold(),
        "project_brief.json",
        "project_context.md",
        LOCAL_PROJECT_PROFILE_FILENAME.casefold(),
    }
)


class AssetLayerError(RuntimeError):
    """Fail-closed canonical asset/audit error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        schema_path: str | None = None,
        failure_field: str | None = None,
    ) -> None:
        self.code = str(code)
        # These are optional, bounded diagnostics for the discovery adapter.
        # They are never populated with payload values or exception text.
        self.schema_path = schema_path if isinstance(schema_path, str) else None
        self.failure_field = failure_field if isinstance(failure_field, str) else None
        super().__init__(message)


# Friendly aliases used by small integrations.
AvailableAssetsError = AssetLayerError
AssetError = AssetLayerError

_SCHEMA_PATH_PATTERN = re.compile(
    r"^\$(?:(?:\.[A-Za-z_][A-Za-z0-9_-]{0,63})|(?:\[[0-9]{1,4}\])){0,24}$"
)


def _schema_path_from_contract_error(error: BaseException) -> str | None:
    """Extract only the local validator's bounded path token."""

    candidate = str(error).split(":", 1)[0].strip()
    return candidate if _SCHEMA_PATH_PATTERN.fullmatch(candidate) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any, field: str, *, required: bool = False, maximum: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise AssetLayerError("ASSET_INPUT_INVALID", f"{field} must be text")
    text = value.strip()
    if required and not text:
        raise AssetLayerError("ASSET_INPUT_INVALID", f"{field} must be non-empty")
    if "\x00" in text or len(text) > maximum:
        raise AssetLayerError("ASSET_INPUT_INVALID", f"{field} exceeds its bound")
    return text


def _assert_safe(value: Any, *, path: str = "$", depth: int = 0) -> None:
    if depth > 12:
        raise AssetLayerError("ASSET_INPUT_TOO_DEEP", "asset data is too deeply nested")
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if key in _SENSITIVE_KEYS:
                raise AssetLayerError("ASSET_SECRET_REJECTED", f"forbidden field at {path}")
            _assert_safe(child, path=f"{path}.{raw_key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise AssetLayerError("ASSET_INPUT_TOO_LARGE", f"too many values at {path}")
        for index, child in enumerate(value):
            _assert_safe(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str):
        if "\x00" in value or len(value) > MAX_TEXT_LENGTH:
            raise AssetLayerError("ASSET_INPUT_INVALID", f"unsafe text at {path}")
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise AssetLayerError("ASSET_SECRET_REJECTED", f"secret-like text at {path}")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise AssetLayerError("ASSET_INPUT_INVALID", f"unsupported value at {path}")


def _relative_ref(value: Any, field: str = "local_ref_or_url") -> str:
    text = _safe_text(value, field, maximum=2_000)
    if not text:
        return text
    if "://" in text:
        try:
            parsed = urlsplit(text)
            if parsed.scheme.casefold() not in {"http", "https", "ssh", "git"} or not parsed.hostname:
                raise ValueError("unsupported URL")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("credentials are not allowed")
        except ValueError as exc:
            raise AssetLayerError("ASSET_REF_INVALID", f"{field} is not a safe URL") from exc
        return text
    raw = text.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or raw in {".", ".."}:
        raise AssetLayerError("ASSET_REF_INVALID", f"{field} must be a relative metadata reference")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise AssetLayerError("ASSET_REF_INVALID", f"{field} contains traversal or empty components")
    if any(part.casefold() in {".auth", "credentials", "secrets", "secret", "tokens"} or part.casefold().startswith(".env") for part in parts):
        raise AssetLayerError("ASSET_REF_INVALID", f"{field} points to a protected path")
    # A root/ref marker is not a bounded asset and would amount to a whole-repo
    # attachment.  Canonical profile/brief refs remain allowed.
    if raw.casefold() in {"repo", "repository", "project", "workspace", "source", "src", "**"}:
        raise AssetLayerError("ASSET_REF_INVALID", f"{field} cannot refer to the whole repository")
    return raw


def _text_list(value: Any, field: str, *, maximum: int = 16, maximum_text: int = 4_000) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        values = value
    else:
        raise AssetLayerError("ASSET_INPUT_INVALID", f"{field} must be a list of text")
    result: list[str] = []
    for index, item in enumerate(values):
        text = _safe_text(str(item), f"{field}[{index}]", maximum=maximum_text)
        if text and text not in result:
            result.append(text)
        if len(result) > maximum:
            raise AssetLayerError("ASSET_INPUT_TOO_LARGE", f"{field} exceeds its bound", failure_field=field)
    return result


def _brief_view(value: Mapping[str, Any]) -> dict[str, Any]:
    nested = value.get("brief") if isinstance(value.get("brief"), Mapping) else {}
    allowed = (
        "title",
        "problem_statement",
        "goal",
        "desired_outcome",
        "priority",
        "success_criteria",
        "acceptance_criteria",
        "constraints",
        "non_goals",
        "scope",
        "stakeholders",
        "preferences",
    )
    result = {key: copy.deepcopy(nested[key]) for key in allowed if key in nested}
    if not result.get("goal") and isinstance(value.get("rough_requirement"), str):
        result["goal"] = value["rough_requirement"].strip()
    return result


def _load_checked_brief(root: Path, supplied: Mapping[str, Any] | None) -> tuple[dict[str, Any], str, str]:
    value: Any = supplied
    if value is None:
        try:
            value = load_project_brief(root)
        except ProjectStateError as exc:
            raise AssetLayerError("BRIEF_UNREADABLE", "canonical project brief could not be loaded") from exc
    if not isinstance(value, Mapping):
        raise AssetLayerError("BRIEF_NOT_FOUND", "an approved project brief is required")
    nested = value.get("project_brief")
    if isinstance(nested, Mapping):
        value = nested
    checked: Mapping[str, Any]
    # Canonical intake records carry an identity and lifecycle state.  Keep the
    # approved gate when it is available, while permitting compact callers to
    # supply a test brief with a stable project_id.
    if "state" in value or "status" in value or "repository_root" in value:
        try:
            checked = validate_project_brief(value, root)
        except ProjectIntakeError as exc:
            raise AssetLayerError("BRIEF_INVALID", "project brief failed validation") from exc
        if checked.get("state") != "APPROVED" or checked.get("status") != "APPROVED":
            raise AssetLayerError("BRIEF_NOT_APPROVED", "asset collection requires an approved brief")
    else:
        checked = copy.deepcopy(dict(value))
        if not isinstance(checked.get("project_id"), str) or not checked.get("project_id"):
            checked["project_id"] = project_identity(root)["project_id"]
    _assert_safe(checked, path="project_brief")
    project_id = str(checked.get("project_id") or project_identity(root)["project_id"])
    return dict(checked), project_id, sha256_json(dict(checked))


def has_local_project(project_root: str | os.PathLike[str] = ".") -> bool:
    """Return whether the root has a user project beyond workflow metadata."""

    root = resolve_project_root(project_root)
    try:
        return any(item.name not in _HOUSEKEEPING for item in root.iterdir()) or (root / ".git").exists()
    except OSError as exc:
        raise AssetLayerError("LOCAL_PROJECT_UNREADABLE", "project directory cannot be inspected") from exc


def _top_level_entries(root: Path) -> list[str]:
    try:
        return sorted(item.name for item in root.iterdir() if item.name not in {".research", ".git"})[:MAX_TOP_LEVEL_ENTRIES]
    except OSError as exc:
        raise AssetLayerError("LOCAL_PROJECT_UNREADABLE", "project directory cannot be inspected") from exc


def _git_audit(root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"present": (root / ".git").exists(), "sha": None, "dirty": False, "status_entry_count": 0}
    if not result["present"]:
        return result

    def run(args: Sequence[str]) -> str:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=MAX_GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return (completed.stdout or "").strip()

    sha = run(["rev-parse", "HEAD"])
    result["sha"] = sha.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", sha) else None
    status = run(["status", "--short"])
    result["status_entry_count"] = min(999, len([line for line in status.splitlines() if line.strip()]))
    result["dirty"] = bool(status.strip())
    return result


def _readme_summary(root: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt"):
        path = root / name
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 64_000:
                continue
            raw = path.read_bytes()[:4_000].decode("utf-8", errors="replace")
        except OSError:
            continue
        lines: list[str] = []
        for line in raw.splitlines():
            compact = " ".join(line.strip().split())
            if not compact or compact.startswith("```"):
                continue
            if any(pattern.search(compact) for pattern in _SECRET_PATTERNS):
                continue
            lines.append(compact.lstrip("#*- "))
            if len(" ".join(lines)) >= 320:
                break
        if lines:
            return " ".join(lines)[:600]
    return ""


def _entry_points(root: Path, entries: Sequence[str]) -> list[str]:
    known = (
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "main.py",
        "app.py",
        "index.js",
        "index.ts",
        "src/main.py",
        "src/index.js",
        "src/index.ts",
    )
    found: list[str] = []
    for ref in known:
        if (root / Path(*ref.split("/"))).is_file():
            found.append(ref)
    if not found:
        # Top-level directories are references only, never source attachments.
        for entry in entries:
            path = root / entry
            if path.is_dir() and entry not in _HOUSEKEEPING:
                found.append(entry + "/")
                if len(found) >= 16:
                    break
    return found[:16]


def _audit_local_project(root: Path, brief: Mapping[str, Any]) -> dict[str, Any]:
    entries = _top_level_entries(root)
    markers = {
        "pyproject.toml": "python",
        "requirements.txt": "python",
        "setup.py": "python",
        "package.json": "node",
        "Cargo.toml": "rust",
        "go.mod": "go",
        "pom.xml": "java",
        "README.md": "readme",
    }
    detected = sorted({label for name, label in markers.items() if (root / name).exists()})
    git = _git_audit(root)
    refs = _entry_points(root, entries)
    # A bounded audit uses a few names and metadata facts, not a tree dump.
    purpose = str(_brief_view(brief).get("goal") or _brief_view(brief).get("problem_statement") or "")[:MAX_SUMMARY_LENGTH]
    visible = str(_brief_view(brief).get("desired_outcome") or _readme_summary(root) or "Capability requires local review")[:MAX_SUMMARY_LENGTH]
    reusable = [f"entry point: {ref}" for ref in refs[:8]]
    if not reusable:
        reusable = ["No conventional entry point was identified by the read-only audit"]
    failures: list[str] = []
    if not any("test" in entry.casefold() for entry in entries):
        failures.append("No top-level test marker was identified by the bounded audit")
    risks: list[str] = []
    if len(detected) > 1:
        risks.append("Multiple runtime markers are present; execution ownership needs confirmation")
    if not git["present"]:
        risks.append("No Git metadata was present for commit-level provenance")
    baseline_refs: list[str] = []
    research = root / ".research"
    if research.is_dir():
        try:
            for item in sorted(research.iterdir(), key=lambda value: value.name.casefold()):
                if not item.is_file() or item.name.casefold() in _CANONICAL_ASSET_NAMES:
                    continue
                lower = item.name.casefold()
                if any(token in lower for token in ("baseline", "result", "worst", "hard", "representative")):
                    baseline_refs.append(item.relative_to(root).as_posix())
                if len(baseline_refs) >= 8:
                    break
        except OSError:
            baseline_refs = []
    non_goals = _brief_view(brief).get("non_goals", [])
    constraints = _brief_view(brief).get("constraints", [])
    return {
        "schema_version": "local_project_profile.v1",
        "marker": LOCAL_PROJECT_CANDIDATE_MARKER,
        "project_purpose": purpose or "Purpose was not stated in the approved brief",
        "current_user_visible_capability": visible,
        "current_architecture_data_flow": "Read-only structure audit: " + (", ".join(detected) if detected else "no conventional runtime marker"),
        "actual_entry_points": refs,
        "important_modules": [entry for entry in entries if entry not in refs and entry.casefold() not in {"readme.md", "readme.rst"}][:16],
        "reusable_components": reusable[:16],
        "current_baseline": baseline_refs or ["No bounded baseline/result artifact was identified"],
        "known_failures_blockers": failures,
        "known_rejected_directions": _text_list(non_goals, "brief.non_goals", maximum=MAX_BRIEF_LIST_ITEMS),
        "protected_frozen_boundaries": [
            ".research/PROJECT_BRIEF.json",
            ".research/AVAILABLE_ASSETS.json",
            ".git metadata is read-only",
        ] + _text_list(constraints, "brief.constraints", maximum=MAX_BRIEF_LIST_ITEMS),
        "material_technical_debt": [
            "Execution and source behavior were not changed by this audit",
        ],
        "potential_architecture_risks": risks,
        "important_source_refs": refs + baseline_refs,
        "git_sha": git["sha"],
        "git_dirty": git["dirty"],
        "git_dirty_entry_count": git["status_entry_count"],
        "top_level_entries": entries,
        "detected_project_markers": detected,
        "checked_read_only": True,
    }


def _profile_markdown(profile: Mapping[str, Any]) -> str:
    def values(key: str) -> list[str]:
        raw = profile.get(key, [])
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item).strip()]
        return [str(raw)] if raw else []

    lines = [
        "# Local Project Profile",
        "",
        f"<!-- marker: {LOCAL_PROJECT_CANDIDATE_MARKER} -->",
        "This is a compact read-only audit. It is not a source-tree dump or an upload manifest.",
        "",
        "- schema_version: `local_project_profile.v1`",
        "- checked_read_only: `true`",
        f"- git_sha: `{profile.get('git_sha') or 'unavailable'}`",
        f"- git_dirty: `{str(bool(profile.get('git_dirty'))).lower()}` ({int(profile.get('git_dirty_entry_count', 0))} status entries)",
        "",
    ]
    sections = (
        ("Project purpose", "project_purpose"),
        ("Current user-visible capability", "current_user_visible_capability"),
        ("Current architecture/data flow", "current_architecture_data_flow"),
        ("Actual entry points", "actual_entry_points"),
        ("Important modules", "important_modules"),
        ("Reusable components", "reusable_components"),
        ("Current baseline", "current_baseline"),
        ("Known failures/blockers", "known_failures_blockers"),
        ("Known rejected directions", "known_rejected_directions"),
        ("Protected/frozen boundaries", "protected_frozen_boundaries"),
        ("Material technical debt", "material_technical_debt"),
        ("Potential architecture risks", "potential_architecture_risks"),
        ("Important source refs", "important_source_refs"),
    )
    for title, key in sections:
        lines.extend([f"## {title}", ""])
        raw = profile.get(key)
        if isinstance(raw, list):
            if raw:
                lines.extend(f"- {str(item)[:4_000]}" for item in raw)
            else:
                lines.append("- None recorded by bounded audit")
        else:
            lines.append(str(raw)[:4_000] if raw else "- None recorded by bounded audit")
        lines.append("")
    rendered = "\n".join(lines)
    if len(rendered.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise AssetLayerError("LOCAL_PROFILE_TOO_LARGE", "local project profile exceeds its bound")
    _assert_safe(rendered, path="LOCAL_PROJECT_PROFILE.md")
    return rendered


def _atomic_text(path: Path, content: str, *, code: str) -> bool:
    if path.exists() and (path.is_symlink() or path.is_dir()):
        raise AssetLayerError(code, "canonical profile path is not a regular file")
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == content:
                return True
        except (OSError, UnicodeError) as exc:
            raise AssetLayerError(code, "canonical profile could not be read") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    fd: int | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            handle.write(content)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise AssetLayerError(code, "canonical profile could not be written") from exc
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
    return False


def build_local_project_profile(
    project_root: str | os.PathLike[str] = ".",
    *,
    brief: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Audit a local project read-only and persist one compact canonical profile."""

    root = resolve_project_root(project_root)
    if not has_local_project(root):
        return None
    checked, _, _ = _load_checked_brief(root, brief)
    profile = _audit_local_project(root, checked)
    markdown = _profile_markdown(profile)
    _atomic_text(root / LOCAL_PROJECT_PROFILE_RELATIVE_PATH, markdown, code="LOCAL_PROFILE_WRITE_FAILED")
    digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    return {**copy.deepcopy(profile), "profile_path": LOCAL_PROJECT_PROFILE_RELATIVE_PATH.as_posix(), "profile_digest": digest, "markdown": markdown}


def load_local_project_profile(project_root: str | os.PathLike[str] = ".") -> dict[str, Any] | None:
    root = resolve_project_root(project_root)
    path = root / LOCAL_PROJECT_PROFILE_RELATIVE_PATH
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise AssetLayerError("LOCAL_PROFILE_INVALID", "local project profile is not a regular file")
    try:
        markdown = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AssetLayerError("LOCAL_PROFILE_INVALID", "local project profile cannot be read") from exc
    if len(markdown.encode("utf-8")) > MAX_PROFILE_BYTES or LOCAL_PROJECT_CANDIDATE_MARKER not in markdown:
        raise AssetLayerError("LOCAL_PROFILE_INVALID", "local project profile is outside its contract")
    _assert_safe(markdown, path="LOCAL_PROJECT_PROFILE.md")
    return {
        "profile_path": LOCAL_PROJECT_PROFILE_RELATIVE_PATH.as_posix(),
        "profile_digest": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "markdown": markdown,
        "checked_read_only": True,
    }


def _asset_digest(asset: Mapping[str, Any]) -> str:
    body = {key: copy.deepcopy(value) for key, value in asset.items() if key != "digest"}
    return sha256_json(body)


def _resource_id(value: Any, *, prefix: str = "resource") -> str:
    """Return a stable, path-independent id for bounded asset provenance."""

    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(value)).strip("-").casefold()
    return f"{prefix}-{slug or 'canonical'}"


def _pack_canonical_resources(root: Path, *, include_profile: bool) -> list[dict[str, Any]]:
    """Describe only workflow-owned canonical files used by an asset pack."""

    specs = [
        (".research/PROJECT_BRIEF.json", "PROJECT_BRIEF.json", MAX_CANONICAL_BYTES),
        (".research/PROJECT_CONTEXT.md", "PROJECT_CONTEXT.md", MAX_CONTEXT_BYTES),
        (AVAILABLE_ASSETS_RELATIVE_PATH.as_posix(), AVAILABLE_ASSETS_FILENAME, MAX_CANONICAL_BYTES),
    ]
    if include_profile:
        specs.append((LOCAL_PROJECT_PROFILE_RELATIVE_PATH.as_posix(), LOCAL_PROJECT_PROFILE_FILENAME, MAX_PROFILE_BYTES))
    resources: list[dict[str, Any]] = []
    for path_text, logical_name, bound in specs:
        path = root / Path(path_text)
        if not path.is_file() or path.is_symlink():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if len(data) > bound:
            continue
        resource_id = _resource_id(logical_name)
        resources.append(
            {
                "resource_id": resource_id,
                "logical_name": logical_name,
                "path": path_text,
                "digest": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
        if len(resources) >= MAX_PACK_RESOURCES:
            break
    return resources


def _make_asset(
    *,
    asset_id: str,
    kind: str,
    role: str,
    source: str,
    local_ref_or_url: str,
    verified: bool,
    summary: str,
    capabilities: Sequence[str] | None = None,
    known_gaps: Sequence[str] | None = None,
    important_constraints: Sequence[str] | None = None,
    protected: bool = True,
    upload_policy: str = "canonical_only",
    is_candidate: bool = False,
    candidate_status: str = "candidate_only",
    verification: Mapping[str, Any] | None = None,
    unverified_claims: Sequence[str] | None = None,
) -> dict[str, Any]:
    if kind not in ASSET_KINDS:
        raise AssetLayerError("ASSET_KIND_INVALID", f"unsupported asset kind: {kind}")
    if role not in ASSET_ROLES:
        raise AssetLayerError("ASSET_ROLE_INVALID", f"unsupported asset role: {role}")
    if upload_policy not in UPLOAD_POLICIES:
        raise AssetLayerError("ASSET_UPLOAD_POLICY_INVALID", f"unsupported upload policy: {upload_policy}")
    item: dict[str, Any] = {
        "asset_id": _safe_text(asset_id, "asset_id", required=True, maximum=200),
        "kind": kind,
        "role": role,
        "source": _safe_text(source, "source", required=True, maximum=2_000),
        "local_ref_or_url": _relative_ref(local_ref_or_url),
        "verified": bool(verified),
        "summary": _safe_text(summary, "summary", maximum=MAX_SUMMARY_LENGTH),
        "capabilities": _text_list(capabilities or [], "capabilities"),
        "known_gaps": _text_list(known_gaps or [], "known_gaps"),
        "important_constraints": _text_list(important_constraints or [], "important_constraints"),
        "protected": bool(protected),
        "upload_policy": upload_policy,
        "is_candidate": bool(is_candidate),
        "candidate_status": _safe_text(candidate_status, "candidate_status", required=True, maximum=64),
        "verification": copy.deepcopy(dict(verification or {})),
        "unverified_claims": _text_list(unverified_claims or [], "unverified_claims"),
    }
    if item["is_candidate"] and item["candidate_status"] not in {"candidate_only", "requires_comparative_review"}:
        raise AssetLayerError("CANDIDATE_PREFERENCE_INVALID", "candidate assets cannot be marked preferred automatically")
    _assert_safe(item, path=f"asset[{asset_id}]")
    item["digest"] = _asset_digest(item)
    return item


def _normalise_asset(value: Mapping[str, Any], index: int = 0) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AssetLayerError("ASSET_INVALID", f"assets[{index}] must be an object")
    _assert_safe(value, path=f"assets[{index}]")
    source = dict(value)
    required = ("asset_id", "kind", "role", "source", "local_ref_or_url", "verified", "summary", "capabilities", "known_gaps", "important_constraints", "protected", "digest", "upload_policy")
    missing = [key for key in required if key not in source]
    if missing:
        raise AssetLayerError("ASSET_INVALID", f"assets[{index}] is missing fields: {', '.join(missing)}")
    item = _make_asset(
        asset_id=source["asset_id"],
        kind=source["kind"],
        role=source["role"],
        source=source["source"],
        local_ref_or_url=source["local_ref_or_url"],
        verified=source["verified"],
        summary=source["summary"],
        capabilities=source["capabilities"],
        known_gaps=source["known_gaps"],
        important_constraints=source["important_constraints"],
        protected=source["protected"],
        upload_policy=source["upload_policy"],
        is_candidate=source.get("is_candidate", source["role"] == "candidate_base"),
        candidate_status=source.get("candidate_status", "candidate_only"),
        verification=source.get("verification", {}),
        unverified_claims=source.get("unverified_claims", []),
    )
    if source.get("digest") != item["digest"]:
        raise AssetLayerError("ASSET_DIGEST_MISMATCH", f"assets[{index}] digest does not match canonical fields")
    return item


def _payload_digest(payload: Mapping[str, Any]) -> str:
    return sha256_json({key: copy.deepcopy(value) for key, value in payload.items() if key not in {"digest", "created_at", "updated_at"}})


def validate_available_assets(document: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise AssetLayerError("AVAILABLE_ASSETS_INVALID", "available assets must be an object")
    try:
        validate_instance(dict(document), load_schema("available_assets"))
    except ContractValidationError as exc:
        raise AssetLayerError(
            "AVAILABLE_ASSETS_INVALID",
            "available assets failed schema validation",
            schema_path=_schema_path_from_contract_error(exc),
        ) from exc
    _assert_safe(document, path="available_assets")
    if document.get("schema_version") != AVAILABLE_ASSETS_SCHEMA_VERSION:
        raise AssetLayerError("AVAILABLE_ASSETS_INVALID", "unsupported available assets schema")
    raw_assets = document.get("assets")
    if not isinstance(raw_assets, list) or len(raw_assets) > MAX_ASSETS:
        raise AssetLayerError("AVAILABLE_ASSETS_INVALID", "assets exceed their bound")
    assets = [_normalise_asset(item, index) for index, item in enumerate(raw_assets)]
    if [item["asset_id"] for item in assets] != sorted(item["asset_id"] for item in assets):
        raise AssetLayerError("AVAILABLE_ASSETS_INVALID", "assets must be sorted by asset_id")
    if len({item["asset_id"] for item in assets}) != len(assets):
        raise AssetLayerError("AVAILABLE_ASSETS_INVALID", "asset_id values must be unique")
    assets_digest = sha256_json(assets)
    if document.get("assets_digest") != assets_digest:
        raise AssetLayerError("AVAILABLE_ASSETS_DIGEST_MISMATCH", "assets_digest does not match assets")
    rejected = _normalise_rejected_candidates(document.get("rejected_candidates", []))
    relationships = _normalise_candidate_relationships(document.get("candidate_relationships", []))
    if document.get("rejected_candidates", []) != rejected or document.get("candidate_relationships", []) != relationships:
        raise AssetLayerError("AVAILABLE_ASSETS_INVALID", "candidate records are not canonical")
    if document.get("digest") != _payload_digest(document):
        raise AssetLayerError("AVAILABLE_ASSETS_DIGEST_MISMATCH", "canonical asset inventory digest does not match")
    detached = copy.deepcopy(dict(document))
    detached["assets"] = assets
    detached["rejected_candidates"] = rejected
    detached["candidate_relationships"] = relationships
    return detached


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise AssetLayerError("AVAILABLE_ASSETS_INVALID", "canonical asset path is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssetLayerError("AVAILABLE_ASSETS_INVALID", "canonical asset file could not be read") from exc
    if not isinstance(value, dict):
        raise AssetLayerError("AVAILABLE_ASSETS_INVALID", "canonical asset file must contain an object")
    return value


def load_available_assets(project_root: str | os.PathLike[str] = ".") -> dict[str, Any] | None:
    root = resolve_project_root(project_root)
    value = _read_json(root / AVAILABLE_ASSETS_RELATIVE_PATH)
    return None if value is None else validate_available_assets(value)


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    if path.exists() and (path.is_symlink() or path.is_dir()):
        raise AssetLayerError("AVAILABLE_ASSETS_WRITE_FAILED", "canonical asset path is not a regular file")
    text = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(text.encode("utf-8")) > MAX_CANONICAL_BYTES:
        raise AssetLayerError("AVAILABLE_ASSETS_TOO_LARGE", "canonical asset inventory exceeds its bound")
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == text:
                return
        except (OSError, UnicodeError) as exc:
            raise AssetLayerError("AVAILABLE_ASSETS_WRITE_FAILED", "canonical asset file could not be read") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    fd: int | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            handle.write(text)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise AssetLayerError("AVAILABLE_ASSETS_WRITE_FAILED", "canonical asset file could not be written") from exc
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


def _base_assets(root: Path, brief: Mapping[str, Any], project_id: str, brief_digest: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    view = _brief_view(brief)
    goal = str(view.get("desired_outcome") or view.get("goal") or view.get("problem_statement") or "Approved project requirement")
    success = view.get("success_criteria", view.get("acceptance_criteria", []))
    constraints = view.get("constraints", [])
    requirement = _make_asset(
        asset_id="requirement-project-brief",
        kind="requirement",
        role="core_requirement",
        source="approved_project_brief",
        local_ref_or_url=AVAILABLE_ASSETS_RELATIVE_PATH.parent.as_posix() + "/PROJECT_BRIEF.json",
        verified=True,
        summary=goal[:MAX_SUMMARY_LENGTH],
        capabilities=_text_list(success, "brief.success_criteria", maximum=MAX_BRIEF_LIST_ITEMS),
        important_constraints=_text_list(constraints, "brief.constraints", maximum=MAX_BRIEF_LIST_ITEMS),
        protected=True,
        upload_policy="canonical_only",
        candidate_status="not_a_candidate",
        verification={"standard": "canonical_brief.v1", "established_facts": ["approved state", f"brief_digest:{brief_digest}"], "checked_read_only": True, "claims_source": "PROJECT_INTAKE"},
    )
    assets = [requirement]
    context_path = root / ".research" / "PROJECT_CONTEXT.md"
    context_present = context_path.is_file() and not context_path.is_symlink()
    context_digest = ""
    if context_present:
        try:
            context_digest = hashlib.sha256(context_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise AssetLayerError("ASSET_INPUT_INVALID", "project context could not be read") from exc
    assets.append(
        _make_asset(
            asset_id="supporting-project-context",
            kind="documentation",
            role="supporting_asset",
            source="project_context_compaction" if context_present else "project_context_missing",
            local_ref_or_url=".research/PROJECT_CONTEXT.md",
            verified=context_present,
            summary="Bounded approved project context" if context_present else "Project context is not present yet",
            capabilities=["semantic project hand-off"] if context_present else [],
            known_gaps=[] if context_present else ["Step 12 context must be generated before Blueprint review"],
            protected=True,
            upload_policy="canonical_only",
            candidate_status="not_a_candidate",
            verification={"standard": "project_context.v1", "established_facts": ([f"context_digest:{context_digest}"] if context_digest else []), "checked_read_only": True, "claims_source": "LOCAL_CANONICAL"},
        )
    )
    profile = build_local_project_profile(root, brief=brief)
    if profile is not None:
        profile_facts = [
            "read-only audit completed",
            f"entry_points:{len(profile.get('actual_entry_points', []))}",
            f"git_sha:{profile.get('git_sha') or 'unavailable'}",
            f"profile_digest:{profile.get('profile_digest', '')}",
        ]
        local_asset = _make_asset(
            asset_id=CANDIDATE_USER_PROJECT_ASSET_ID,
            kind="user_repository",
            role="candidate_base",
            source="codex_read_only_local_audit",
            local_ref_or_url=LOCAL_PROJECT_PROFILE_RELATIVE_PATH.as_posix(),
            verified=True,
            summary="Existing local project audited as Candidate 0; no preference is implied",
            capabilities=_text_list(profile.get("reusable_components", []), "profile.reusable_components", maximum=16),
            known_gaps=_text_list(profile.get("known_failures_blockers", []), "profile.known_failures_blockers", maximum=16),
            important_constraints=_text_list(profile.get("protected_frozen_boundaries", []), "profile.protected_frozen_boundaries", maximum=16),
            protected=True,
            upload_policy="never",
            is_candidate=True,
            candidate_status="candidate_only",
            verification={"standard": "candidate_evidence.v1", "established_facts": profile_facts, "source_refs": profile.get("important_source_refs", [])[:MAX_SOURCE_REFS], "checked_read_only": True, "claims_source": "LOCAL_PROJECT_AUDIT"},
        )
        assets.append(local_asset)
    optional: list[dict[str, Any]] = []
    research = root / ".research"
    if research.is_dir():
        try:
            for path in sorted(research.rglob("*"), key=lambda value: value.as_posix().casefold()):
                if len(optional) >= MAX_OPTIONAL_ARTIFACTS or not path.is_file() or path.is_symlink():
                    continue
                if path.name.casefold() in _CANONICAL_ASSET_NAMES or any(part in {"discovery", "blueprint", "stages"} for part in path.relative_to(research).parts[:-1]):
                    continue
                lower = path.name.casefold()
                if not any(token in lower for token in ("baseline", "result", "worst", "hard", "representative")):
                    continue
                try:
                    size = path.stat().st_size
                    if size > 256 * 1024:
                        continue
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    continue
                rel = path.relative_to(root).as_posix()
                optional.append(
                    _make_asset(
                        asset_id="artifact-" + hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16],
                        kind="baseline_result",
                        role="baseline" if "baseline" in lower else "evidence",
                        source="local_read_only_artifact_audit",
                        local_ref_or_url=rel,
                        verified=True,
                        summary=f"Bounded metadata for local artifact {path.name}",
                        capabilities=["representative evidence"],
                        important_constraints=[f"bytes:{size}"],
                        protected=True,
                        upload_policy="metadata_only",
                        candidate_status="not_a_candidate",
                        verification={"standard": "artifact_metadata.v1", "established_facts": [f"sha256:{digest}", f"bytes:{size}"], "checked_read_only": True, "claims_source": "LOCAL_ARTIFACT_AUDIT"},
                    )
                )
        except OSError:
            optional = []
    assets.extend(optional)
    return assets, profile


def _merge_assets(existing: Sequence[Mapping[str, Any]], fresh: Sequence[Mapping[str, Any]], additions: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    # Fresh canonical requirements/local audit facts win. Existing external
    # candidates survive a later local audit so the canonical inventory remains
    # the single update point for Discovery -> Blueprint hand-off.
    for item in [*existing, *fresh, *(additions or [])]:
        normalized = _normalise_asset(item)
        by_id[normalized["asset_id"]] = normalized
    values = sorted(by_id.values(), key=lambda item: item["asset_id"])
    if len(values) > MAX_ASSETS:
        raise AssetLayerError("ASSET_LIMIT", f"at most {MAX_ASSETS} assets are allowed")
    return values


def _normalise_rejected_candidates(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AssetLayerError("CANDIDATE_RECORD_INVALID", "rejected_candidates must be an array")
    result: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if isinstance(item, str):
            identity = _safe_text(item, f"rejected_candidates[{index}].identity", required=True, maximum=2_000)
            reason = "candidate was not promoted to verified assets"
        elif isinstance(item, Mapping):
            raw_identity = item.get("identity", item.get("repo_url", item.get("asset_id", "")))
            identity = _safe_text(raw_identity, f"rejected_candidates[{index}].identity", required=True, maximum=2_000)
            reason = _safe_text(item.get("reason", item.get("short_reason", "candidate was not promoted to verified assets")), f"rejected_candidates[{index}].reason", required=True, maximum=1_000)
        else:
            raise AssetLayerError("CANDIDATE_RECORD_INVALID", f"rejected_candidates[{index}] must be text or object")
        record = {"identity": identity, "reason": reason}
        if record not in result:
            result.append(record)
        if len(result) > 16:
            raise AssetLayerError("CANDIDATE_RECORD_LIMIT", "at most sixteen rejected candidates are retained")
    return result


def _normalise_candidate_relationships(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        value = [value]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AssetLayerError("CANDIDATE_RECORD_INVALID", "candidate_relationships must be an array")
    result: list[dict[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise AssetLayerError("CANDIDATE_RECORD_INVALID", f"candidate_relationships[{index}] must be an object")
        left = _safe_text(item.get("candidate_asset_id", item.get("left", "")), f"candidate_relationships[{index}].candidate_asset_id", required=True, maximum=200)
        right = _safe_text(item.get("related_asset_id", item.get("right", CANDIDATE_USER_PROJECT_ASSET_ID)), f"candidate_relationships[{index}].related_asset_id", required=True, maximum=200)
        relation = _safe_text(item.get("relationship", "comparison_required"), f"candidate_relationships[{index}].relationship", required=True, maximum=200)
        reason = _safe_text(item.get("reason", "requires comparative review"), f"candidate_relationships[{index}].reason", required=True, maximum=1_000)
        record = {"candidate_asset_id": left, "related_asset_id": right, "relationship": relation, "reason": reason}
        if record not in result:
            result.append(record)
        if len(result) > 32:
            raise AssetLayerError("CANDIDATE_RECORD_LIMIT", "at most thirty-two candidate relationships are retained")
    return result


def _canonical_payload(*, project_id: str, brief_digest: str, assets: Sequence[Mapping[str, Any]], existing: Mapping[str, Any] | None = None, revision: int | None = None, rejected_candidates: Any = None, candidate_relationships: Any = None) -> dict[str, Any]:
    normalized = sorted([_normalise_asset(item) for item in assets], key=lambda item: item["asset_id"])
    now = _now()
    created_at = str(existing.get("created_at")) if isinstance(existing, Mapping) and existing.get("created_at") else now
    if revision is None:
        revision = int(existing.get("revision", 0)) + 1 if isinstance(existing, Mapping) else 1
    payload: dict[str, Any] = {
        "schema_version": AVAILABLE_ASSETS_SCHEMA_VERSION,
        "project_id": project_id,
        "revision": int(revision),
        "created_at": created_at,
        "updated_at": now,
        "brief_digest": brief_digest,
        "local_project_present": any(item.get("asset_id") == CANDIDATE_USER_PROJECT_ASSET_ID for item in normalized),
        "assets": normalized,
        "assets_digest": sha256_json(normalized),
        "rejected_candidates": _normalise_rejected_candidates(
            rejected_candidates if rejected_candidates is not None else (existing.get("rejected_candidates", []) if isinstance(existing, Mapping) else [])
        ),
        "candidate_relationships": _normalise_candidate_relationships(
            candidate_relationships if candidate_relationships is not None else (existing.get("candidate_relationships", []) if isinstance(existing, Mapping) else [])
        ),
        "marker": ASSET_LAYER_MARKER,
    }
    payload["digest"] = _payload_digest(payload)
    _assert_safe(payload, path="available_assets")
    try:
        validate_instance(payload, load_schema("available_assets"))
    except ContractValidationError as exc:
        raise AssetLayerError(
            "AVAILABLE_ASSETS_INVALID",
            "generated available asset inventory failed schema",
            schema_path=_schema_path_from_contract_error(exc),
        ) from exc
    return payload


def collect_available_assets(
    project_root: str | os.PathLike[str] = ".",
    *,
    brief: Mapping[str, Any] | None = None,
    additional_assets: Sequence[Mapping[str, Any]] | None = None,
    rejected_candidates: Any = None,
    candidate_relationships: Any = None,
) -> dict[str, Any]:
    """Create/update ``AVAILABLE_ASSETS.json`` and return its canonical data."""

    root = resolve_project_root(project_root)
    checked, project_id, brief_digest = _load_checked_brief(root, brief)
    existing = load_available_assets(root)
    fresh, profile = _base_assets(root, checked, project_id, brief_digest)
    existing_assets = existing.get("assets", []) if isinstance(existing, Mapping) else []
    merged = _merge_assets(existing_assets, fresh, additional_assets)
    candidate_payload = _canonical_payload(
        project_id=project_id,
        brief_digest=brief_digest,
        assets=merged,
        existing=existing,
        revision=(int(existing.get("revision", 0)) if existing and sha256_json(merged) == existing.get("assets_digest") else None),
        rejected_candidates=rejected_candidates,
        candidate_relationships=candidate_relationships,
    )
    reused = False
    if existing is not None:
        # Keep timestamps/revision/digest stable when the canonical facts are
        # identical. This is the idempotence boundary.
        if existing.get("project_id") != project_id:
            raise AssetLayerError("AVAILABLE_ASSETS_PROJECT_MISMATCH", "asset inventory belongs to another project")
        if (
            existing.get("assets_digest") == candidate_payload.get("assets_digest")
            and existing.get("brief_digest") == brief_digest
            and existing.get("rejected_candidates", []) == candidate_payload.get("rejected_candidates", [])
            and existing.get("candidate_relationships", []) == candidate_payload.get("candidate_relationships", [])
        ):
            candidate_payload = copy.deepcopy(existing)
            reused = True
        else:
            previous_revision = int(existing.get("revision", 0))
            candidate_payload = _canonical_payload(project_id=project_id, brief_digest=brief_digest, assets=merged, existing=existing, revision=previous_revision + 1)
    _atomic_json(root / AVAILABLE_ASSETS_RELATIVE_PATH, candidate_payload)
    result = copy.deepcopy(candidate_payload)
    result["idempotent_reuse"] = reused
    if profile is not None:
        result["local_profile_digest"] = profile.get("profile_digest")
    return result


def record_verified_candidates(
    project_root: str | os.PathLike[str],
    candidates: Sequence[Mapping[str, Any]],
    *,
    brief: Mapping[str, Any] | None = None,
    rejected_candidates: Any = None,
    candidate_relationships: Any = None,
) -> dict[str, Any]:
    """Add locally verified external candidates using the Candidate 0 standard."""

    root = resolve_project_root(project_root)
    checked, project_id, brief_digest = _load_checked_brief(root, brief)
    existing = load_available_assets(root)
    additions: list[dict[str, Any]] = []
    relations: Any = copy.deepcopy(candidate_relationships) if candidate_relationships is not None else []
    local_candidate_present = has_local_project(root) or any(
        isinstance(item, Mapping) and item.get("asset_id") == CANDIDATE_USER_PROJECT_ASSET_ID
        for item in (existing.get("assets", []) if isinstance(existing, Mapping) else [])
    )
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            raise AssetLayerError("CANDIDATE_INVALID", f"candidate[{index}] must be an object")
        verification = candidate.get("local_verification")
        if not isinstance(verification, Mapping) or verification.get("verified_locally") is not True:
            # Unverified GPT claims are deliberately not promoted into the
            # canonical established-facts inventory.
            continue
        repo_url = candidate.get("repo_url")
        if not isinstance(repo_url, str) or not repo_url:
            continue
        try:
            parsed = urlsplit(repo_url)
            if parsed.username is not None or parsed.password is not None or not parsed.hostname:
                continue
        except ValueError:
            continue
        digest = hashlib.sha256(repo_url.encode("utf-8")).hexdigest()[:16]
        established: list[str] = ["repository identity was checked by local verifier"]
        refs: list[str] = []
        for key in ("default_head", "default_ref", "verification_source", "cache_scope"):
            value = verification.get(key)
            if isinstance(value, str) and value.strip():
                established.append(f"{key}:{value.strip()[:400]}")
        structure = verification.get("code_structure")
        if isinstance(structure, Mapping):
            paths = structure.get("file_paths")
            if isinstance(paths, list):
                refs = [str(path)[:300] for path in paths[:MAX_SOURCE_REFS] if isinstance(path, str) and path.strip()]
                established.append(f"bounded_file_path_count:{len(paths)}")
        additions.append(
            _make_asset(
                asset_id="candidate-external-" + digest,
                kind="external_repository",
                role="candidate_base",
                source="discovery_local_verifier",
                local_ref_or_url=repo_url,
                verified=True,
                summary="External repository locally verified as a candidate; no preference is implied",
                capabilities=["locally reachable repository"] + (["bounded source structure inspected"] if refs else []),
                known_gaps=[],
                important_constraints=[f"claims_source:{verification.get('claims_source', 'LOCAL_VERIFIER')}"],
                protected=False,
                upload_policy="metadata_only",
                is_candidate=True,
                candidate_status="candidate_only",
                verification={"standard": "candidate_evidence.v1", "established_facts": established, "source_refs": refs, "checked_read_only": True, "claims_source": "LOCAL_VERIFIER"},
                unverified_claims=[str(candidate.get(key))[:1_000] for key in ("apparent_match", "reusable_part", "likely_gap", "expected_modification") if isinstance(candidate.get(key), str) and candidate.get(key).strip()],
            )
        )
        if local_candidate_present and isinstance(relations, list):
            # This relation expresses only that the assets must be compared;
            # it is not a recommendation or a selected architecture.
            relations.append(
                {
                    "candidate_asset_id": "candidate-external-" + digest,
                    "related_asset_id": CANDIDATE_USER_PROJECT_ASSET_ID,
                    "relationship": "comparison_required",
                    "reason": "local and external candidates use the same verified-facts standard",
                }
            )
    # Ensure the base inventory exists even when every external claim was
    # rejected.  This also guarantees Candidate 0 is auditable when present.
    base_result = collect_available_assets(
        root,
        brief=checked,
        additional_assets=additions,
        rejected_candidates=rejected_candidates,
        candidate_relationships=relations,
    )
    return base_result


def ensure_available_assets(project_root: str | os.PathLike[str] = ".", *, brief: Mapping[str, Any] | None = None) -> dict[str, Any]:
    root = resolve_project_root(project_root)
    loaded = load_available_assets(root)
    if loaded is not None:
        # Re-audit only through the canonical update path so a stale profile or
        # a newly visible local project is not silently ignored.
        return collect_available_assets(root, brief=brief)
    return collect_available_assets(root, brief=brief)


def _compact_asset(asset: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(asset))
    verification = result.get("verification")
    if isinstance(verification, Mapping):
        compact = dict(verification)
        if "established_facts" in compact:
            compact["established_facts"] = [str(item)[:2_000] for item in list(compact.get("established_facts", []))[:16]]
        if "source_refs" in compact:
            compact["source_refs"] = [str(item)[:400] for item in list(compact.get("source_refs", []))[:MAX_SOURCE_REFS]]
        result["verification"] = compact
    result["unverified_claims"] = [str(item)[:1_000] for item in list(result.get("unverified_claims", []))[:16]]
    # A pack may intentionally clip long verification metadata.  Its digest
    # describes the packed representation; the canonical inventory retains
    # the original digest and remains the source of truth.
    if result != dict(asset):
        result["digest"] = _asset_digest(result)
    return result


def build_asset_pack(
    project_root: str | os.PathLike[str] = ".",
    *,
    brief: Mapping[str, Any] | None = None,
    purpose: str = "discovery",
) -> dict[str, Any]:
    """Return a bounded metadata-only Discovery/Blueprint asset pack."""

    root = resolve_project_root(project_root)
    inventory = ensure_available_assets(root, brief=brief)
    selected = list(inventory.get("assets", []))
    priority = {"core_requirement": 0, "supporting_asset": 1, "candidate_base": 2, "baseline": 3, "evidence": 4, "tooling": 5}
    selected.sort(key=lambda item: (priority.get(str(item.get("role")), 9), str(item.get("asset_id"))))
    required_id = "requirement-project-brief"
    required = next((item for item in selected if item.get("asset_id") == required_id), None)
    bounded: list[dict[str, Any]] = []
    if required is not None:
        bounded.append(_compact_asset(required))
    for item in selected:
        if item.get("asset_id") == required_id:
            continue
        if len(bounded) >= MAX_PACK_ASSETS:
            break
        # At most four optional artifacts are carried; candidate and canonical
        # metadata remain available without turning a pack into a file list.
        if item.get("kind") == "baseline_result" and sum(entry.get("kind") == "baseline_result" for entry in bounded) >= MAX_OPTIONAL_ARTIFACTS:
            continue
        bounded.append(_compact_asset(item))
    pack: dict[str, Any] = {
        "schema_version": ASSET_PACK_SCHEMA_VERSION,
        "project_id": inventory["project_id"],
        "purpose": _safe_text(purpose, "purpose", required=True, maximum=64),
        "available_assets_digest": inventory["assets_digest"],
        "assets": bounded,
        "asset_ids": [str(item["asset_id"]) for item in bounded],
        "canonical_files": [
            ".research/PROJECT_BRIEF.json",
            ".research/PROJECT_CONTEXT.md",
            ".research/AVAILABLE_ASSETS.json",
        ] + ([LOCAL_PROJECT_PROFILE_RELATIVE_PATH.as_posix()] if inventory.get("local_project_present") else []),
        "whole_repo_attached": False,
        "upload_policy": "metadata_only; canonical files only; source on demand",
        "bounded": True,
    }
    # Keep the required brief and the highest-value candidates while trimming
    # optional metadata if many verified repositories are present.  This is a
    # metadata pack, not a reason to increase the attachment budget.
    omitted: list[str] = []
    while len(canonical_json(pack).encode("utf-8")) > MAX_PACK_BYTES and len(pack["assets"]) > 1:
        removed = pack["assets"].pop()
        if isinstance(removed, Mapping) and isinstance(removed.get("asset_id"), str):
            omitted.append(removed["asset_id"])
        pack["asset_ids"] = [str(item["asset_id"]) for item in pack["assets"]]
    if omitted:
        pack["omitted_asset_ids"] = omitted[:MAX_ASSETS]
        pack["omitted_asset_count"] = len(omitted)
    # A pack id is derived only from canonical inventory identity and the
    # bounded selection.  It never embeds a checkout path or a timestamp, so
    # repeated Discovery/Blueprint preflights can compare it safely.
    pack["pack_id"] = "asset-pack-" + sha256_json(
        {
            "project_id": pack["project_id"],
            "purpose": pack["purpose"],
            "available_assets_digest": pack["available_assets_digest"],
            "asset_ids": pack["asset_ids"],
        }
    )[:24]
    pack_resources = _pack_canonical_resources(
        root,
        include_profile=bool(inventory.get("local_project_present")),
    )
    pack["resource_ids"] = [str(item["resource_id"]) for item in pack_resources]
    pack["resource_digests"] = {
        str(item["resource_id"]): str(item["digest"]) for item in pack_resources
    }
    pack["resource_provenance"] = pack_resources
    _assert_safe(pack, path="asset_pack")
    pack["pack_digest"] = sha256_json(pack)
    encoded = canonical_json(pack)
    if len(encoded.encode("utf-8")) > MAX_PACK_BYTES:
        raise AssetLayerError("ASSET_PACK_TOO_LARGE", "bounded asset pack exceeds its limit")
    return pack


def validate_asset_pack(pack: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(pack, Mapping):
        raise AssetLayerError("ASSET_PACK_INVALID", "asset pack must be an object")
    detached = copy.deepcopy(dict(pack))
    digest = detached.pop("pack_digest", None)
    if digest != sha256_json(detached):
        raise AssetLayerError("ASSET_PACK_DIGEST_MISMATCH", "asset pack digest does not match")
    detached["pack_digest"] = digest
    if detached.get("whole_repo_attached") is not False or detached.get("bounded") is not True:
        raise AssetLayerError("ASSET_PACK_INVALID", "asset pack is not bounded")
    assets = detached.get("assets")
    if not isinstance(assets, list) or len(assets) > MAX_PACK_ASSETS:
        raise AssetLayerError("ASSET_PACK_INVALID", "asset pack exceeds its bound")
    for index, item in enumerate(assets):
        _normalise_asset(item, index)
    _assert_safe(detached, path="asset_pack")
    return detached


def normalize_composition_decision(value: Any, *, default: str = "BUILD_NEW") -> str:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise AssetLayerError("COMPOSITION_DECISION_INVALID", "base decision must be text")
    candidate = value.strip().upper().replace("-", "_").replace(" ", "_")
    aliases = {"KEEP": "KEEP_EXISTING", "BORROW": "KEEP_AND_BORROW", "REPLACE": "REPLACE_BASE", "NEW": "BUILD_NEW"}
    candidate = aliases.get(candidate, candidate)
    if candidate not in COMPOSITION_DECISIONS:
        raise AssetLayerError("COMPOSITION_DECISION_INVALID", f"unsupported composition decision: {value}")
    return candidate


# Compatibility spellings used by thin callers.
build_available_assets = collect_available_assets
update_available_assets = collect_available_assets
generate_local_project_profile = build_local_project_profile
build_discovery_asset_pack = build_asset_pack
build_blueprint_asset_pack = build_asset_pack
load_asset_inventory = load_available_assets
validate_asset_inventory = validate_available_assets


__all__ = [
    "ASSET_KINDS",
    "ASSET_LAYER_MARKER",
    "ASSET_PACK_SCHEMA_VERSION",
    "ASSET_ROLES",
    "AVAILABLE_ASSETS_FILENAME",
    "AVAILABLE_ASSETS_PATH",
    "AVAILABLE_ASSETS_RELATIVE_PATH",
    "AVAILABLE_ASSETS_SCHEMA_VERSION",
    "AvailableAssetsError",
    "AssetError",
    "AssetLayerError",
    "CANDIDATE_USER_PROJECT",
    "CANDIDATE_USER_PROJECT_ASSET_ID",
    "COMPOSITION_DECISIONS",
    "LOCAL_PROJECT_CANDIDATE_MARKER",
    "LOCAL_PROJECT_PROFILE_FILENAME",
    "LOCAL_PROJECT_PROFILE_PATH",
    "LOCAL_PROJECT_PROFILE_RELATIVE_PATH",
    "UPLOAD_POLICIES",
    "build_asset_pack",
    "build_available_assets",
    "build_blueprint_asset_pack",
    "build_discovery_asset_pack",
    "build_local_project_profile",
    "collect_available_assets",
    "ensure_available_assets",
    "generate_local_project_profile",
    "has_local_project",
    "load_asset_inventory",
    "load_available_assets",
    "load_local_project_profile",
    "normalize_composition_decision",
    "record_verified_candidates",
    "update_available_assets",
    "validate_asset_inventory",
    "validate_asset_pack",
    "validate_available_assets",
]
