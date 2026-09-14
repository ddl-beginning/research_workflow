"""Portable machine setup and startup helpers.

This module owns only the product-hardening boundary: machine-local paths,
non-secret runtime composition, browser health probing, and project discovery.
The V2 journal and lifecycle remain owned by the existing Product runtime.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .execution_profile import ExecutionProfileError, ensure_execution_profile
from .project_state import PROJECT_BRIEF_RELATIVE_PATH, load_project_brief, resolve_project_root


MACHINE_ROOT_ENV = "RESEARCH_WORKFLOW_MACHINE_ROOT"
BRIDGE_ROOT_ENV = "RESEARCH_WORKFLOW_BRIDGE_ROOT"
PRODUCT_VERSION = "2.1"
RUNTIME_CONFIG_FILENAME = "product-v2-runtime.json"
WORKSPACE_REGISTRY_FILENAME = "workspace-registry.json"
HEALTH_FILENAME = "health.json"
BRIDGE_INSTALL_DIRNAME = "bridge"
BRIDGE_ENTRYPOINT = "scripts/consult-pack.mjs"
COOKIE_EXPORT_FORBIDDEN = True
HEALTH_PROMPT = "Reply with exactly: WORKFLOW_HEALTH_OK"

_BRIDGE_RUNTIME_DIRS = frozenset(
    {
        ".auth",
        ".consultations",
        ".diagnostics",
        ".git",
        "node_modules",
        "playwright-report",
        "test-results",
    }
)

_SENSITIVE_KEY_MARKERS = (
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "cookie",
    "cookies",
    "password",
    "secret",
    "session_token",
    "storage_state",
    "token",
)


class StartupError(RuntimeError):
    """A bounded setup/startup diagnostic."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.details = copy.deepcopy(dict(details or {}))
        super().__init__(message)


@dataclass(frozen=True)
class MachinePaths:
    root: Path
    config: Path
    runtime: Path
    browser_profile: Path
    workspace_registry: Path
    health: Path

    def bounded_view(self) -> dict[str, Any]:
        return {
            "root": self.root.as_posix(),
            "config": self.config.as_posix(),
            "runtime": self.runtime.as_posix(),
            "browser_profile": self.browser_profile.as_posix(),
            "workspace_registry": self.workspace_registry.as_posix(),
            "health": self.health.as_posix(),
        }


@dataclass(frozen=True)
class ProbeResult:
    status: str
    code: str
    detail: str

    def bounded_view(self) -> dict[str, str]:
        return {"status": self.status, "code": self.code, "detail": self.detail}


def machine_paths(machine_root: str | os.PathLike[str] | None = None) -> MachinePaths:
    """Resolve the portable Windows-first machine ownership boundary."""

    selected = machine_root or os.environ.get(MACHINE_ROOT_ENV)
    if selected:
        root = Path(selected).expanduser().resolve()
    else:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            root = (Path(local_app_data) / "ResearchWorkflow").resolve()
        else:
            config_home = os.environ.get("XDG_CONFIG_HOME")
            root = ((Path(config_home) if config_home else Path.home() / ".config") / "ResearchWorkflow").resolve()
    return MachinePaths(
        root=root,
        config=root / RUNTIME_CONFIG_FILENAME,
        runtime=root / "runtime",
        browser_profile=root / "browser-profile",
        workspace_registry=root / WORKSPACE_REGISTRY_FILENAME,
        health=root / HEALTH_FILENAME,
    )


