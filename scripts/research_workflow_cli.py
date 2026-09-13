#!/usr/bin/env python3
"""Install and inspect the local research-workflow Codex integration.

The command is intentionally a small, configuration-driven boundary:

* the runtime root defaults to this repository (derived from this file);
* the MCP registration is managed only through the official codex mcp
  commands;
* the user-scope skill is copied atomically from the repository skill;
* status/doctor expose bounded facts and never print environment values,
  command stderr, credentials, or raw MCP configuration.

It is not a GUI installer and it does not start a workflow, a bridge, or a
Codex turn.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


# Direct script execution sets ``sys.path[0]`` to ``scripts/``.  Keep the
# product source importable from any caller working directory without adding a
# machine-specific checkout path or requiring installation as a package.
_PRODUCT_ROOT = Path(__file__).resolve().parents[1]
if str(_PRODUCT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PRODUCT_ROOT))


DEFAULT_MCP_NAME = "research-supervisor"
DEFAULT_SKILL_NAME = "research-workflow"
SKILL_QUICK_VALIDATE_ENV = "CODEX_SKILL_QUICK_VALIDATE"
CODEX_EXECUTABLE_ENV = "CODEX_CLI_PATH"
MAX_DIAGNOSTIC_TEXT = 512
MCP_APPROVAL_MODE = "approve"


class CLIError(RuntimeError):
    """A bounded command-line failure."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.details = copy.deepcopy(dict(details or {}))
        super().__init__(message)

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": "research_workflow_cli_error.v1",
            "error": {"code": self.code, "message": str(self)},
        }
        if self.details:
            result["details"] = copy.deepcopy(self.details)
        return result


@dataclass(frozen=True)
class RuntimePaths:
    runtime_root: Path
    launcher: Path
    skill_source: Path
    user_skill_root: Path
    skill_target: Path
    codex_executable: Path | None
    python_executable: Path
    quick_validate: Path | None
    mcp_name: str = DEFAULT_MCP_NAME

    def bounded_view(self) -> dict[str, Any]:
        return {
            "runtime_root": self.runtime_root.as_posix(),
            "launcher": self.launcher.as_posix(),
            "skill_source": self.skill_source.as_posix(),
            "user_skill_root": self.user_skill_root.as_posix(),
            "skill_target": self.skill_target.as_posix(),
            "codex_executable_present": self.codex_executable is not None,
            "python_executable": self.python_executable.as_posix(),
            "quick_validate_present": self.quick_validate is not None,
            "mcp_name": self.mcp_name,
        }


@dataclass(frozen=True)
class CommandOutcome:
    returncode: int
    stdout: str = ""


def _bounded_output(value: str) -> str:
    """Keep a safe diagnostic marker without copying arbitrary subprocess text."""

    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_DIAGNOSTIC_TEXT]


def _resolve_existing_directory(value: str | os.PathLike[str], field: str) -> Path:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CLIError("PATH_INVALID", f"{field} is not an existing directory") from exc
    if not resolved.is_dir():
        raise CLIError("PATH_INVALID", f"{field} is not an existing directory")
    return resolved


def _resolve_file(value: str | os.PathLike[str], field: str, *, required: bool = True) -> Path | None:
    candidate = Path(value).expanduser()
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        if required:
            raise CLIError("PATH_INVALID", f"{field} is not a valid path") from exc
        return None
    if required and not resolved.is_file():
        raise CLIError("DEPENDENCY_MISSING", f"{field} was not found")
    return resolved if resolved.is_file() else None


def _discover_codex(configured: str | None) -> Path | None:
    value = configured or os.environ.get(CODEX_EXECUTABLE_ENV)
    if value:
        explicit = Path(value).expanduser()
        if explicit.is_file():
            return explicit.resolve()
        discovered = shutil.which(value)
        return Path(discovered).resolve() if discovered else None
    discovered = shutil.which("codex")
    return Path(discovered).resolve() if discovered else None


