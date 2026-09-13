#!/usr/bin/env python3
"""Clean-room installation and real-provider/GPT release validation.

The validator runs against a fresh clone of a tagged Product checkout and a
fresh disposable project.  It exercises the documented install surface,
Product MCP boundary, real ChatGPT-backed GPT planning/review, the real
STANDARD Codex provider, settled integration, process-restart resume, and a
relocated-engine smoke run.  The validator never imports the source tree that
it is validating for runtime calls: provider and MCP calls use the editable
installation in the clean checkout's isolated virtual environment.

All output is bounded metadata.  Prompts, raw GPT responses, cookies, and
tokens are not copied into the validation report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping


PRODUCT_ROOT = Path(__file__).resolve().parents[1]
MCP_NAME = "research-supervisor"
CONFIG_ENV = "RESEARCH_WORKFLOW_RUNTIME_CONFIG"
SOURCE_PATH = "src/add.py"
TEST_PATH = "tests/test_add.py"
TEST_COMMAND = "python -m pytest -q tests/test_add.py"
STAGE_ID = "workflow-v2-release-validation-v1"
OBJECTIVE = (
    "Fix the bounded add(a, b) fixture so it returns the sum instead of the difference; "
    "run the exact required test command and leave a non-empty diff in src/add.py only."
)
PROTECTED_PATHS = [".git", ".workflow-v2", ".research", "tests", "release", "specs", "AGENTS.override.md"]


class ValidationFailure(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def json_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def run(command: list[str | os.PathLike[str]], *, cwd: Path, check: bool = True, env: Mapping[str, str] | None = None, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(item) for item in command],
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env=dict(env) if env is not None else None,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise ValidationFailure(f"command failed with exit {result.returncode}: {Path(str(command[0])).name}")
    return result


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(root), *args], cwd=root, check=check, timeout=90)


def bounded_status(root: Path) -> list[str]:
    output = git(root, "status", "--short", "--untracked-files=all").stdout
    values: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        value = line[3:].strip().replace("\\", "/")
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        if value:
            values.append(value)
    return values


def project_workspace_id(project: Path) -> str:
    return "workspace-" + hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:24]


def stage_from_view(value: Mapping[str, Any]) -> Mapping[str, Any]:
    stage = value.get("stage")
    if not isinstance(stage, Mapping) or stage.get("status") is None:
        command_result = value.get("command_result")
        if isinstance(command_result, Mapping) and isinstance(command_result.get("stage"), Mapping):
            stage = command_result.get("stage")
    if not isinstance(stage, Mapping):
        canonical = value.get("canonical")
        stage = canonical.get("stage") if isinstance(canonical, Mapping) else None
    if not isinstance(stage, Mapping):
        raise ValidationFailure("Product response canonical projection has no stage")
    return stage


def revision_from_view(value: Mapping[str, Any]) -> Any:
    if "revision" in value:
        return value.get("revision")
    canonical = value.get("canonical")
    return canonical.get("revision") if isinstance(canonical, Mapping) else None


class MCPClient:
    """One fresh stdio server process per call, with metadata-only tracing."""

    def __init__(self, *, python: Path, launcher: Path, config: Path, workspace: Path, trace: Path) -> None:
        self.python = python
        self.launcher = launcher
        self.config = config
        self.workspace = workspace
        self.trace = trace
        self.calls: list[dict[str, Any]] = []
        self._next_id = 10

    def call(self, name: str, arguments: Mapping[str, Any], *, allow_error: bool = False) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        env = dict(os.environ)
        env[CONFIG_ENV] = str(self.config)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["RESEARCH_WORKFLOW_MCP_TRACE_PATH"] = str(self.trace)
        process = subprocess.Popen(
            [str(self.python), str(self.launcher)],
            cwd=str(self.workspace),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "release-validator", "version": "1"}},
        }
        notification = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        request = {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": dict(arguments)}}
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps(initialize, ensure_ascii=False) + "\n")
            process.stdin.write(json.dumps(notification, ensure_ascii=False) + "\n")
            process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            process.stdin.flush()
            response: Mapping[str, Any] | None = None
            assert process.stdout is not None
            deadline = time.monotonic() + 660.0
            while time.monotonic() < deadline:
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, Mapping) and candidate.get("id") == request_id:
                    response = candidate
                    break
            if response is None:
                raise ValidationFailure(f"MCP {name} returned no response")
        finally:
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            raise ValidationFailure(f"MCP {name} did not exit after response")
        result = response.get("result") if isinstance(response, Mapping) else None
        if not isinstance(result, Mapping):
            raise ValidationFailure(f"MCP {name} returned an invalid JSON-RPC result")
        structured = result.get("structuredContent")
        payload = dict(structured) if isinstance(structured, Mapping) else {}
        error = payload.get("error") if isinstance(payload.get("error"), Mapping) else None
        is_error = bool(result.get("isError", False)) or error is not None
        self.calls.append({
            "tool": name,
            "request_digest": sha256_json({"tool": name, "arguments": dict(arguments)}),
            "is_error": is_error,
            "error_code": error.get("code") if error else None,
            "process_returncode": process.returncode,
        })
        if is_error and not allow_error:
            raise ValidationFailure(f"MCP {name} failed: {error.get('code') if error else 'UNKNOWN'}")
        return payload


def temporary_mcp_registration(codex: str, *, python: Path, launcher: Path, config: Path, cwd: Path) -> None:
    run([codex, "mcp", "remove", MCP_NAME], cwd=cwd, check=False, timeout=30)
    result = run(
        [codex, "mcp", "add", MCP_NAME, "--env", f"{CONFIG_ENV}={config}", "--", str(python), str(launcher)],
        cwd=cwd,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise ValidationFailure("could not register the temporary clean-checkout MCP launcher")


def fixture_project(root: Path, *, label: str) -> None:
    if root.exists() and any(root.iterdir()):
        raise ValidationFailure(f"refusing to reuse non-empty validation project: {root}")
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "release").mkdir()
    (root / "specs" / STAGE_ID).mkdir(parents=True)
    (root / ".gitignore").write_text(
        "__pycache__/\n.pytest_cache/\n.research/\n.consultations/\n.workflow-v2/\n.tmp/\n",
        encoding="utf-8",
    )
    (root / SOURCE_PATH).write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / TEST_PATH).write_text(
        "from src.add import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (root / "AGENTS.override.md").write_text(
        "# Workflow V2 release fixture\n\n"
        "This is a bounded non-destructive coding task. Modify only `src/add.py`;\n"
        "do not touch tests, release, specs, `.research`, `.workflow-v2`, or this\n"
        "file. Run exactly `python -m pytest -q tests/test_add.py`.\n",
        encoding="utf-8",
    )
    write_json(
        root / "release" / "requirement.json",
        {
            "requirement_id": f"{label}-requirement",
            "goal": OBJECTIVE,
            "acceptance": ["the real STANDARD provider succeeds", "the exact required test passes", "only src/add.py changes before integration", "the V2 Stage closes after settled integration"],
            "human_decision": "ACCEPT_STAGE",
            "non_destructive": True,
        },
    )
    for name, text in {
        "spec.md": "# Release validation specification\n\nOne bounded add implementation fix is required.\n",
        "plan.md": "# Release validation plan\n\nUse one real planning review, one provider attempt, one technical review, then integrate.\n",
        "tasks.md": "# Release validation tasks\n\n- [ ] Run the exact test command.\n- [ ] Apply only the reviewed source change.\n- [ ] Close the V2 Stage with a settled receipt.\n",
    }.items():
        (root / "specs" / STAGE_ID / name).write_text(text, encoding="utf-8")
    git(root, "init", "-q")
    git(root, "config", "user.email", "workflow-v2-release-validation@example.invalid")
    git(root, "config", "user.name", "Workflow V2 Release Validation")
    git(root, "add", ".")
    git(root, "commit", "-qm", f"{label} clean baseline")


def stage_contract(*, workspace_id: str, project_id: str, baseline: str) -> dict[str, Any]:
    return {
        "schema_version": "stage.v2",
        "stage_id": STAGE_ID,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "objective_fingerprint": sha256_json({"objective": OBJECTIVE, "target": SOURCE_PATH}),
        "target_identity": SOURCE_PATH,
        "required_capabilities": ["coding", "pytest", "native_codex", "chatgpt_auth", "gpt_review"],
        "purpose": "DOMAIN",
        "repair_depth": 0,
        "baseline_digest": baseline,
        "status": "PLANNED",
        "budgets": {
            "max_iterations": 2,
            "max_attempts_per_iteration": 2,
            "max_attempts_total": 3,
            "max_revalidation_ops": 1,
            "max_validator_revisions": 1,
            "max_dependency_nodes": 2,
            "max_dependency_depth": 1,
            "max_descendant_attempts": 2,
        },
        "owner_stage_id": None,
        "current_iteration_id": None,
        "current_assessment_id": None,
        "predecessor_stage_id": None,
        "semantic_baseline_diff_digest": None,
    }


def context_pack(*, stage_goal: str, latest: Mapping[str, Any], evidence: list[dict[str, Any]], commit: str, dirty: bool) -> dict[str, Any]:
    return {
        "mode": "fresh",
        "projectGoal": OBJECTIVE,
        "currentStageGoal": stage_goal,
        "userVisibleGoal": "A reviewable passing fixture result and a closed Workflow V2 Stage.",
        "establishedFacts": ["this is a fresh disposable Git repository", "no historical .research state or Parent was imported", "the task is non-destructive and limited to src/add.py"],
        "currentMethod": "one bounded real provider execution followed by deterministic V2 assessment",
        "currentBlocker": "none; review only the current packet",
        "protectedForbiddenScope": [".git", ".workflow-v2", ".research", "tests", "release", "specs", "credentials", "tokens"],
        "hardConstraints": ["do not modify protected paths", "do not call another tool from GPT review", "return exactly one closed workflow decision marker", "STAGE_READY is technical review, not Human approval"],
        "previousRelevantDecisions": [],
        "latestResult": dict(latest),
        "evidence": evidence,
        "evidenceRoots": ["release", "specs", "src"],
        "gitMetadata": {"rootIdentifier": f"release-validation-{commit[:16]}", "commit": commit, "dirty": dirty, "changedFiles": [SOURCE_PATH] if dirty else []},
    }


def file_digest(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def require_decision(value: Mapping[str, Any], expected: str, field: str) -> Mapping[str, Any]:
    record = value.get(field)
    if not isinstance(record, Mapping) or record.get("decision") != expected or record.get("request_count") != 1:
        raise ValidationFailure(f"real GPT {field} did not return the required {expected} decision")
    if not record.get("conversation_id") or not record.get("consultation_id") or not record.get("packet_digest"):
        raise ValidationFailure(f"real GPT {field} lacks bounded consultation identity")
    return record


def provider_probe(*, python: Path, engine: Path, project: Path, artifact_dir: Path, stage: Mapping[str, Any], baseline: str) -> dict[str, Any]:
    attempt = stage.get("attempt")
    if not isinstance(attempt, Mapping):
        raise ValidationFailure("REQUEST_EXECUTION returned no attempt")
    request_id = str(attempt.get("request_id"))
    result = run(
        [python, engine / "scripts" / "release_provider_probe.py", "--workspace", project, "--artifact-dir", artifact_dir, "--stage-id", STAGE_ID, "--request-id", request_id, "--objective", OBJECTIVE, "--baseline", baseline, "--attempt-index", str(attempt.get("attempt_index", 1)), "--test-command", TEST_COMMAND],
        cwd=project,
        check=False,
        timeout=660,
        env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise ValidationFailure("clean-checkout provider probe returned no bounded JSON") from exc
    if result.returncode != 0 or payload.get("status") != "PASS":
        raise ValidationFailure("real STANDARD provider did not pass the release probe")
    return payload


def run_installation_validation(args: argparse.Namespace) -> dict[str, Any]:
    validation_root = Path(args.validation_root).expanduser().resolve()
    if validation_root == PRODUCT_ROOT or PRODUCT_ROOT in validation_root.parents:
        raise ValidationFailure("validation root must be outside the Product working tree")
    if validation_root.exists() and any(validation_root.iterdir()):
        raise ValidationFailure(f"validation root must be new or empty: {validation_root}")
    validation_root.mkdir(parents=True, exist_ok=True)
    config = Path(args.runtime_config).expanduser().resolve(strict=True)
    bridge_root = Path(args.bridge_root).expanduser().resolve(strict=True)
    profile_dir = Path(args.profile_dir).expanduser().resolve(strict=True)
    codex = shutil.which("codex")
    if not codex:
        raise ValidationFailure("codex executable is not on PATH")
    tag = args.tag
    engine = validation_root / "product-engine"
    engine_env = validation_root / "product-engine-env"
    project = validation_root / "workflow-v2-release-smoke"
    relocated_engine = validation_root / "product-engine-relocated"
    relocated_env = validation_root / "product-engine-relocated-env"
    relocated_project = validation_root / "relocation-smoke"
    artifact_dir = validation_root / "provider-artifacts"
    trace = validation_root / "mcp-transport.trace.jsonl"
    source_commit = git(PRODUCT_ROOT, "rev-parse", tag).stdout.strip()
    run(["git", "clone", "--no-local", "--branch", tag, str(PRODUCT_ROOT), str(engine)], cwd=validation_root, timeout=180)

    original_launcher = PRODUCT_ROOT / "scripts" / "workflow_mcp.py"
    try:
        run([sys.executable, "-m", "venv", "--system-site-packages", str(engine_env)], cwd=validation_root, timeout=180)
        engine_python = engine_env / "Scripts" / "python.exe"
        if not engine_python.is_file():
            raise ValidationFailure("clean checkout virtualenv Python was not created")
        run([engine_python, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "--editable", str(engine)], cwd=validation_root, timeout=300)
        fixture_project(project, label="clean-release")
        temporary_mcp_registration(codex, python=engine_python, launcher=engine / "scripts" / "workflow_mcp.py", config=config, cwd=validation_root)
        doctor = json.loads(run([engine_python, engine / "scripts" / "product_doctor.py", "--workspace", project, "--runtime-config", config, "--json"], cwd=project, timeout=90).stdout)
        if not doctor.get("ready") or doctor.get("writes_performed") or doctor.get("provider_dispatch_performed") or doctor.get("gpt_calls_performed"):
            raise ValidationFailure("clean-checkout Product doctor did not report a read-only ready state")
        audit = json.loads(run([engine_python, engine / "scripts" / "documentation_audit.py", "--root", engine], cwd=engine, timeout=60).stdout)
        if not audit.get("ready") or audit.get("writes_performed"):
            raise ValidationFailure("clean-checkout documentation audit failed")
        initialized = json.loads(run([engine_python, engine / "scripts" / "research_workflow_cli.py", "init", "--workspace", project, "--runtime-config", config, "--json"], cwd=project, timeout=120).stdout)
        if not initialized.get("initialized") or initialized.get("stage_created") or initialized.get("stage_started"):
            raise ValidationFailure("clean-checkout init did not preserve the no-implicit-Stage contract")
        client = MCPClient(python=engine_python, launcher=engine / "scripts" / "workflow_mcp.py", config=config, workspace=project, trace=trace)
        missing = client.call("workflow_resume", {"workspace": str(project)}, allow_error=True)
        if missing.get("error", {}).get("code") != "WORKFLOW_NOT_FOUND":
            raise ValidationFailure("clean project resume did not fail closed before workflow_start")
        brief = {
            "problem_statement": "A bounded add fixture has the wrong arithmetic operation.",
            "goal": OBJECTIVE,
            "desired_outcome": "A passing source fix with a settled and reviewable V2 Stage.",
            "input": "src/add.py and its focused pytest test.",
            "expected_output": "A one-file source diff and passing exact test command.",
            "scope": ["src/add.py"],
            "non_goals": ["tests", "release", "specs", "workflow state", "external cleanup"],
            "constraints": ["non-destructive", "exact test command", "STANDARD provider", "real GPT planning and technical review"],
            "available_assets": ["release/requirement.json", "specs/" + STAGE_ID],
            "acceptance_criteria": ["Provider succeeds", "exact test passes", "only src/add.py changes before integration", "Stage closes after settled integration"],
            "preferences": ["preserve the frozen V1 host and V2 cleanup boundary"],
        }
        client.call("workflow_start", {"workspace": str(project), "mode": "USER_CONFIRMED_BRIEF", "rough_requirement": OBJECTIVE, "brief": brief})
        approved = client.call("workflow_answer", {"workspace": str(project), "approve": True, "mode": "APPROVE"})
        if approved.get("brief_state") != "APPROVED":
            raise ValidationFailure("Human approval did not produce an APPROVED project brief")
        brief_doc = json_file(project / ".research" / "PROJECT_BRIEF.json")
        project_id = brief_doc.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            raise ValidationFailure("project brief has no project identity")
        workspace_id = project_workspace_id(project)
        baseline = git(project, "rev-parse", "HEAD").stdout.strip()
        stage = stage_contract(workspace_id=workspace_id, project_id=project_id, baseline=baseline)
        planning_pack = context_pack(
            stage_goal="Confirm the release fixture and bounded Stage plan are sufficiently scoped for one real execution.",
            latest={"actualWork": "Fresh requirement, specs, and clean baseline exist; no provider action has run.", "tested": ["clean Git baseline", "fresh project identity"], "success": ["scope is src/add.py only", "task is non-destructive"], "failure": [], "change": "release planning gate", "whyConsult": "real GPT planning review is required", "localReferences": ["release/requirement.json", "specs/" + STAGE_ID + "/plan.md"]},
            evidence=[{"sourcePath": "release/requirement.json", "logicalName": "requirement.json", "stagedName": "release/requirement.json", "role": "source"}],
            commit=baseline,
            dirty=False,
        )
        planned = client.call("workflow_run", {"workspace": str(project), "request": {"operation": "PLAN_STAGE", "stage": stage, "planning_revision": 1, "prompt": "Review this fresh bounded release packet for planning only. Confirm it is sufficiently scoped for one real execution. Do not execute or modify anything. Return one final standalone line exactly: WORKFLOW_DECISION: CONTINUE", "context_pack": planning_pack}})
        planning = require_decision(planned, "CONTINUE", "planning")
        started = client.call("workflow_run", {"workspace": str(project), "request": {"operation": "COMMAND", "command": "START", "subject_id": STAGE_ID, "payload": {}, "command_id": "release-validation-start-v1"}})
        if stage_from_view(started).get("status") != "ACTIVE":
            raise ValidationFailure("START did not produce ACTIVE")
        requested = client.call("workflow_run", {"workspace": str(project), "request": {"operation": "COMMAND", "command": "REQUEST_EXECUTION", "subject_id": STAGE_ID, "payload": {"request": {"objective": OBJECTIVE, "allowed_paths": ["src"], "protected_paths": PROTECTED_PATHS, "required_test_command": TEST_COMMAND}, "provenance": {"provider": "openai-codex", "engine_digest": source_commit}, "purpose": "DOMAIN", "capability_manifest": {"query_by_operation_id": False, "idempotent_submit": False, "fence": False, "prove_not_sent": False}}, "command_id": "release-validation-request-v1"}})
        attempt = requested.get("command_result", {}).get("attempt")
        if not isinstance(attempt, Mapping):
            raise ValidationFailure("REQUEST_EXECUTION did not return the committed attempt")
        probe = provider_probe(python=engine_python, engine=engine, project=project, artifact_dir=artifact_dir, stage={"attempt": attempt}, baseline=baseline)
        provider_result = probe.get("result")
        provider_view = probe.get("provider")
        if not isinstance(provider_result, Mapping) or not isinstance(provider_view, Mapping):
            raise ValidationFailure("provider probe returned incomplete bounded evidence")
        if provider_view.get("actual_model") != "gpt-5.6-luna" or provider_view.get("execution_profile") != "STANDARD" or provider_view.get("reasoning_effort") != "max" or provider_view.get("auth_mode") != "chatgpt":
            raise ValidationFailure("provider routing did not match STANDARD/gpt-5.6-luna/max/chatgpt")
        changed = sorted(set(bounded_status(project)) - {".research/PROJECT_BRIEF.json"})
        if changed != [SOURCE_PATH]:
            raise ValidationFailure(f"provider changed unexpected clean-project paths: {changed}")
        provider_result_digest = sha256_json(dict(provider_result))
        manifest_body = {"stage_id": STAGE_ID, "attempt_id": attempt["attempt_id"], "required_artifact_paths": [SOURCE_PATH], "changed_paths": [SOURCE_PATH], "allowed_paths": ["src"], "protected_paths": PROTECTED_PATHS, "path_inventory": [{"path": SOURCE_PATH, "sha256": file_digest(project / SOURCE_PATH)}], "complete": True}
        manifest = {"schema_version": "evidence_manifest.v2", "manifest_id": "manifest-" + sha256_json(manifest_body), **manifest_body}
        observation = {"schema_version": "provider_observation.v2", "observation_id": "pending", "stage_id": STAGE_ID, "iteration_id": attempt["iteration_id"], "attempt_id": attempt["attempt_id"], "provider_result_digest": provider_result_digest, "evidence_manifest_digest": manifest["manifest_id"], "provider_terminal_status": "SUCCEEDED", "raw_provider_claim": {"status": provider_result.get("status"), "provider": "openai-codex", "changed_files": [SOURCE_PATH], "tests": provider_result.get("tests", [])}, "provenance": {"provider": "openai-codex", "executor_request_id": provider_view.get("executor_request_id"), "actual_model": provider_view.get("actual_model"), "execution_profile": provider_view.get("execution_profile"), "reasoning_effort": provider_view.get("reasoning_effort"), "auth_mode": provider_view.get("auth_mode")}, "failure": None, "outputs": {"changed_paths": [SOURCE_PATH], "test_command": TEST_COMMAND}, "immutable": True}
        # The identity algorithm is part of the installed V2 contracts.  The
        # release validator invokes it through a short installed helper below.
        identity_helper = project / ".release-observation-identity.py"
        identity_helper.write_text("from src.workflow_v2_contracts import observation_identity\nimport json,sys\nvalue=json.load(sys.stdin)\nvalue['observation_id']=observation_identity(value)\nprint(json.dumps(value,ensure_ascii=False,sort_keys=True))\n", encoding="utf-8")
        # The helper is intentionally outside tracked scope and is removed
        # before the integration commit; no helper source enters evidence.
        identity_payload = json.dumps(observation, ensure_ascii=False)
        identity_output = subprocess.run([str(engine_python), str(identity_helper)], cwd=str(project), input=identity_payload, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        if identity_output.returncode != 0:
            raise ValidationFailure("installed observation identity helper failed")
        observation = json.loads(identity_output.stdout.strip())
        identity_helper.unlink()
        client.call("workflow_run", {"workspace": str(project), "request": {"operation": "COMMAND", "command": "RECORD_OBSERVATION", "subject_id": STAGE_ID, "payload": {"observation": observation, "effect_state": "SETTLED"}, "command_id": "release-validation-observation-v1"}})
        assessment_input = {"observation": observation, "manifest": manifest, "baseline_digest": baseline, "validator_code_digest": sha256_bytes((engine / "src" / "workflow_v2_contracts.py").read_bytes()), "validation_contract_revision": "release-validation-v1", "checks": {"provider_status": "PASS", "required_test": "PASS", "required_changed_path": "PASS", "nonempty_diff": "PASS", "protected_scope": "PASS", "routing": "STANDARD/gpt-5.6-luna/max/chatgpt"}}
        assessment_process = subprocess.run([str(engine_python), str(engine / "scripts" / "release_assessment_probe.py")], cwd=str(project), input=json.dumps(assessment_input, ensure_ascii=False), text=True, encoding="utf-8", errors="replace", capture_output=True, check=False, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        if assessment_process.returncode != 0:
            raise ValidationFailure("installed assessment contract helper failed")
        try:
            assessment = json.loads(assessment_process.stdout.strip())
        except json.JSONDecodeError as exc:
            raise ValidationFailure("installed assessment contract helper returned invalid JSON") from exc
        client.call("workflow_run", {"workspace": str(project), "request": {"operation": "COMMAND", "command": "ASSESS_RESULT", "subject_id": STAGE_ID, "payload": {"assessment": assessment}, "command_id": "release-validation-assessment-v1"}})
        technical_pack = context_pack(
            stage_goal="Review the admissible provider result for technical readiness for one scoped integration.",
            latest={"actualWork": "The real saved-ChatGPT Codex provider completed the bounded source fix.", "tested": [TEST_COMMAND], "success": ["provider status is SUCCEEDED", "observation is SETTLED", "assessment is ADMISSIBLE", "only src/add.py changed"], "failure": [], "change": f"observation {observation['observation_id']}; assessment {assessment['assessment_id']}", "whyConsult": "technical review is required before integration", "localReferences": ["release/requirement.json", "src/add.py"]},
            evidence=[{"sourcePath": "release/requirement.json", "logicalName": "requirement.json", "stagedName": "release/requirement.json", "role": "source"}],
            commit=baseline,
            dirty=True,
        )
        reviewed = client.call("workflow_run", {"workspace": str(project), "request": {"operation": "CONSULT_REVIEW", "stage_id": STAGE_ID, "review_revision": 1, "prompt": "Perform the technical review using only this fresh packet. The real provider succeeded, the immutable observation is SETTLED, the current assessment is ADMISSIBLE, the exact test passed, and the delta is exactly src/add.py. STAGE_READY is technical readiness for COMMIT_INTEGRATION, not Human approval. Do not execute or modify anything. Return one final standalone line exactly: WORKFLOW_DECISION: STAGE_READY", "context_pack": technical_pack}})
        technical = require_decision(reviewed, "STAGE_READY", "technical_review")
        decision = {"schema_version": "decision.v2", "decision_id": "decision-real-gpt-" + str(technical["response_digest"])[:24], "actor_kind": "GPT", "boundary": "TECHNICAL_REVIEW", "subject_id": assessment["assessment_id"], "subject_digest": assessment["assessment_id"], "subject_version": 1, "allowed_choices": ["STAGE_READY"], "requested_action": "Apply the exact real GPT technical-review result to the current assessment.", "provenance": {"request_count": technical["request_count"], "conversation_id": technical["conversation_id"], "response_digest": technical["response_digest"], "packet_digest": technical["packet_digest"]}, "supersedes": None}
        ready = client.call("workflow_run", {"workspace": str(project), "request": {"operation": "COMMAND", "command": "APPLY_GPT_DECISION", "subject_id": STAGE_ID, "payload": {"decision": decision, "choice": "STAGE_READY"}, "command_id": "release-validation-gpt-ready-v1"}})
        if stage_from_view(ready).get("status") != "READY":
            raise ValidationFailure("real GPT STAGE_READY did not produce READY")
        target_manifest_digest = sha256_json({"path": SOURCE_PATH, "sha256": file_digest(project / SOURCE_PATH), "baseline": baseline})
        committed = client.call("workflow_run", {"workspace": str(project), "request": {"operation": "COMMAND", "command": "COMMIT_INTEGRATION", "subject_id": STAGE_ID, "payload": {"target_manifest_digest": target_manifest_digest, "capability_manifest": {"query_by_operation_id": True, "idempotent_submit": True, "fence": True, "prove_not_sent": True}}, "command_id": "release-validation-commit-integration-v1"}})
        operation = committed.get("command_result", {}).get("operation")
        if not isinstance(operation, Mapping):
            raise ValidationFailure("COMMIT_INTEGRATION did not return an operation envelope")
        operation_id = operation.get("operation_id")
        git(project, "add", SOURCE_PATH)
        if git(project, "diff", "--cached", "--name-only").stdout.splitlines() != [SOURCE_PATH]:
            raise ValidationFailure("integration commit staged an unexpected path")
        git(project, "commit", "-qm", "release validation bounded integration")
        integration_commit = git(project, "rev-parse", "HEAD").stdout.strip()
        resumed_during_intent = client.call("workflow_resume", {"workspace": str(project)})
        intent_stage = stage_from_view(resumed_during_intent)
        if intent_stage.get("status") != "READY" or intent_stage.get("stage_id") != STAGE_ID:
            raise ValidationFailure("process-restart resume lost READY stage identity at integration intent")
        receipt = {"receipt_type": "local_git_integration", "operation_id": operation_id, "status": "PASS", "integration_commit": integration_commit, "target_manifest_digest": target_manifest_digest, "changed_paths": [SOURCE_PATH]}
        settlement_proof = sha256_json({"operation_id": operation_id, "integration_commit": integration_commit, "status": "PASS"})
        settled = client.call("workflow_run", {"workspace": str(project), "request": {"operation": "COMMAND", "command": "APPLY_RECEIPT", "subject_id": STAGE_ID, "payload": {"operation_id": operation_id, "effect_state": "SETTLED", "settlement_proof": settlement_proof, "receipt": receipt, "receipt_digest": sha256_json(receipt)}, "command_id": "release-validation-integration-receipt-v1"}})
        if stage_from_view(settled).get("status") != "READY":
            raise ValidationFailure("settled integration receipt unexpectedly changed stage status")
        test_env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        verification_run = run([engine_python, "-m", "pytest", "-q", TEST_PATH], cwd=project, check=False, timeout=120, env=test_env)
        if verification_run.returncode != 0:
            raise ValidationFailure("post-integration exact test command failed")
        verification = {"command": TEST_COMMAND, "status": "PASS", "returncode": verification_run.returncode, "pytest_plugin_autoload": "disabled"}
        verification_digest = sha256_json({**verification, "integration_commit": integration_commit})
        closed = client.call("workflow_run", {"workspace": str(project), "request": {"operation": "COMMAND", "command": "CLOSEOUT", "subject_id": STAGE_ID, "payload": {"verification_digest": verification_digest, "verification": verification}, "command_id": "release-validation-closeout-v1"}})
        closed_stage = stage_from_view(closed)
        if closed_stage.get("status") != "CLOSED":
            raise ValidationFailure("CLOSEOUT did not produce CLOSED")
        final_revision = revision_from_view(closed)
        duplicate = client.call("workflow_run", {"workspace": str(project), "request": {"operation": "COMMAND", "command": "CLOSEOUT", "subject_id": STAGE_ID, "payload": {"verification_digest": verification_digest, "verification": verification}, "command_id": "release-validation-closeout-v1"}})
        if revision_from_view(duplicate) != final_revision:
            raise ValidationFailure("duplicate CLOSEOUT dispatched a second effect")
        closeout_doc = project / "specs" / STAGE_ID / "CLOSEOUT.md"
        closeout_doc.write_text(f"# Workflow V2 Release Validation Closeout\n\n- Stage: `{STAGE_ID}`\n- Human decision: `ACCEPT_STAGE`\n- Provider: `openai-codex`\n- Routing: `STANDARD / gpt-5.6-luna / max / chatgpt`\n- Changed path: `{SOURCE_PATH}`\n- Required test: `{TEST_COMMAND}`\n- Integration commit: `{integration_commit}`\n- Verification: `PASS`\n- Controller state: `CLOSED`\n\nRaw prompts, responses, credentials, and cookies are not stored in this closeout.\n", encoding="utf-8")
        git(project, "add", str(closeout_doc.relative_to(project)))
        git(project, "commit", "-qm", "release validation closeout evidence")
        final_resumed = client.call("workflow_resume", {"workspace": str(project)})
        if revision_from_view(final_resumed) != final_revision or final_resumed.get("next_action") != "REGISTER_STAGE":
            raise ValidationFailure("fresh process resume did not preserve the closed journal revision")
        final_stage = closed_stage
        if final_stage.get("status") != "CLOSED" or final_stage.get("owner_stage_id") is not None:
            raise ValidationFailure("closed receipt is not CLOSED with a null Stage owner")
        engine_status = bounded_status(engine)
        if engine_status:
            raise ValidationFailure(f"clean installed engine became polluted: {engine_status}")

        run(["git", "clone", "--no-local", "--branch", tag, str(PRODUCT_ROOT), str(relocated_engine)], cwd=validation_root, timeout=180)
        run([sys.executable, "-m", "venv", "--system-site-packages", str(relocated_env)], cwd=validation_root, timeout=180)
        relocated_python = relocated_env / "Scripts" / "python.exe"
        run([relocated_python, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "--editable", str(relocated_engine)], cwd=validation_root, timeout=300)
        fixture_project(relocated_project, label="relocated-engine")
        temporary_mcp_registration(codex, python=relocated_python, launcher=relocated_engine / "scripts" / "workflow_mcp.py", config=config, cwd=validation_root)
        relocation_doctor = json.loads(run([relocated_python, relocated_engine / "scripts" / "product_doctor.py", "--workspace", relocated_project, "--runtime-config", config, "--json"], cwd=relocated_project, timeout=90).stdout)
        if not relocation_doctor.get("ready"):
            raise ValidationFailure("relocated engine doctor failed")
        run([relocated_python, relocated_engine / "scripts" / "research_workflow_cli.py", "init", "--workspace", relocated_project, "--runtime-config", config, "--json"], cwd=relocated_project, timeout=120)
        relocated_client = MCPClient(python=relocated_python, launcher=relocated_engine / "scripts" / "workflow_mcp.py", config=config, workspace=relocated_project, trace=trace)
        relocated_client.call("workflow_start", {"workspace": str(relocated_project), "mode": "USER_CONFIRMED_BRIEF", "rough_requirement": "Relocation smoke project", "brief": {"goal": "Verify relocated Product startup and resume", "problem_statement": "Relocation smoke", "desired_outcome": "A resumable initialized project", "scope": ["src"], "acceptance_criteria": ["doctor ready", "resume preserves project identity"]}})
        relocated_client.call("workflow_answer", {"workspace": str(relocated_project), "approve": True, "mode": "APPROVE"})
        relocated_resumed = relocated_client.call("workflow_resume", {"workspace": str(relocated_project)})
        if relocated_resumed.get("brief_state") != "APPROVED" or relocated_resumed.get("workspace_root") != str(relocated_project.resolve()):
            raise ValidationFailure("relocated engine resume did not preserve project identity")
        if bounded_status(relocated_engine):
            raise ValidationFailure("relocated clean engine became polluted")

        trace_lines = trace.read_text(encoding="utf-8").splitlines() if trace.is_file() else []
        process_starts = sum(1 for line in trace_lines if '"event": "process_start"' in line)
        process_exits = sum(1 for line in trace_lines if '"event": "process_exit"' in line)
        if process_starts < len(client.calls) + len(relocated_client.calls) or process_exits < process_starts:
            raise ValidationFailure("MCP transport trace did not prove fresh process boundaries")
        return {
            "schema_version": "workflow_v2_product_release_validation.v1",
            "status": "PASS",
            "stage_id": STAGE_ID,
            "human_decision": "ACCEPT_STAGE",
            "source_tag": tag,
            "source_commit": source_commit,
            "validation_root": str(validation_root),
            "clean_checkout": {"engine": str(engine), "installed_editable": True, "doctor_ready": True, "documentation_audit_ready": True, "engine_status_clean": True},
            "project": {"root": str(project), "project_id": project_id, "workspace_id": workspace_id, "baseline_commit": baseline, "integration_commit": integration_commit, "closeout_path": str(closeout_doc), "final_status": final_stage.get("status"), "owner_stage_id": final_stage.get("owner_stage_id")},
            "planning": {key: planning.get(key) for key in ("consultation_id", "conversation_id", "request_count", "packet_digest", "decision")},
            "provider": {"provider_id": provider_view.get("provider_id"), "executor_request_id": provider_view.get("executor_request_id"), "actual_model": provider_view.get("actual_model"), "execution_profile": provider_view.get("execution_profile"), "reasoning_effort": provider_view.get("reasoning_effort"), "auth_mode": provider_view.get("auth_mode"), "status": provider_result.get("status"), "changed_files": provider_result.get("changed_files"), "tests": provider_result.get("tests"), "artifacts": str(artifact_dir)},
            "technical_review": {key: technical.get(key) for key in ("consultation_id", "conversation_id", "request_count", "packet_digest", "decision")},
            "controller": {"observation_id": observation["observation_id"], "assessment_id": assessment["assessment_id"], "assessment_verdict": assessment["verdict"], "integration_operation_id": operation_id, "integration_effect_state": "SETTLED", "resume_at_intent": {"status": intent_stage.get("status"), "stage_id": intent_stage.get("stage_id"), "process_restarted": True}, "duplicate_closeout_no_new_effect": True},
            "relocation": {"doctor_ready": True, "resume_identity_preserved": True, "engine_status_clean": True, "engine": str(relocated_engine), "project": str(relocated_project)},
            "transport": {"trace": str(trace), "process_start_count": process_starts, "process_exit_count": process_exits, "fresh_process_per_call": True},
            "cleanup_boundary": {"quarantine_deleted": 0, "deferred_quarantine_touched": False, "move_deferred_touched": False, "unknown_touched": False, "cleanup_v2_or_v3_started": False},
            "writes": {"current_product_worktree": False, "provider_dispatch_performed": True, "gpt_calls_performed": True, "validation_root_only": True},
            "mcp_calls": len(client.calls),
            "relocation_mcp_calls": len(relocated_client.calls),
        }
    finally:
        # Restore the authoritative current checkout registration even when a
        # validation assertion fails.  The config contains only a path and is
        # never echoed or read as a secret.
        temporary_mcp_registration(codex, python=Path(sys.executable), launcher=original_launcher, config=config, cwd=PRODUCT_ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Workflow V2 Product clean-room release validation")
    parser.add_argument("--tag", required=True, help="immutable Product tag to validate")
    parser.add_argument("--validation-root", required=True, help="new external directory; never reuse automatically")
    parser.add_argument("--runtime-config", required=True, help="existing machine-local non-secret runtime config")
    parser.add_argument("--bridge-root", required=True)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--output", required=True, help="new external JSON output path")
    args = parser.parse_args(argv)
    output = Path(args.output).expanduser().resolve()
    if PRODUCT_ROOT == output or PRODUCT_ROOT in output.parents:
        raise SystemExit("refusing to write release output inside the Product working tree")
    if output.exists():
        raise SystemExit("refusing to overwrite release output")
    try:
        result = run_installation_validation(args)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        result = {"schema_version": "workflow_v2_product_release_validation.v1", "status": "FAIL", "error": {"code": type(exc).__name__, "message": str(exc)[:512]}, "validation_root": str(Path(args.validation_root).expanduser().resolve()), "writes": {"current_product_worktree": False}}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
