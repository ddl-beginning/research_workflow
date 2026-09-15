#!/usr/bin/env python3
"""Canonical ``workflow`` command for portable V2.1 startup."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import product_doctor
from scripts import research_workflow_cli as installer
from src.execution_profile import ExecutionProfileError
from src.human_summary import build_human_presentation, render_human_presentation
from src.openai_codex_executor import OpenAICodexExecutor
from src.portable_startup import (
    PRODUCT_VERSION,
    MachinePaths,
    StartupError,
    bridge_source_identity,
    discover_bridge_root,
    ensure_machine_config,
    ensure_machine_directories,
    load_health,
    load_machine_config,
    machine_paths,
    persist_profile,
    probe_browser,
    provision_bridge,
    project_brief_summary,
    project_identity_present,
    record_workspace,
    reset_machine_config,
    secret_scan_paths,
)
from src.product_workflow_runtime import ProductWorkflowRuntime, initialize_product_runtime
from src.project_intake import IntakeMode, ProjectRequirementsIntake
from src.runtime_composition import RuntimeCompositionError, load_runtime_composition_config
from src.workflow_runtime import WorkflowRuntimeError


class CommandError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(message)


def _workspace(value: str | None) -> Path:
    candidate = Path(value or Path.cwd()).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CommandError("WORKSPACE_INVALID", "project directory does not exist") from exc
    if not resolved.is_dir():
        raise CommandError("WORKSPACE_INVALID", "project path is not a directory")
    return resolved


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return (Path(configured).expanduser() if configured else Path.home() / ".codex").resolve()


def _command_version(command: str) -> tuple[bool, str | None]:
    executable = shutil.which(command) or (command if Path(command).is_file() else None)
    if not executable:
        return False, None
    try:
        result = subprocess.run([executable, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return False, None
    value = next((line.strip() for line in (result.stdout + "\n" + result.stderr).splitlines() if line.strip()), None)
    return result.returncode == 0, value[:128] if value else None


def _runtime_config_path(paths: MachinePaths, args: argparse.Namespace) -> Path:
    return Path(args.runtime_config).expanduser().resolve() if args.runtime_config else paths.config


def _config_for_setup(
    paths: MachinePaths,
    args: argparse.Namespace,
    *,
    bridge_root: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    bridge_root = bridge_root or discover_bridge_root(
        args.bridge_root,
        product_root=ROOT,
        configured=None,
    )
    if args.runtime_config:
        selected = Path(args.runtime_config).expanduser().resolve()
        custom_paths = MachinePaths(selected.parent, selected, paths.runtime, paths.browser_profile, paths.workspace_registry, paths.health)
        return ensure_machine_config(
            custom_paths,
            bridge_root=bridge_root,
            browser_profile=Path(args.browser_profile).expanduser().resolve() if args.browser_profile else None,
            codex_executable=shutil.which("codex"),
            node_executable=args.node_executable,
            project_url=args.project_url,
            force=False,
        )
    return ensure_machine_config(
        paths,
        bridge_root=bridge_root,
        browser_profile=Path(args.browser_profile).expanduser().resolve() if args.browser_profile else None,
        codex_executable=shutil.which("codex"),
        node_executable=args.node_executable,
        project_url=args.project_url,
        force=False,
    )


def _check_codex(config: Mapping[str, Any] | None) -> dict[str, Any]:
    executor = config.get("executor") if isinstance(config, Mapping) and isinstance(config.get("executor"), Mapping) else {}
    configured = executor.get("codex_executable")
    executable = str(configured) if isinstance(configured, str) and configured else shutil.which("codex")
    if not executable:
        return {"status": "FAIL", "code": "CODEX_NOT_FOUND", "detail": "Codex CLI was not found"}
    try:
        provider = OpenAICodexExecutor(
            codex_executable=executable,
            preferred_model=str(executor.get("preferred_model", "gpt-5.6-luna")),
            fallback_model=executor.get("fallback_model"),
        )
    except Exception:
        return {"status": "FAIL", "code": "CODEX_RUNTIME_UNAVAILABLE", "detail": "Codex runtime could not be inspected"}
    runtime = provider.runtime
    if not runtime.authenticated or runtime.auth_mode != "chatgpt":
        return {"status": "FAIL", "code": "AUTH_REQUIRED", "detail": "Please sign in to Codex with ChatGPT"}
    model_ok = any(item.get("id") == "gpt-5.6-luna" for item in runtime.models)
    if not model_ok:
        return {"status": "FAIL", "code": "STANDARD_MODEL_UNAVAILABLE", "detail": "gpt-5.6-luna is unavailable"}
    return {"status": "PASS", "code": "OK", "detail": "Codex is installed, authenticated, and has gpt-5.6-luna"}


def _bridge_check(config: Mapping[str, Any] | None) -> dict[str, Any]:
    bridge = config.get("bridge") if isinstance(config, Mapping) and isinstance(config.get("bridge"), Mapping) else {}
    root = Path(str(bridge.get("root"))).expanduser() if bridge.get("root") else None
    profile = Path(str(bridge.get("profile_dir"))).expanduser() if bridge.get("profile_dir") else None
    node = str(bridge.get("node_executable", "node"))
    if not bridge.get("enabled"):
        return {"status": "FAIL", "code": "BRIDGE_CONFIGURATION_REQUIRED", "detail": "Browser Bridge is not configured"}
    if root is None or not (root / "scripts" / "consult-pack.mjs").is_file():
        return {"status": "FAIL", "code": "BRIDGE_SCRIPT_MISSING", "detail": "Browser Bridge checkout is missing consult-pack.mjs"}
    if (root / "package.json").is_file() and not (root / "node_modules" / "playwright").is_dir():
        return {"status": "FAIL", "code": "BRIDGE_DEPENDENCIES_MISSING", "detail": "Browser Bridge npm dependencies are not installed"}
    if not profile or not profile.is_dir():
        return {"status": "FAIL", "code": "BROWSER_PROFILE_REQUIRED", "detail": "dedicated browser profile is missing"}
    if not (shutil.which(node) or Path(node).is_file()):
        return {"status": "FAIL", "code": "NODE_NOT_FOUND", "detail": "Node.js was not found"}
    expected_version = bridge.get("version")
    expected_digest = bridge.get("source_digest")
    if expected_version or expected_digest:
        try:
            actual = bridge_source_identity(root)
        except StartupError:
            return {"status": "FAIL", "code": "BRIDGE_IDENTITY_INVALID", "detail": "Browser Bridge identity could not be verified"}
        if expected_version != actual.get("version") or expected_digest != actual.get("source_digest"):
            return {"status": "FAIL", "code": "BRIDGE_VERSION_MISMATCH", "detail": "Browser Bridge source identity does not match setup"}
        return {"status": "PASS", "code": "OK", "detail": f"Browser Bridge {actual['version']} is provisioned and compatible", "version": actual["version"], "source_digest": actual["source_digest"]}
    return {"status": "PASS", "code": "OK", "detail": "Browser Bridge and dedicated profile are configured"}


def _machine_config_for(paths: MachinePaths, args: argparse.Namespace) -> tuple[MachinePaths, dict[str, Any] | None]:
    selected = _runtime_config_path(paths, args)
    if selected == paths.config:
        return paths, load_machine_config(paths)
    custom = MachinePaths(selected.parent, selected, paths.runtime, paths.browser_profile, paths.workspace_registry, paths.health)
    return custom, load_machine_config(custom)


def _doctor_report(workspace: Path, paths: MachinePaths, config_path: Path, *, probe: bool = False) -> dict[str, Any]:
    try:
        product = product_doctor.run_doctor(workspace, config_path)
    except (OSError, RuntimeError, ValueError) as exc:
        product = {
            "ready": False,
            "checks": [],
            "error": {"code": type(exc).__name__, "message": str(exc)[:256]},
            "writes_performed": False,
            "provider_dispatch_performed": False,
            "gpt_calls_performed": False,
        }
    config = load_machine_config(paths)
    browser_check = _bridge_check(config)
    if probe and config is not None:
        browser_probe = probe_browser(config, paths)
    else:
        health = load_health(paths)
        if health and health.get("status") == "PASS":
            browser_probe = {"status": "PASS", "code": "OK", "detail": "last bounded browser health probe passed"}
        elif health and health.get("code") == "GPT_AUTH_REQUIRED":
            browser_probe = {"status": "FAIL", "code": "GPT_AUTH_REQUIRED", "detail": "sign in to ChatGPT in the dedicated browser profile"}
        elif health and health.get("status") == "FAIL":
            browser_probe = {"status": "FAIL", "code": str(health.get("code") or "GPT_BROWSER_PROBE_FAILED"), "detail": "run workflow doctor --probe-browser for a fresh browser check"}
        else:
            browser_probe = {"status": "FAIL", "code": "GPT_BROWSER_NOT_CHECKED", "detail": "run workflow setup or workflow doctor --probe-browser"}
    if hasattr(browser_probe, "bounded_view"):
        browser_probe = browser_probe.bounded_view()
    summary = project_brief_summary(workspace) if project_identity_present(workspace) else None
    profile_ok = bool(summary and isinstance(summary.get("profile"), Mapping) and summary["profile"].get("name") == "autonomous_research")
    journal_path = workspace / ".workflow-v2" / "journal.json"
    project_status = "READY" if summary and profile_ok and journal_path.is_file() else "INITIALIZE REQUIRED" if summary else "NOT INITIALIZED"
    check_by_name = {str(item.get("name")): item for item in product.get("checks", []) if isinstance(item, Mapping)}

    def state(name: str, fallback: bool = False) -> str:
        return "OK" if check_by_name.get(name, {}).get("status") == "PASS" else "FAIL"

    report = {
        "schema_version": "workflow_portable_doctor.v1",
        "workflow_version": PRODUCT_VERSION,
        "workspace": workspace.as_posix(),
        "config_path": config_path.as_posix(),
        "ready": all(
            item == "OK"
            for item in (
                state("product.source"),
                state("python"),
                state("codex.runtime"),
                state("chatgpt.auth"),
                browser_check["status"] == "PASS" and "OK" or "FAIL",
                browser_probe["status"] == "PASS" and "OK" or "FAIL",
                state("mcp.product_entry"),
                state("contract.stage_actions"),
                state("runtime.config"),
                "OK" if project_status == "READY" else "FAIL",
            )
        ),
        "checks": {
            "Workflow Engine": {"status": "OK" if state("product.source") == "OK" else "FAIL", "detail": "installed Product files"},
            "Workflow Version": {"status": "OK", "detail": f"v{PRODUCT_VERSION}"},
            "Codex": {"status": "OK" if state("codex.runtime") == "OK" else "FAIL", "detail": "Codex CLI"},
            "Codex Auth": {"status": "OK" if state("chatgpt.auth") == "OK" else "LOGIN REQUIRED", "detail": "Sign in to Codex with ChatGPT." if state("chatgpt.auth") != "OK" else "ChatGPT auth is available."},
            "GPT Browser": {"status": "OK" if browser_probe["status"] == "PASS" else "LOGIN REQUIRED" if browser_probe["code"] == "GPT_AUTH_REQUIRED" else "CHECK SETUP", "detail": browser_probe["detail"]},
            "Browser Bridge": {"status": "OK" if browser_check["status"] == "PASS" else "FAIL", "detail": browser_check["detail"]},
            "MCP": {"status": "OK" if state("mcp.product_entry") == "OK" else "FAIL", "detail": "research-supervisor registration"},
            "Stage Contract": {"status": "OK" if state("contract.stage_actions") == "OK" else "FAIL", "detail": "canonical Stage propagation handshake"},
            "Machine Runtime": {"status": "OK" if state("runtime.config") == "OK" else "FAIL", "detail": paths.root.as_posix()},
            "Project": {"status": "OK" if summary else "NOT INITIALIZED", "detail": summary.get("project_name") if summary else workspace.name},
            "Project Workflow": {"status": project_status, "detail": "autonomous_research profile + V2 journal" if project_status == "READY" else "run workflow init"},
        },
        "profile": summary.get("profile") if summary else None,
        "product": product,
        "browser_probe": browser_probe,
        "writes_performed": bool(probe),
        "cookie_export": False,
    }
    return report


def _setup(args: argparse.Namespace, workspace: Path) -> dict[str, Any]:
    paths = machine_paths(args.machine_root)
    if args.reset_machine_config:
        reset_machine_config(paths)
    ensure_machine_directories(paths)
    packaged_bridge = discover_bridge_root(
        args.bridge_root,
        product_root=ROOT,
        configured=None,
    )
    if packaged_bridge is None:
        raise CommandError(
            "BRIDGE_SOURCE_NOT_FOUND",
            "versioned Browser Bridge source was not found in this Workflow checkout",
        )
    try:
        provisioned_bridge, bridge_identity, bridge_changed = provision_bridge(
            paths,
            packaged_bridge,
            node_executable=args.node_executable,
        )
    except StartupError as exc:
        raise CommandError(exc.code, str(exc), details=exc.details) from exc
    config, config_changed = _config_for_setup(paths, args, bridge_root=provisioned_bridge)
    config_paths, selected_config = _machine_config_for(paths, args)
    if selected_config is None:
        selected_config = config
    steps: list[dict[str, Any]] = []

    def step(name: str, status: str, code: str, detail: str) -> None:
        steps.append({"name": name, "status": status, "code": code, "detail": detail})

    step("installation", "PASS" if (ROOT / "pyproject.toml").is_file() else "FAIL", "OK" if (ROOT / "pyproject.toml").is_file() else "PRODUCT_NOT_FOUND", "Workflow Engine checkout discovered")
    py_ok = sys.version_info >= (3, 11)
    step("python", "PASS" if py_ok else "FAIL", "OK" if py_ok else "PYTHON_VERSION_UNSUPPORTED", sys.version.split()[0])
    node_ok, node_version = _command_version(str(selected_config.get("bridge", {}).get("node_executable", args.node_executable)) if isinstance(selected_config.get("bridge"), Mapping) else args.node_executable)
    step("node", "PASS" if node_ok else "FAIL", "OK" if node_ok else "NODE_NOT_FOUND", node_version or "Node.js is required by Browser Bridge")
    codex = _check_codex(selected_config)
    step("codex_auth", codex["status"], codex["code"], codex["detail"])
    step("machine_config", "PASS", "OK", f"{'created' if config_changed else 'reused'} {config_paths.config.as_posix()}")
    bridge = _bridge_check(selected_config)
    step("browser_bridge", bridge["status"], bridge["code"], bridge["detail"])
    step(
        "bridge_provision",
        "PASS" if bridge_changed else "PASS",
        "OK",
        f"Browser Bridge {bridge_identity['version']} provisioned at {provisioned_bridge.as_posix()}",
    )
    profile_dir = selected_config.get("bridge", {}).get("profile_dir") if isinstance(selected_config.get("bridge"), Mapping) else None
    if isinstance(profile_dir, str):
        Path(profile_dir).expanduser().mkdir(parents=True, exist_ok=True)
    step("browser_profile", "PASS" if isinstance(profile_dir, str) else "FAIL", "OK" if isinstance(profile_dir, str) else "BROWSER_PROFILE_REQUIRED", "dedicated machine-local browser profile is ready" if isinstance(profile_dir, str) else "profile path is missing")
    browser = probe_browser(selected_config, config_paths, timeout_seconds=args.browser_timeout) if not args.skip_browser_probe else None
    if browser is None:
        step("gpt_browser", "SKIPPED", "GPT_BROWSER_NOT_CHECKED", "browser probe was explicitly skipped")
    else:
        step("gpt_browser", browser.status, browser.code, browser.detail)

    skill_source = ROOT / "skills" / "workflow-launcher"
    user_skill_root = _codex_home() / "skills"
    try:
        installer_paths = installer.discover_paths(
            runtime_root=ROOT,
            skill_source=skill_source,
            user_skill_root=user_skill_root,
            codex_executable=selected_config.get("executor", {}).get("codex_executable") if isinstance(selected_config.get("executor"), Mapping) else None,
            mcp_name="research-supervisor",
            skill_name="workflow",
            runtime_config=config_paths.config,
        )
        installer.install_skill(installer_paths)
        installer.ensure_registration(installer_paths)
        step("mcp", "PASS", "OK", "research-supervisor and the workflow launcher are registered")
    except Exception as exc:
        step("mcp", "FAIL", getattr(exc, "code", "MCP_SETUP_FAILED"), "MCP registration could not be completed")

    scan = secret_scan_paths([config_paths.config, config_paths.workspace_registry, config_paths.health])
    step("secret_scan", "PASS" if scan["passed"] else "FAIL", "OK" if scan["passed"] else "SECRET_MATERIAL_DETECTED", "machine artifacts contain no credential material" if scan["passed"] else "forbidden material was detected")
    doctor = _doctor_report(workspace, config_paths, config_paths.config, probe=False)
    machine_ready = all(item["status"] in {"PASS", "SKIPPED"} for item in steps if item["name"] not in {"secret_scan"}) and scan["passed"]
    return {
        "schema_version": "workflow_setup.v1",
        "operation": "setup",
        "ready": machine_ready,
        "machine_ready": machine_ready,
        "config_changed": config_changed,
        "machine_paths": config_paths.bounded_view(),
        "steps": steps,
        "doctor": doctor,
        "cookie_export": False,
    }


def _init(args: argparse.Namespace, workspace: Path) -> dict[str, Any]:
    paths = machine_paths(args.machine_root)
    config_path = _runtime_config_path(paths, args)
    if not config_path.is_file():
        raise CommandError("SETUP_REQUIRED", "run workflow setup before workflow init")
    preflight = product_doctor.run_doctor(workspace, config_path)
    machine_failures = [item for item in preflight.get("checks", []) if item.get("name") not in {"project.workspace"} and item.get("status") != "PASS"]
    if machine_failures:
        raise CommandError("SETUP_REQUIRED", "machine setup is not ready; run workflow setup", details={"doctor": preflight})
    if not project_identity_present(workspace):
        if not args.goal and not args.brief:
            return {
                "schema_version": "workflow_init.v1",
                "operation": "init",
                "ready": False,
                "code": "INIT_INPUT_REQUIRED",
                "message": "No project identity exists. Run workflow init --goal \"...\" or provide --brief.",
                "writes_performed": False,
            }
        brief: dict[str, Any] = {"goal": args.goal} if args.goal else {}
        if args.brief:
            try:
                loaded = json.loads(Path(args.brief).read_text(encoding="utf-8")) if Path(args.brief).is_file() else json.loads(args.brief)
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise CommandError("INIT_INPUT_INVALID", "--brief must be JSON or a JSON file") from exc
            if not isinstance(loaded, Mapping):
                raise CommandError("INIT_INPUT_INVALID", "--brief must contain a JSON object")
            brief.update(dict(loaded))
        intake = ProjectRequirementsIntake(workspace)
        intake.initialize(mode=IntakeMode.USER_CONFIRMED_BRIEF, brief=brief, entrypoint="workflow-init")
    profile = persist_profile(workspace)
    initialized = initialize_product_runtime(workspace, config_path=config_path)
    summary = project_brief_summary(workspace) or {}
    registry = record_workspace(paths, workspace, str(summary.get("project_id")))
    result = {"schema_version": "workflow_init.v1", "operation": "init", "ready": True, "profile": profile, "registry": registry, **initialized}
    presentation = build_human_presentation(canonical_state=result.get("canonical"), artifact_root=workspace)
    return {"human_summary": presentation["human_summary"], "machine_details": presentation["machine_details"], "presentation": presentation, **result}


def _resume(args: argparse.Namespace, workspace: Path) -> dict[str, Any]:
    if not project_identity_present(workspace):
        return {
            "schema_version": "workflow_resume.v1",
            "operation": "resume",
            "ready": False,
            "code": "WORKFLOW_NOT_FOUND",
            "message": "No Workflow Project is bound to this directory. Run workflow init --goal \"...\".",
            "writes_performed": False,
        }
    paths = machine_paths(args.machine_root)
    config_path = _runtime_config_path(paths, args)
    if not config_path.is_file():
        raise CommandError("SETUP_REQUIRED", "run workflow setup before resuming this project")
    profile = persist_profile(workspace)
    initialized = initialize_product_runtime(workspace, config_path=config_path)
    config = load_runtime_composition_config(workspace, config_path=config_path)
    resumed = ProductWorkflowRuntime(workspace, config=config).resume()
    summary = project_brief_summary(workspace) or {}
    registry = record_workspace(paths, workspace, str(summary.get("project_id")))
    maintenance_keys = (
        "blocker_classification", "resume_blocker_revalidation", "technical_recovery",
        "technical_gpt_escalation", "gpt_decision_applied", "provider_execution",
        "stage_owned_output_missing", "generation_started", "blocked_validation",
        "human_intervention_count", "human_action",
    )
    result = {
        "schema_version": "workflow_resume.v1",
        "operation": "resume",
        "ready": True,
        "profile": profile,
        "registry": registry,
        **initialized,
        "canonical": resumed.get("canonical", initialized.get("canonical")),
        **{key: resumed[key] for key in ("legacy_resolution", "old_operation_preserved", "old_operation_redispatched", "side_effect_audit", "self_repair", *maintenance_keys) if key in resumed},
    }
    presentation = build_human_presentation(metadata=result, canonical_state=result.get("canonical"), artifact_root=workspace)
    return {"human_summary": presentation["human_summary"], "machine_details": presentation["machine_details"], "presentation": presentation, **result}


def _status(args: argparse.Namespace, workspace: Path) -> dict[str, Any]:
    summary = project_brief_summary(workspace) if project_identity_present(workspace) else None
    return {
        "schema_version": "workflow_status.v1",
        "operation": "status",
        "workspace": workspace.as_posix(),
        "project": summary,
        "journal": (workspace / ".workflow-v2" / "journal.json").is_file(),
        "machine_config": _runtime_config_path(machine_paths(args.machine_root), args).is_file(),
        "cookie_export": False,
    }


def _human_doctor(report: Mapping[str, Any]) -> str:
    lines = ["Workflow V2.1", ""]
    for name, item in report.get("checks", {}).items():
        status = str(item.get("status", "FAIL"))
        lines.append(f"{name:<22} {status}")
    if not report.get("ready"):
        lines.extend(["", "Action:", "Run workflow setup"])
        browser_status = report.get("checks", {}).get("GPT Browser", {}).get("status")
        if browser_status == "LOGIN REQUIRED":
            lines.append("Sign in to ChatGPT in the dedicated browser profile, then rerun workflow.exe doctor --probe-browser.")
        elif browser_status == "CHECK SETUP":
            lines.append("Run workflow.exe doctor --probe-browser for a fresh GPT browser check.")
        if report.get("checks", {}).get("Project Workflow", {}).get("status") != "READY":
            lines.append("For a new project, run workflow init --goal \"...\".")
    return "\n".join(lines)


def _human_setup(result: Mapping[str, Any]) -> str:
    lines = ["Workflow setup", ""]
    for item in result.get("steps", []):
        lines.append(f"{str(item.get('name', 'step')):<22} {item.get('status')}" + (f"  {item.get('detail')}" if item.get("status") != "PASS" else ""))
    lines.extend(["", "WORKFLOW_SETUP:", "READY" if result.get("ready") else "ACTION_REQUIRED"])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Portable Workflow V2.1 startup and first-run setup")
    parser.add_argument("command", nargs="?", choices=("setup", "doctor", "init", "resume", "status"), help="operation; omitted means resume or init guidance")
    parser.add_argument("--project", "--workspace", dest="workspace", help="project directory; defaults to the current directory")
    parser.add_argument("--machine-root", help="override %%LOCALAPPDATA%%/ResearchWorkflow")
    parser.add_argument("--runtime-config", help="override the machine runtime config path")
    parser.add_argument("--bridge-root", help="Browser Bridge checkout")
    parser.add_argument("--browser-profile", help="dedicated browser profile path")
    parser.add_argument("--project-url", help="optional non-secret ChatGPT Project URL")
    parser.add_argument("--node-executable", default="node")
    parser.add_argument("--browser-timeout", type=int, default=180)
    parser.add_argument("--skip-browser-probe", action="store_true")
    parser.add_argument("--probe-browser", action="store_true")
    parser.add_argument("--reset-machine-config", action="store_true")
    parser.add_argument("--goal", help="initial project goal for workflow init")
    parser.add_argument("--brief", help="JSON object or JSON file for workflow init")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true", help="emit bounded JSON")
    return parser


def run_command(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    workspace = _workspace(args.workspace)
    command = args.command
    if command is None:
        command = "resume" if project_identity_present(workspace) else "init"
    if command == "setup":
        result = _setup(args, workspace)
        return (0 if result["ready"] else 1), result
    if command == "doctor":
        paths = machine_paths(args.machine_root)
        config_path = _runtime_config_path(paths, args)
        report = _doctor_report(workspace, paths, config_path, probe=args.probe_browser)
        return (0 if report["ready"] else 1), report
    if command == "init":
        result = _init(args, workspace)
        return (0 if result.get("ready") else 1), result
    if command == "resume":
        result = _resume(args, workspace)
        return (0 if result.get("ready") else 1), result
    result = _status(args, workspace)
    return 0, result


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code, result = run_command(args)
    except (CommandError, StartupError, RuntimeCompositionError, WorkflowRuntimeError, ExecutionProfileError, OSError, ValueError) as exc:
        code = 2
        result = {"schema_version": "workflow_error.v1", "ready": False, "error": {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)[:512]}}
        if getattr(exc, "details", None):
            result["details"] = getattr(exc, "details")
    if args.json or args.verbose:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == "setup":
        print(_human_setup(result))
    elif args.command == "doctor":
        print(_human_doctor(result))
    elif isinstance(result.get("presentation"), Mapping):
        print(render_human_presentation(result["presentation"]))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