def _discover_python(configured: str | os.PathLike[str] | None) -> Path:
    value = str(configured) if configured is not None else sys.executable
    explicit = Path(value).expanduser()
    if explicit.is_file():
        return explicit.resolve()
    discovered = shutil.which(value)
    if discovered:
        return Path(discovered).resolve()
    raise CLIError("DEPENDENCY_MISSING", "Python executable was not found by explicit path or PATH")


def _discover_quick_validate(configured: str | None) -> Path | None:
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    env_path = os.environ.get(SKILL_QUICK_VALIDATE_ENV)
    if env_path:
        candidates.append(Path(env_path).expanduser())
    home = Path.home()
    candidates.extend(
        [
            home / ".codex" / "skills" / ".system" / "skill-creator" / "scripts" / "quick_validate.py",
            home / ".agents" / "skills" / ".system" / "skill-creator" / "scripts" / "quick_validate.py",
        ]
    )
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if resolved.is_file():
            return resolved
    return None


def discover_paths(
    *,
    runtime_root: str | os.PathLike[str] | None = None,
    skill_source: str | os.PathLike[str] | None = None,
    user_skill_root: str | os.PathLike[str] | None = None,
    codex_executable: str | None = None,
    python_executable: str | os.PathLike[str] | None = None,
    quick_validate: str | os.PathLike[str] | None = None,
    mcp_name: str = DEFAULT_MCP_NAME,
) -> RuntimePaths:
    """Resolve portable runtime paths without assuming a checkout location."""

    default_root = Path(__file__).resolve().parents[1]
    root = _resolve_existing_directory(runtime_root or default_root, "runtime_root")
    launcher = _resolve_file(root / "scripts" / "workflow_mcp.py", "workflow MCP launcher")
    assert launcher is not None
    source = _resolve_existing_directory(
        skill_source or root / "skills" / "stage-oriented-research-workflow",
        "skill source",
    )
    user_root = Path(user_skill_root or (Path.home() / ".agents" / "skills")).expanduser().resolve()
    python_path = _discover_python(python_executable)
    return RuntimePaths(
        runtime_root=root,
        launcher=launcher,
        skill_source=source,
        user_skill_root=user_root,
        skill_target=user_root / DEFAULT_SKILL_NAME,
        codex_executable=_discover_codex(codex_executable),
        python_executable=python_path,
        quick_validate=_discover_quick_validate(str(quick_validate) if quick_validate else None),
        mcp_name=mcp_name,
    )


def _run_command(argv: Sequence[str], *, cwd: Path | None = None) -> CommandOutcome:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except (OSError, ValueError) as exc:
        raise CLIError("COMMAND_UNAVAILABLE", "required command could not be started") from exc
    return CommandOutcome(completed.returncode, completed.stdout or "")


def _require_codex(paths: RuntimePaths) -> Path:
    if paths.codex_executable is None:
        raise CLIError("CODEX_NOT_FOUND", "Codex CLI was not found by explicit path or PATH")
    return paths.codex_executable


def _codex_json(paths: RuntimePaths, args: Sequence[str]) -> Any:
    codex = _require_codex(paths)
    result = _run_command([str(codex), *args])
    if result.returncode != 0:
        raise CLIError("CODEX_COMMAND_FAILED", f"codex {' '.join(args[:2])} failed", details={"exit_code": result.returncode})
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CLIError("CODEX_OUTPUT_INVALID", "Codex returned non-JSON MCP data") from exc