def _bridge_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root)
        if any(part in _BRIDGE_RUNTIME_DIRS for part in relative.parts):
            continue
        if candidate.name in {".env", ".env.example"} or candidate.name.startswith(".env."):
            continue
        if candidate.suffix == ".log" or candidate.name.endswith(".auth.json"):
            continue
        if candidate.name.startswith("storage-state"):
            continue
        files.append(candidate)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def _digest_files(root: Path, files: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for item in files:
        relative = item.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def bridge_source_identity(bridge_root: str | os.PathLike[str]) -> dict[str, Any]:
    """Return the bounded, reproducible identity of one Bridge source tree."""

    root = Path(bridge_root).expanduser().resolve()
    package_path = root / "package.json"
    lock_path = root / "package-lock.json"
    entrypoint = root / BRIDGE_ENTRYPOINT
    if not root.is_dir() or not package_path.is_file() or not lock_path.is_file() or not entrypoint.is_file():
        raise StartupError("BRIDGE_SOURCE_INVALID", "packaged Browser Bridge source is incomplete")
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StartupError("BRIDGE_SOURCE_INVALID", "Browser Bridge package metadata is unreadable") from exc
    if not isinstance(package, Mapping) or package.get("name") != "chatgpt-browser-bridge":
        raise StartupError("BRIDGE_SOURCE_INVALID", "Browser Bridge package name is invalid")
    version = package.get("version")
    dependencies = package.get("dependencies")
    if not isinstance(version, str) or not version.strip() or not isinstance(dependencies, Mapping):
        raise StartupError("BRIDGE_SOURCE_INVALID", "Browser Bridge package metadata is incomplete")
    files = _bridge_source_files(root)
    return {
        "version": version.strip(),
        "source_digest": _digest_files(root, files),
        "dependency_lock_digest": hashlib.sha256(lock_path.read_bytes()).hexdigest(),
        "entrypoint": BRIDGE_ENTRYPOINT,
        "dependencies": {str(key): str(value) for key, value in sorted(dependencies.items(), key=lambda pair: str(pair[0]))},
    }


def provision_bridge(
    paths: MachinePaths,
    source_root: str | os.PathLike[str],
    *,
    node_executable: str = "node",
) -> tuple[Path, dict[str, Any], bool]:
    """Copy packaged Bridge source and install its lockfile dependencies.

    The installed tree is machine-local and deliberately keeps auth/profile
    state outside the versioned source boundary. Re-running setup reuses the
    same provision root and does not create another Bridge copy.
    """

    source = Path(source_root).expanduser().resolve(strict=True)
    identity = bridge_source_identity(source)
    target = paths.root / BRIDGE_INSTALL_DIRNAME
    previous_identity: dict[str, Any] | None = None
    try:
        previous_identity = bridge_source_identity(target)
    except (OSError, RuntimeError, StartupError):
        previous_identity = None
    previous_dependencies_installed = (
        (target / "node_modules" / "playwright").is_dir()
        and (target / "node_modules" / "@modelcontextprotocol" / "sdk").is_dir()
    )
    changed = previous_identity != identity or not previous_dependencies_installed
    target.mkdir(parents=True, exist_ok=True)
    for item in _bridge_source_files(source):
        relative = item.relative_to(source)
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
    target_identity = bridge_source_identity(target)
    if target_identity["source_digest"] != identity["source_digest"]:
        raise StartupError("BRIDGE_PROVISION_FAILED", "provisioned Browser Bridge source digest does not match packaged source")
    node = shutil.which(node_executable) or (str(node_executable) if Path(str(node_executable)).is_file() else None)
    if not node:
        raise StartupError("NODE_NOT_FOUND", "Node.js was not found")
    npm = shutil.which("npm")
    if not npm:
        raise StartupError("NPM_NOT_FOUND", "npm was not found")
    dependencies_installed = (target / "node_modules" / "playwright").is_dir() and (target / "node_modules" / "@modelcontextprotocol" / "sdk").is_dir()
    if not dependencies_installed:
        try:
            completed = subprocess.run(
                [npm, "ci", "--no-audit", "--no-fund"],
                cwd=str(target),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise StartupError("BRIDGE_DEPENDENCY_INSTALL_FAILED", "Browser Bridge dependencies could not be installed") from exc
        if completed.returncode != 0:
            raise StartupError(
                "BRIDGE_DEPENDENCY_INSTALL_FAILED",
                "Browser Bridge dependencies could not be installed",
                details={"returncode": int(completed.returncode)},
            )
        dependencies_installed = True
    if not dependencies_installed:
        raise StartupError("BRIDGE_DEPENDENCY_INSTALL_FAILED", "Browser Bridge dependencies are incomplete")
    return target, target_identity, changed


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _reject_sensitive_keys(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _SENSITIVE_KEY_MARKERS:
                raise StartupError("MACHINE_CONFIG_SECRET", f"machine config contains a forbidden field at {path}.{key}")
            _reject_sensitive_keys(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_sensitive_keys(child, path=f"{path}[{index}]")


def load_machine_config(paths: MachinePaths) -> dict[str, Any] | None:
    if not paths.config.is_file():
        return None
    try:
        raw = json.loads(paths.config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StartupError("MACHINE_CONFIG_INVALID", "machine-local runtime config is unreadable") from exc
    if not isinstance(raw, Mapping):
        raise StartupError("MACHINE_CONFIG_INVALID", "machine-local runtime config must be an object")
    _reject_sensitive_keys(raw)
    return copy.deepcopy(dict(raw))


def discover_bridge_root(
    explicit: str | os.PathLike[str] | None = None,
    *,
    product_root: str | os.PathLike[str] | None = None,
    configured: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Find a separate Browser Bridge checkout without a machine hardcode."""

    candidates: list[Path] = []
    for value in (explicit, configured, os.environ.get(BRIDGE_ROOT_ENV)):
        if value:
            candidates.append(Path(value).expanduser())
    if product_root:
        root = Path(product_root).expanduser().resolve()
        # A release checkout owns its packaged Bridge. The historical
        # sibling-directory locations remain a compatibility fallback only.
        candidates.extend((root / "bridge", root.parent / "chatgpt_browser_bridge", root / "chatgpt_browser_bridge"))
    cwd = Path.cwd().resolve()
    candidates.extend((cwd / "chatgpt_browser_bridge", cwd.parent / "chatgpt_browser_bridge"))
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_dir() and (resolved / "scripts" / "consult-pack.mjs").is_file():
            return resolved
    return None


def default_runtime_config(
    paths: MachinePaths,
    *,
    bridge_root: Path | None,
    browser_profile: Path | None = None,
    codex_executable: str | None = None,
    node_executable: str = "node",
    project_url: str | None = None,
) -> dict[str, Any]:
    profile = (browser_profile or paths.browser_profile).resolve()
    identity: dict[str, Any] = {}
    if bridge_root is not None:
        try:
            identity = bridge_source_identity(bridge_root)
        except StartupError:
            # Test seams and legacy external bridges may expose only the
            # consult script. The strict identity gate is applied whenever a
            # packaged source is provisioned by ``workflow setup``.
            identity = {}
    return {
        "schema_version": "runtime_composition.v1",
        "lifecycle_version": "v2",
        "stage": {"state_path": ".workflow-v2/journal.json"},
        "executor": {
            "providers": ["openai-codex"],
            "codex_executable": codex_executable,
            "preferred_model": "gpt-5.6-luna",
            "fallback_model": None,
            "auth_mode": "chatgpt",
            "timeout_seconds": 600,
            "artifact_dir": paths.runtime.as_posix(),
        },
        "bridge": {
            "enabled": bridge_root is not None,
            "root": bridge_root.as_posix() if bridge_root else None,
            "profile_dir": profile.as_posix(),
            "node_executable": node_executable,
            "entrypoint": identity.get("entrypoint", BRIDGE_ENTRYPOINT),
            "version": identity.get("version"),
            "source_digest": identity.get("source_digest"),
            "dependency_lock_digest": identity.get("dependency_lock_digest"),
            "source_mode": "packaged" if identity else "external",
            "transport": "homepage_fallback" if project_url is None else None,
            "project_url": project_url,
        },
    }


def ensure_machine_config(
    paths: MachinePaths,
    *,
    bridge_root: Path | None,
    browser_profile: Path | None = None,
    codex_executable: str | None = None,
    node_executable: str = "node",
    project_url: str | None = None,
    force: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Create the non-secret machine config, preserving existing choices."""

    current = load_machine_config(paths)
    if current is not None and not force:
        return current, False
    config = default_runtime_config(
        paths,
        bridge_root=bridge_root,
        browser_profile=browser_profile,
        codex_executable=codex_executable,
        node_executable=node_executable,
        project_url=project_url,
    )
    _reject_sensitive_keys(config)
    _atomic_json(paths.config, config)
    return config, True


def ensure_machine_directories(paths: MachinePaths) -> None:
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.runtime.mkdir(parents=True, exist_ok=True)
    paths.browser_profile.mkdir(parents=True, exist_ok=True)


def record_workspace(paths: MachinePaths, project_root: str | os.PathLike[str], project_id: str) -> dict[str, Any]:
    """Maintain a regenerable, non-authoritative machine workspace registry."""

    entries: list[dict[str, str]] = []
    if paths.workspace_registry.is_file():
        try:
            raw = json.loads(paths.workspace_registry.read_text(encoding="utf-8"))
            if isinstance(raw, Mapping) and isinstance(raw.get("workspaces"), list):
                entries = [
                    {"project_id": str(item.get("project_id")), "path": str(item.get("path"))}
                    for item in raw["workspaces"]
                    if isinstance(item, Mapping) and item.get("project_id") and item.get("path")
                ]
        except (OSError, UnicodeError, json.JSONDecodeError):
            entries = []
    root = resolve_project_root(project_root)
    entry = {"project_id": str(project_id), "path": root.as_posix()}
    entries = [item for item in entries if item != entry and item.get("project_id") != project_id]
    entries.append(entry)
    _atomic_json(paths.workspace_registry, {"schema_version": "workspace_registry.v1", "workspaces": entries})
    return entry


def load_health(paths: MachinePaths) -> dict[str, Any] | None:
    if not paths.health.is_file():
        return None
    try:
        raw = json.loads(paths.health.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return dict(raw) if isinstance(raw, Mapping) else None


def _write_health(paths: MachinePaths, result: ProbeResult) -> None:
    _atomic_json(
        paths.health,
        {
            "schema_version": "workflow_browser_health.v1",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "status": result.status,
            "code": result.code,
        },
    )


def probe_browser(
    config: Mapping[str, Any],
    paths: MachinePaths,
    *,
    command_runner: Callable[..., Any] | None = None,
    timeout_seconds: int = 180,
) -> ProbeResult:
    """Run one bounded bridge health prompt without exposing browser state."""

    bridge = config.get("bridge") if isinstance(config.get("bridge"), Mapping) else {}
    root_value = bridge.get("root")
    profile_value = bridge.get("profile_dir")
    node_value = bridge.get("node_executable", "node")
    if not bridge.get("enabled"):
        result = ProbeResult("FAIL", "BRIDGE_CONFIGURATION_REQUIRED", "Browser Bridge is not configured")
        _write_health(paths, result)
        return result
    if not isinstance(root_value, str) or not (Path(root_value).expanduser() / "scripts" / "consult.mjs").is_file():
        result = ProbeResult("FAIL", "BRIDGE_SCRIPT_MISSING", "Browser Bridge consult entry is missing")
        _write_health(paths, result)
        return result
    profile = Path(profile_value).expanduser() if isinstance(profile_value, str) else paths.browser_profile
    profile.mkdir(parents=True, exist_ok=True)
    node = shutil.which(str(node_value)) or (str(node_value) if Path(str(node_value)).is_file() else None)
    if not node:
        result = ProbeResult("FAIL", "NODE_NOT_FOUND", "Node.js was not found")
        _write_health(paths, result)
        return result
    argv = [node, str(Path(root_value).expanduser() / "scripts" / "consult.mjs"), "--prompt", HEALTH_PROMPT, "--profile-dir", str(profile)]
    runner = command_runner or subprocess.run
    try:
        completed = runner(argv, cwd=str(Path(root_value).expanduser()), capture_output=True, text=True, timeout=timeout_seconds, check=False)
        stdout = str(getattr(completed, "stdout", "") or "")
        stderr = str(getattr(completed, "stderr", "") or "")
        returncode = int(getattr(completed, "returncode", 1))
    except subprocess.TimeoutExpired:
        result = ProbeResult("FAIL", "GPT_BROWSER_TIMEOUT", "GPT browser health probe timed out")
        _write_health(paths, result)
        return result
    except (OSError, ValueError):
        result = ProbeResult("FAIL", "GPT_BROWSER_UNAVAILABLE", "GPT browser health probe could not start")
        _write_health(paths, result)
        return result
    combined = f"{stdout}\n{stderr}"
    if "LOGIN_REQUIRED" in combined:
        result = ProbeResult("FAIL", "GPT_AUTH_REQUIRED", "Sign in to ChatGPT in the dedicated browser profile")
    elif returncode == 0 and "WORKFLOW_HEALTH_OK" in combined:
        result = ProbeResult("PASS", "OK", "GPT browser profile and composer are ready")
    else:
        result = ProbeResult("FAIL", "GPT_BROWSER_PROBE_FAILED", "GPT browser health probe did not complete")
    _write_health(paths, result)
    return result


def project_identity_present(project_root: str | os.PathLike[str]) -> bool:
    root = resolve_project_root(project_root)
    return (root / PROJECT_BRIEF_RELATIVE_PATH).is_file()


def project_brief_summary(project_root: str | os.PathLike[str]) -> dict[str, Any] | None:
    root = resolve_project_root(project_root)
    try:
        raw = load_project_brief(root)
    except Exception as exc:
        raise StartupError("PROJECT_BRIEF_UNREADABLE", "canonical project brief could not be read") from exc
    if raw is None:
        return None
    return {
        "project_id": raw.get("project_id"),
        "project_name": (raw.get("brief") or {}).get("project_name") or (raw.get("brief") or {}).get("title") or root.name,
        "state": raw.get("state"),
        "profile": raw.get("execution_profile"),
    }


def persist_profile(project_root: str | os.PathLike[str]) -> dict[str, Any]:
    try:
        return ensure_execution_profile(project_root, create=True)
    except ExecutionProfileError as exc:
        raise StartupError(exc.code, str(exc)) from exc


def reset_machine_config(paths: MachinePaths) -> bool:
    """Remove only the generated runtime config; preserve project/history/auth."""

    if not paths.config.exists():
        return False
    if not paths.config.is_file():
        raise StartupError("MACHINE_CONFIG_INVALID", "machine config path is not a file")
    paths.config.unlink()
    return True


def secret_scan_paths(paths: Sequence[Path]) -> dict[str, Any]:
    """Scan setup artifacts for forbidden material without printing contents."""

    suspicious: list[str] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lowered = text.casefold()
        if any(marker in lowered for marker in ("cookie_export", "storage_state", "access_token", "auth_token", "password")):
            suspicious.append(path.as_posix())
    return {"passed": not suspicious, "checked": [path.as_posix() for path in paths], "suspicious": suspicious}


__all__ = [
    "BRIDGE_ENTRYPOINT",
    "BRIDGE_INSTALL_DIRNAME",
    "BRIDGE_ROOT_ENV",
    "COOKIE_EXPORT_FORBIDDEN",
    "HEALTH_PROMPT",
    "MACHINE_ROOT_ENV",
    "MachinePaths",
    "PRODUCT_VERSION",
    "ProbeResult",
    "StartupError",
    "default_runtime_config",
    "bridge_source_identity",
    "discover_bridge_root",
    "ensure_machine_config",
    "ensure_machine_directories",
    "load_health",
    "load_machine_config",
    "machine_paths",
    "persist_profile",
    "provision_bridge",
    "probe_browser",
    "project_brief_summary",
    "project_identity_present",
    "record_workspace",
    "reset_machine_config",
    "secret_scan_paths",
]
