#!/usr/bin/env python3
"""Read-only Product V2 preflight for a project workspace.

The doctor inspects local dependencies and configuration only.  It never
creates a Stage, dispatches a Provider, invokes GPT, registers MCP, or writes
workflow state.  Credentials are represented only by presence/absence.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.openai_codex_executor import OpenAICodexExecutor
from src.product_workflow_runtime import doctor_product_runtime
from src.runtime_composition import RuntimeCompositionConfig, RuntimeCompositionError, load_runtime_composition_config


PRODUCT_VERSION = "2.1.0"
MCP_NAME = "research-supervisor"
CONFIG_ENV = "RESEARCH_WORKFLOW_RUNTIME_CONFIG"


def _check(name: str, ok: bool, *, detail: str | None = None, code: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"name": name, "status": "PASS" if ok else "FAIL"}
    if code:
        result["code"] = code
    if detail:
        result["detail"] = detail[:512]
    return result


def _command_version(command: str) -> tuple[bool, str | None]:
    executable = shutil.which(command) or (command if Path(command).is_file() else None)
    if not executable:
        return False, None
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, None
    version = next((line.strip() for line in (result.stdout + "\n" + result.stderr).splitlines() if line.strip()), None)
    return result.returncode == 0, version[:128] if version else None


def _registration_check(config: RuntimeCompositionConfig | None) -> dict[str, Any]:
    codex = shutil.which(config.codex_executable if config and config.codex_executable else "codex")
    if not codex:
        return _check("mcp.product_entry", False, code="CODEX_NOT_FOUND", detail="Codex CLI was not found")
    try:
        result = subprocess.run(
            [codex, "mcp", "list", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        payload = None
    entries = payload if isinstance(payload, list) else payload.get("servers", []) if isinstance(payload, Mapping) else []
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("name") != MCP_NAME:
            continue
        transport = entry.get("transport") if isinstance(entry.get("transport"), Mapping) else entry
        command = str(transport.get("command", ""))
        args = transport.get("args", [])
        env = transport.get("env", {})
        launcher = ROOT / "scripts" / "workflow_mcp.py"
        command_ok = Path(command).resolve() == Path(sys.executable).resolve() or Path(command).name.casefold() in {"python", "python.exe"}
        launcher_ok = isinstance(args, list) and any(Path(str(item)).resolve() == launcher.resolve() for item in args)
        env_ok = isinstance(env, Mapping) and CONFIG_ENV in env
        return _check(
            "mcp.product_entry",
            command_ok and launcher_ok and env_ok,
            code="MCP_REGISTRATION_MISMATCH" if not (command_ok and launcher_ok and env_ok) else None,
            detail="registered Product launcher and machine-local runtime config are present",
        )
    return _check("mcp.product_entry", False, code="MCP_NOT_REGISTERED", detail=f"MCP server {MCP_NAME!r} is not registered")


def run_doctor(workspace: str | os.PathLike[str], config_path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    project = Path(workspace).expanduser().resolve()
    checks: list[dict[str, Any]] = []
    checks.append(_check("product.version", (ROOT / "pyproject.toml").is_file(), detail=PRODUCT_VERSION))
    checks.append(_check("product.source", (ROOT / "src" / "workflow_mcp.py").is_file() and (ROOT / "schemas" / "workflow_v2" / "stage.schema.json").is_file(), code="PRODUCT_FILES_MISSING"))
    checks.append(_check("project.workspace", project.is_dir(), code="WORKSPACE_INVALID"))
    checks.append(_check("python", sys.version_info >= (3, 11), detail=sys.version.split()[0], code="PYTHON_VERSION_UNSUPPORTED"))

    node_command = "node"
    config: RuntimeCompositionConfig | None = None
    config_error: RuntimeCompositionError | None = None
    if project.is_dir():
        try:
            config = load_runtime_composition_config(project, config_path=config_path)
            node_command = config.node_executable
        except RuntimeCompositionError as exc:
            config_error = exc
    checks.append(_check("runtime.config", config is not None and config_error is None and config.config_path is not None and config.config_path.is_file(), code=config_error.code if config_error else "RUNTIME_CONFIG_REQUIRED"))
    node_ok, node_version = _command_version(node_command)
    checks.append(_check("node", node_ok, detail=node_version, code="NODE_NOT_FOUND"))

    runtime: dict[str, Any] | None = None
    if config is not None and config_error is None and project.is_dir():
        runtime = doctor_product_runtime(project, config=config)
        checks.append(_check("lifecycle.v2", runtime.get("lifecycle_version") == "v2", code="EXPLICIT_V2_CONFIG_REQUIRED"))
        checks.append(_check("codex.runtime", bool(runtime.get("provider", {}).get("available")), code="CODEX_RUNTIME_UNAVAILABLE"))
        checks.append(_check("chatgpt.auth", runtime.get("provider", {}).get("auth_mode") == "chatgpt" and bool(runtime.get("provider", {}).get("authenticated")), code="AUTHENTICATION_REQUIRED"))
        checks.append(_check("standard.model", bool(runtime.get("provider", {}).get("standard_model_available")), code="STANDARD_MODEL_UNAVAILABLE"))
        checks.append(_check("browser.bridge", not any(item.get("component") in {"bridge.root", "bridge.node", "bridge.profile", "bridge.scope"} for item in runtime.get("missing", [])), code="BRIDGE_CONFIGURATION_REQUIRED"))
        checks.append(_check("machine.runtime_root", True, detail="default: %LOCALAPPDATA%/ResearchWorkflow; TEMP artifacts are machine-local"))
    else:
        for name, code in (("lifecycle.v2", "EXPLICIT_V2_CONFIG_REQUIRED"), ("codex.runtime", "CODEX_RUNTIME_UNAVAILABLE"), ("chatgpt.auth", "AUTHENTICATION_REQUIRED"), ("standard.model", "STANDARD_MODEL_UNAVAILABLE"), ("browser.bridge", "BRIDGE_CONFIGURATION_REQUIRED"), ("machine.runtime_root", "RUNTIME_CONFIG_REQUIRED")):
            checks.append(_check(name, False, code=code))
    checks.append(_registration_check(config))
    ready = all(item["status"] == "PASS" for item in checks)
    return {
        "schema_version": "workflow_v2_product_doctor.v1",
        "product_version": PRODUCT_VERSION,
        "workspace": project.as_posix(),
        "config_path": config.config_path.as_posix() if config and config.config_path else None,
        "ready": ready,
        "checks": checks,
        "runtime": runtime,
        "writes_performed": False,
        "provider_dispatch_performed": False,
        "gpt_calls_performed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Workflow V2 Product doctor")
    parser.add_argument("--workspace", required=True, help="project workspace to diagnose")
    parser.add_argument("--runtime-config", help="machine-local runtime composition JSON")
    parser.add_argument("--json", action="store_true", help="emit JSON (the default)")
    args = parser.parse_args(argv)
    try:
        result = run_doctor(args.workspace, args.runtime_config)
    except (OSError, RuntimeError, ValueError) as exc:
        result = {"schema_version": "workflow_v2_product_doctor.v1", "ready": False, "error": {"code": type(exc).__name__, "message": str(exc)[:512]}, "writes_performed": False, "provider_dispatch_performed": False, "gpt_calls_performed": False}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result.get("ready") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