def _mcp_entries(payload: Any) -> list[dict[str, Any]]:
    value = payload
    if isinstance(value, Mapping):
        value = value.get("servers", value.get("mcp_servers", value.get("data", value)))
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _transport(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    value = entry.get("transport")
    return value if isinstance(value, Mapping) else entry


def _registration_summary(entry: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    transport = _transport(entry)
    command = transport.get("command")
    args = transport.get("args", [])
    env = transport.get("env", {})
    cwd = transport.get("cwd")
    return {
        "name": entry.get("name"),
        "transport_type": transport.get("type", "stdio"),
        "command": str(command) if isinstance(command, str) else None,
        "args": [str(item) for item in args] if isinstance(args, list) else [],
        "env_keys": sorted(str(key) for key in env) if isinstance(env, Mapping) else [],
        "cwd_configured": isinstance(cwd, str) and bool(cwd.strip()),
        "enabled": entry.get("enabled", True) is not False,
    }


def _path_token_equal(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str):
        return left == right
    if left == right:
        return True
    try:
        return os.path.normcase(str(Path(left).expanduser().resolve())) == os.path.normcase(
            str(Path(right).expanduser().resolve())
        )
    except (OSError, RuntimeError, ValueError):
        return False


def registration_matches(entry: Mapping[str, Any] | None, paths: RuntimePaths) -> bool:
    """Compare only the target server's bounded stdio identity."""

    if entry is None:
        return False
    transport = _transport(entry)
    if transport.get("type", "stdio") != "stdio":
        return False
    command = transport.get("command")
    args = transport.get("args", [])
    if paths.codex_executable is None or not _path_token_equal(command, str(paths.python_executable)):
        return False
    if not isinstance(args, list) or len(args) != 1 or not _path_token_equal(args[0], str(paths.launcher)):
        return False
    env = transport.get("env", {})
    if isinstance(env, Mapping) and env:
        return False
    return True


def _find_registration(entries: Iterable[Mapping[str, Any]], name: str) -> dict[str, Any] | None:
    for entry in entries:
        if entry.get("name") == name:
            return dict(entry)
    return None


def _mcp_snapshot(paths: RuntimePaths) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    payload = _codex_json(paths, ["mcp", "list", "--json"])
    entries = _mcp_entries(payload)
    return entries, _find_registration(entries, paths.mcp_name)


def run_quick_validate(paths: RuntimePaths, skill_path: Path) -> dict[str, Any]:
    if paths.quick_validate is None:
        return {
            "ok": False,
            "code": "QUICK_VALIDATE_NOT_FOUND",
            "message": "skill-creator quick_validate.py was not discovered",
        }
    result = _run_command([str(paths.python_executable), str(paths.quick_validate), str(skill_path)])
    return {
        "ok": result.returncode == 0,
        "code": "OK" if result.returncode == 0 else "SKILL_INVALID",
        "message": "skill passed quick_validate" if result.returncode == 0 else "skill failed quick_validate",
        "exit_code": result.returncode,
    }


def skill_fingerprint(skill_path: Path) -> dict[str, Any]:
    if not skill_path.is_dir():
        return {"exists": False, "sha256": None, "files": []}
    digest = hashlib.sha256()
    files: list[str] = []
    for item in sorted((path for path in skill_path.rglob("*") if path.is_file()), key=lambda path: path.as_posix()):
        relative = item.relative_to(skill_path).as_posix()
        files.append(relative)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return {"exists": True, "sha256": digest.hexdigest(), "files": files}


def _copy_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent), delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(source.read_bytes())
    try:
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def _append_receipt(path: Path, *, operation: str, exit_code: int, result: Mapping[str, Any]) -> None:
    """Persist bounded local verification metadata without subprocess text."""

    existing: dict[str, Any] = {
        "schema_version": "research_workflow_cli_receipt.v1",
        "operations": [],
    }
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping) and isinstance(loaded.get("operations"), list):
                existing["operations"] = list(loaded["operations"])
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise CLIError("RECEIPT_INVALID", "verification receipt exists but is unreadable")
    existing["operations"].append(
        {
            "operation": operation,
            "exit_code": int(exit_code),
            "result": copy.deepcopy(dict(result)),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        delete=False,
        mode="w",
        encoding="utf-8",
        newline="\n",
    ) as handle:
        temporary = Path(handle.name)
        json.dump(existing, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def install_skill(paths: RuntimePaths) -> dict[str, Any]:
    source_validation = run_quick_validate(paths, paths.skill_source)
    if not source_validation["ok"]:
        raise CLIError("SKILL_INVALID", source_validation["message"], details={"validation": source_validation})
    before = skill_fingerprint(paths.skill_target)
    source_files = [path for path in paths.skill_source.rglob("*") if path.is_file()]
    for source_file in source_files:
        relative = source_file.relative_to(paths.skill_source)
        _copy_atomic(source_file, paths.skill_target / relative)
    target_validation = run_quick_validate(paths, paths.skill_target)
    if not target_validation["ok"]:
        raise CLIError("SKILL_INSTALL_INVALID", target_validation["message"], details={"validation": target_validation})
    after = skill_fingerprint(paths.skill_target)
    return {
        "before": before,
        "after": after,
        "source": skill_fingerprint(paths.skill_source),
        "changed": before.get("sha256") != after.get("sha256"),
        "validation": target_validation,
    }


def _expected_registration(paths: RuntimePaths) -> dict[str, Any]:
    return {
        "name": paths.mcp_name,
        "transport_type": "stdio",
        "command": str(paths.python_executable),
        "args": [str(paths.launcher)],
        "env_keys": [],
        "cwd_configured": False,
        "enabled": True,
    }


def _codex_config_path() -> Path:
    """Resolve the user-scope Codex config without assuming a host platform."""

    configured_home = os.environ.get("CODEX_HOME")
    config_root = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    return (config_root / "config.toml").resolve()


def _mcp_parent_table(text: str, name: str) -> tuple[list[str], int | None, int | None]:
    """Return config lines and the direct parent MCP table span.

    The span stops at the next TOML table, so nested ``tools.<tool>`` tables do
    not accidentally receive a parent-level setting.
    """

    lines = text.splitlines(keepends=True)
    header = f"[mcp_servers.{name}]"
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == header:
            start = index
            break
    if start is None:
        return lines, None, None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    return lines, start, end


def _mcp_approval_summary(paths: RuntimePaths) -> dict[str, Any]:
    """Read only the bounded approval metadata for the target MCP server."""

    config_path = _codex_config_path()
    result: dict[str, Any] = {
        "config_path": config_path.as_posix(),
        "config_present": config_path.is_file(),
        "server_table_present": False,
        "default_tools_approval_mode": None,
        "valid": False,
    }
    if not config_path.is_file():
        result["code"] = "CODEX_CONFIG_MISSING"
        return result
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        result["code"] = "CODEX_CONFIG_UNREADABLE"
        return result
    lines, start, end = _mcp_parent_table(text, paths.mcp_name)
    if start is None or end is None:
        result["code"] = "MCP_TABLE_MISSING"
        return result
    result["server_table_present"] = True
    pattern = re.compile(r"^\s*default_tools_approval_mode\s*=\s*(['\"])([^'\"]+)\1\s*(?:#.*)?$")
    values = [match.group(2) for line in lines[start + 1 : end] if (match := pattern.match(line.rstrip("\r\n")))]
    mode = values[-1] if values else None
    result["default_tools_approval_mode"] = mode
    result["valid"] = mode == MCP_APPROVAL_MODE
    result["code"] = "OK" if result["valid"] else "MCP_APPROVAL_NOT_CONFIGURED"
    return result


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    original_mode = path.stat().st_mode if path.exists() else None
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        delete=False,
        mode="w",
        encoding="utf-8",
        newline="",
    ) as handle:
        temporary = Path(handle.name)
        handle.write(text)
    try:
        if original_mode is not None:
            os.chmod(temporary, original_mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _ensure_mcp_approval_config(paths: RuntimePaths) -> dict[str, Any]:
    """Persist the official parent-table ``approve`` policy idempotently."""

    config_path = _codex_config_path()
    if not config_path.is_file():
        raise CLIError("CODEX_CONFIG_MISSING", "Codex user config was not found after MCP registration")
    try:
        original = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CLIError("CODEX_CONFIG_UNREADABLE", "Codex user config could not be read") from exc
    lines, start, end = _mcp_parent_table(original, paths.mcp_name)
    if start is None or end is None:
        raise CLIError("MCP_TABLE_MISSING", "target MCP table was not found in Codex user config")
    newline = "\r\n" if "\r\n" in original else "\n"
    pattern = re.compile(r"^\s*default_tools_approval_mode\s*=")
    matches = [index for index in range(start + 1, end) if pattern.match(lines[index].rstrip("\r\n"))]
    replacement = f'default_tools_approval_mode = "{MCP_APPROVAL_MODE}"{newline}'
    changed = False
    if matches:
        if lines[matches[0]] != replacement:
            lines[matches[0]] = replacement
            changed = True
        for index in reversed(matches[1:]):
            del lines[index]
            changed = True
    else:
        lines.insert(start + 1, replacement)
        changed = True
    updated = "".join(lines)
    if updated != original:
        _write_text_atomic(config_path, updated)
    summary = _mcp_approval_summary(paths)
    if not summary["valid"]:
        raise CLIError("MCP_APPROVAL_CONFIG_INVALID", "Codex MCP default approval is not approve", details={"approval": summary})
    return {"changed": changed, "config_path": config_path.as_posix(), "default_tools_approval_mode": MCP_APPROVAL_MODE}


def _restore_registration_argv(entry: Mapping[str, Any], name: str, codex: Path) -> list[str] | None:
    """Build a conservative compensating add command for a prior stdio entry.

    The installer never copies environment values or URL credentials.  A
    registration with env/cwd/URL state is therefore not auto-restored after a
    failed replacement; the failure is reported for explicit operator repair.
    """

    transport = _transport(entry)
    if transport.get("type", "stdio") != "stdio":
        return None
    command = transport.get("command")
    args = transport.get("args", [])
    env = transport.get("env")
    env_vars = transport.get("env_vars", [])
    cwd = transport.get("cwd")
    if (
        not isinstance(command, str)
        or not isinstance(args, list)
        or env not in (None, {})
        or env_vars not in (None, [], {})
        or cwd not in (None, "")
    ):
        return None
    values = [command, *args]
    if any(
        not isinstance(value, str)
        or any(marker in value.casefold() for marker in ("token", "secret", "password", "cookie", "api_key", "apikey"))
        for value in values
    ):
        return None
    return [str(codex), "mcp", "add", name, "--", command, *[str(value) for value in args]]


def ensure_registration(paths: RuntimePaths, *, force: bool = False) -> dict[str, Any]:
    codex = _require_codex(paths)
    _, existing = _mcp_snapshot(paths)
    before = _registration_summary(existing)
    changed = "unchanged"
    if force or existing is None or not registration_matches(existing, paths):
        if existing is not None:
            # Do not remove an existing registration that cannot be rebuilt
            # without copying URL credentials or environment values.  A
            # failed replacement must preserve unrelated operator state.
            if _restore_registration_argv(existing, paths.mcp_name, codex) is None:
                raise CLIError(
                    "MCP_REPLACEMENT_UNSAFE",
                    "existing target MCP registration is not safely restorable; refusing to remove it",
                    details={"registration": _registration_summary(existing)},
                )
            result = _run_command([str(codex), "mcp", "remove", paths.mcp_name])
            if result.returncode != 0:
                raise CLIError("MCP_REMOVE_FAILED", "existing target MCP registration could not be removed", details={"exit_code": result.returncode})
            changed = "replaced"
        else:
            changed = "created"
        result = _run_command(
            [
                str(codex),
                "mcp",
                "add",
                paths.mcp_name,
                "--",
                str(paths.python_executable),
                str(paths.launcher),
            ]
        )
        if result.returncode != 0:
            restored = False
            if existing is not None:
                restore_argv = _restore_registration_argv(existing, paths.mcp_name, codex)
                if restore_argv is not None:
                    restore = _run_command(restore_argv)
                    restored = restore.returncode == 0
            code = "MCP_ADD_FAILED_RESTORED" if restored else "MCP_ADD_FAILED_ROLLBACK_UNAVAILABLE"
            raise CLIError(
                code,
                "target MCP registration could not be added; prior registration was restored"
                if restored
                else "target MCP registration could not be added and prior registration could not be restored automatically",
                details={"exit_code": result.returncode, "restored": restored},
            )
    approval = _ensure_mcp_approval_config(paths)
    _, registered = _mcp_snapshot(paths)
    if not registration_matches(registered, paths):
        raise CLIError(
            "MCP_REGISTRATION_INVALID",
            "Codex MCP registration does not match the runtime launcher",
            details={"registration": _registration_summary(registered), "expected": _expected_registration(paths)},
        )
    return {
        "changed": changed,
        "before": before,
        "after": _registration_summary(registered),
        "expected": _expected_registration(paths),
        "approval": approval,
    }


def inspect(paths: RuntimePaths) -> dict[str, Any]:
    missing: list[dict[str, str]] = []
    for relative, component in (
        (Path("src") / "__init__.py", "runtime.package"),
        (Path("src") / "workflow_mcp.py", "runtime.mcp_module"),
    ):
        if not (paths.runtime_root / relative).is_file():
            missing.append({"component": component, "code": "DEPENDENCY_MISSING", "message": f"{component} is missing"})
    source = skill_fingerprint(paths.skill_source)
    target = skill_fingerprint(paths.skill_target)
    source_validation = (
        run_quick_validate(paths, paths.skill_source)
        if source["exists"]
        else {"ok": False, "code": "SKILL_SOURCE_MISSING", "message": "repository skill source is missing"}
    )
    target_validation = (
        run_quick_validate(paths, paths.skill_target)
        if target["exists"]
        else {"ok": False, "code": "SKILL_TARGET_MISSING", "message": "user-scope skill is missing"}
    )
    if not paths.launcher.is_file():
        missing.append({"component": "launcher", "code": "LAUNCHER_MISSING", "message": "workflow MCP launcher is missing"})
    if not source["exists"] or not source_validation["ok"]:
        missing.append({"component": "skill.source", "code": source_validation["code"], "message": source_validation["message"]})
    if not target["exists"] or not target_validation["ok"]:
        missing.append({"component": "skill.user_scope", "code": target_validation["code"], "message": target_validation["message"]})
    if source["exists"] and target["exists"] and source["sha256"] != target["sha256"]:
        missing.append({"component": "skill.user_scope", "code": "SKILL_HASH_MISMATCH", "message": "user-scope skill differs from repository skill"})

    registration: dict[str, Any] | None = None
    approval = _mcp_approval_summary(paths)
    registration_error: dict[str, Any] | None = None
    if paths.codex_executable is None:
        missing.append({"component": "codex", "code": "CODEX_NOT_FOUND", "message": "Codex CLI was not found"})
    else:
        try:
            _, entry = _mcp_snapshot(paths)
            registration = _registration_summary(entry)
            if entry is None:
                missing.append({"component": "mcp.registration", "code": "MCP_NOT_REGISTERED", "message": "target MCP server is not registered"})
            elif not registration_matches(entry, paths):
                missing.append({"component": "mcp.registration", "code": "MCP_REGISTRATION_MISMATCH", "message": "registered launcher does not match this runtime"})
            if not approval["valid"]:
                missing.append({"component": "mcp.approval", "code": approval["code"], "message": "target MCP default_tools_approval_mode is not approve"})
        except CLIError as exc:
            registration_error = {"code": exc.code, "message": str(exc)}
            missing.append({"component": "mcp.registration", "code": exc.code, "message": str(exc)})

    return {
        "schema_version": "research_workflow_status.v1",
        "ready": not missing,
        "paths": paths.bounded_view(),
        "registration": registration,
        "approval": approval,
        "skill": {
            "source": source,
            "user_scope": target,
            "source_validation": source_validation,
            "user_scope_validation": target_validation,
        },
        "registration_error": registration_error,
        "missing": missing,
    }


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--runtime-root", help="repository root; defaults to the parent of this script")
    parser.add_argument("--skill-source", help="skill directory; defaults to <runtime-root>/.agents/skills/research-workflow")
    parser.add_argument("--user-skill-root", help="user skill root; defaults to ~/.agents/skills")
    parser.add_argument("--codex-executable", help="Codex executable path or command name")
    parser.add_argument("--python-executable", help="Python executable used for MCP and validation")
    parser.add_argument("--quick-validate", help="skill-creator quick_validate.py path")
    parser.add_argument("--mcp-name", default=DEFAULT_MCP_NAME, help="target MCP registration name")
    parser.add_argument("--receipt", help="append bounded operation evidence to this local JSON path")
    parser.add_argument("--json", action="store_true", help="emit bounded JSON")


def _paths_from_args(args: argparse.Namespace) -> RuntimePaths:
    return discover_paths(
        runtime_root=args.runtime_root,
        skill_source=args.skill_source,
        user_skill_root=args.user_skill_root,
        codex_executable=args.codex_executable,
        python_executable=args.python_executable,
        quick_validate=args.quick_validate,
        mcp_name=args.mcp_name,
    )


def _initialize_product_runtime(
    workspace: str | os.PathLike[str],
    *,
    config_path: str | os.PathLike[str] | None = None,
) -> Mapping[str, Any]:
    """Call the product runtime initializer without loading installer paths.

    ``init`` is a product-workspace operation.  Keep its import lazy so the
    command does not discover the user skill, Codex executable, or MCP
    registration before the product runtime has diagnosed the requested
    workspace.  The small wrapper is also the test seam for the CLI boundary.
    """

    from src.product_workflow_runtime import initialize_product_runtime
    from src.runtime_composition import RuntimeCompositionError
    from src.workflow_runtime import WorkflowRuntimeError

    try:
        result = initialize_product_runtime(workspace, config_path=config_path)
    except (RuntimeCompositionError, WorkflowRuntimeError) as exc:
        details = getattr(exc, "details", None)
        if not isinstance(details, Mapping) or not details:
            bounded_view = getattr(exc, "bounded_view", None)
            if callable(bounded_view):
                try:
                    candidate = bounded_view()
                except Exception:  # noqa: BLE001 - diagnostic boundary
                    candidate = None
                if isinstance(candidate, Mapping):
                    details = candidate
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or not code.strip():
            code = "PRODUCT_RUNTIME_INIT_FAILED"
        raise CLIError(code, str(exc)[:MAX_DIAGNOSTIC_TEXT], details=details if isinstance(details, Mapping) else None) from exc
    if not isinstance(result, Mapping):
        raise CLIError("INIT_RESULT_INVALID", "product runtime initializer returned a non-object")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install and diagnose the local Workflow V2 Product Codex integration.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, aliases in (
        ("install", ["register"]),
        ("update", ["reregister"]),
        ("status", []),
        ("doctor", []),
    ):
        subparser = subparsers.add_parser(name, aliases=aliases)
        _common_arguments(subparser)
    init_parser = subparsers.add_parser(
        "init",
        help="initialize and diagnose the Product runtime for an explicit workspace",
    )
    init_parser.add_argument("--workspace", required=True, help="existing Product workspace directory")
    init_parser.add_argument("--runtime-config", help="existing runtime-composition config file")
    init_parser.add_argument("--receipt", help="append bounded operation evidence to this local JSON path")
    init_parser.add_argument("--json", action="store_true", help="emit bounded JSON")
    return parser


def run_command(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    if args.command == "init":
        initialized = _initialize_product_runtime(
            args.workspace,
            config_path=args.runtime_config,
        )
        result = copy.deepcopy(dict(initialized))
        result.setdefault("schema_version", "research_workflow_init.v1")
        result.setdefault("operation", "init")
        return (0 if result.get("ready") is not False else 1), result

    paths = _paths_from_args(args)
    if args.command in {"install", "register"}:
        skill = install_skill(paths)
        registration = ensure_registration(paths)
        result = {"schema_version": "research_workflow_install.v1", "operation": "install", "skill": skill, "registration": registration}
        result["status"] = inspect(paths)
        return 0, result
    if args.command in {"update", "reregister"}:
        skill = install_skill(paths)
        registration = ensure_registration(paths, force=True)
        result = {"schema_version": "research_workflow_update.v1", "operation": "update", "skill": skill, "registration": registration}
        result["status"] = inspect(paths)
        return 0, result
    result = inspect(paths)
    if args.command == "doctor" and not result["ready"]:
        return 1, result
    return 0, result


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code, result = run_command(args)
    except CLIError as exc:
        code, result = 2, exc.payload()
    if args.receipt:
        _append_receipt(
            Path(args.receipt).expanduser().resolve(),
            operation=args.command,
            exit_code=code,
            result=result,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
