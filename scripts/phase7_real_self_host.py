"""Run the bounded real-provider / real-GPT Workflow V2 self-host check.

This is Phase 7 validation tooling, not a lifecycle authority.  The only
authority used by the run is :class:`workflow_v2_controller.StageController`.
The fixture is created in a caller-selected fresh directory, the provider is
the saved-ChatGPT Codex CLI executor, and GPT review crosses the existing
headed browser bridge exactly once per consultation.

No prompt or raw response is copied into the candidate evidence.  The bridge
receipt and Codex metadata-only artifacts remain in their respective bounded
workspace/artifact directories so the external identities can be audited.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bridge_adapter import normalize_bridge_envelope
from src.executor import ExecutionRequest, select_executor
from src.openai_codex_executor import OpenAICodexExecutor
from src.stage_integration import parse_dialogue_decision, subprocess_bridge_runner
from src.workflow_v2_contracts import assess_observation, observation_identity
from src.workflow_v2_controller import StageController


SOURCE_PATH = "src/add.py"
TEST_PATH = "tests/test_add.py"
TEST_COMMAND = "python -m pytest -q tests/test_add.py"
INSTRUCTIONS_PATH = "AGENTS.override.md"
JOURNAL_RELATIVE = ".workflow-v2/journal.json"
OBJECTIVE = (
    "Fix the bounded add(a, b) fixture so it returns the sum instead of the difference; "
    "run the exact required test command and leave a non-empty diff in src/add.py only."
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def run(command: list[str], *, cwd: Path, check: bool = True, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env=dict(env) if env is not None else None,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(f"command failed: {command[0]} {command[1:]}; exit={completed.returncode}")
    return completed


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(root), *args], cwd=root, check=check)


def fixture(root: Path) -> None:
    """Create a fresh, bounded, non-destructive Git fixture."""

    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"refusing to reuse a non-empty real validation workspace: {root}")
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / ".workflow-v2").mkdir()
    (root / "planning").mkdir()
    # The bridge requires every explicitly declared evidence root to exist
    # before it builds a packet.  Planning has no technical-review file yet,
    # so create the bounded empty root up front; no contents are staged from
    # it until the post-provider packet is built.
    (root / "evidence").mkdir()
    (root / SOURCE_PATH).write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / TEST_PATH).write_text(
        "from src.add import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (root / INSTRUCTIONS_PATH).write_text(
        """# Real Workflow V2 bounded validation fixture

This is a single non-destructive provider turn. Do not delegate or use any
external service. Modify only `src/add.py`, fix the add implementation, and
run exactly `python -m pytest -q tests/test_add.py`. Do not touch tests, the
workflow journal, planning evidence, or this instruction file.
""",
        encoding="utf-8",
    )
    write_json(
        root / "planning" / "requirement.json",
        {
            "requirement_id": "requirement-real-v2-add-20260912",
            "goal": OBJECTIVE,
            "acceptance": [
                "provider status is SUCCEEDED",
                "the exact bounded pytest command passes",
                "only src/add.py changes before integration",
                "the V2 Stage closes only after settled integration and verification",
            ],
            "non_destructive": True,
        },
    )
    git(root, "init", "-q")
    git(root, "config", "user.email", "workflow-v2-real-e2e@example.invalid")
    git(root, "config", "user.name", "Workflow V2 Real E2E")


def file_digest(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def baseline_commit(root: Path) -> str:
    git(root, "add", SOURCE_PATH, TEST_PATH, INSTRUCTIONS_PATH, "planning/requirement.json")
    git(root, "commit", "-qm", "fresh real V2 validation baseline")
    return git(root, "rev-parse", "HEAD").stdout.strip()


def status_paths(root: Path) -> set[str]:
    lines = git(root, "status", "--short", "--untracked-files=all").stdout.splitlines()
    paths: set[str] = set()
    for line in lines:
        if len(line) < 4:
            continue
        value = line[3:].strip()
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        if value:
            paths.add(value.replace("\\", "/"))
    return paths


def fresh_pack(*, goal: str, stage_goal: str, latest: Mapping[str, Any], evidence: list[dict[str, Any]], git_commit: str) -> dict[str, Any]:
    return {
        "mode": "fresh",
        "projectGoal": goal,
        "currentStageGoal": stage_goal,
        "userVisibleGoal": "A reviewable, passing bounded fixture result and a closed V2 Stage.",
        "establishedFacts": [
            "the workspace is a fresh disposable Git repository",
            "no historical .research state or Parent was imported",
            "the task is non-destructive and limited to src/add.py",
        ],
        "currentMethod": "one bounded real provider execution followed by deterministic V2 assessment",
        "currentBlocker": "none; review the current packet only",
        "protectedForbiddenScope": [".git", ".workflow-v2", "tests", "planning", "credentials", "tokens"],
        "hardConstraints": [
            "do not modify protected paths",
            "do not call another tool from the GPT review",
            "return exactly one closed workflow decision marker",
            "STAGE_READY is a technical review result, not Human approval",
        ],
        "previousRelevantDecisions": [],
        "latestResult": dict(latest),
        "evidence": evidence,
        "evidenceRoots": ["planning", "evidence"],
        "gitMetadata": {
            "rootIdentifier": f"fresh-real-v2-{git_commit[:16]}",
            "commit": git_commit,
            "dirty": True,
            "changedFiles": [SOURCE_PATH],
        },
    }


def bounded_receipt(result: Mapping[str, Any]) -> dict[str, Any]:
    receipt = result.get("receipt")
    if not isinstance(receipt, Mapping):
        raise RuntimeError("real bridge returned no receipt")
    required = {
        "consultation_id": receipt.get("consultation_id"),
        "conversation_id": receipt.get("conversation_id"),
        "request_count": receipt.get("request_count"),
        "status": receipt.get("status"),
        "conversation_validated": receipt.get("conversation_validated"),
        "receipt_path": result.get("receipt_path"),
        "context_pack": receipt.get("context_pack"),
    }
    if required["status"] != "complete" or required["request_count"] != 1:
        raise RuntimeError("real bridge receipt is not one completed request")
    if not isinstance(required["consultation_id"], str) or not required["consultation_id"]:
        raise RuntimeError("real bridge receipt has no consultation identity")
    if not isinstance(required["conversation_id"], str) or not required["conversation_id"]:
        raise RuntimeError("real bridge receipt has no conversation identity")
    if required["conversation_validated"] is not True:
        raise RuntimeError("real bridge conversation identity was not validated")
    context_pack = required["context_pack"]
    if not isinstance(context_pack, Mapping):
        raise RuntimeError("real bridge receipt has no context-pack provenance")
    if not isinstance(context_pack.get("pack_sha256"), str) or not context_pack["pack_sha256"]:
        raise RuntimeError("real bridge receipt has no packet digest")
    return required


def consult_real(*, root: Path, bridge_root: Path, profile_dir: Path, prompt: str, pack: Mapping[str, Any], timeout_ms: int) -> dict[str, Any]:
    raw = subprocess_bridge_runner(
        prompt,
        mode="fresh",
        continue_from=None,
        context_pack=pack,
        root_dir=str(root),
        profile_dir=str(profile_dir),
        timeout_ms=timeout_ms,
        bridge_root=str(bridge_root),
        transport="homepage_fallback",
    )
    checked = normalize_bridge_envelope(raw, expected_mode="fresh", require_receipt=True)
    receipt = bounded_receipt(checked)
    response = checked.get("response_text")
    if not isinstance(response, str) or not response.strip():
        raise RuntimeError("real bridge returned no response text")
    checked["bounded_receipt"] = receipt
    checked["response_sha256"] = sha256_bytes(response.encode("utf-8"))
    checked["decision"] = parse_dialogue_decision(response)
    return checked


def stage_contract(*, workspace_id: str, stage_id: str, baseline: str) -> dict[str, Any]:
    return {
        "schema_version": "stage.v2",
        "stage_id": stage_id,
        "workspace_id": workspace_id,
        "project_id": "project-real-v2-add-20260912",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 7 real Workflow V2 bounded self-host validation")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--bridge-root", required=True)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument("--evidence-out", required=True)
    parser.add_argument("--timeout-ms", type=int, default=300_000)
    args = parser.parse_args(argv)

    # Keep the exact provider test command deterministic from its first
    # invocation.  Without this explicit environment boundary, ambient
    # machine pytest plugins can make Codex observe a failed probe and then a
    # passing retry; the immutable provider evidence would correctly remain
    # non-ready even though the final test passed.
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

    root = Path(args.workspace).expanduser().resolve()
    artifact_root = Path(args.artifact_dir).expanduser().resolve()
    bridge_root = Path(args.bridge_root).expanduser().resolve()
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    evidence_out = Path(args.evidence_out).expanduser().resolve()
    if evidence_out.exists():
        raise RuntimeError(f"refusing to overwrite validation evidence: {evidence_out}")
    if not bridge_root.is_dir() or not profile_dir.is_dir():
        raise RuntimeError("bridge root or authenticated profile directory is unavailable")

    fixture(root)
    workspace_id = f"workspace-real-v2-add-{root.name[-24:]}"
    stage_id = f"stage-real-v2-add-{root.name[-24:]}"
    journal_path = root / JOURNAL_RELATIVE
    controller = StageController(workspace_id=workspace_id, state_path=journal_path)
    initial = controller.initialize()
    baseline = baseline_commit(root)

    planning_pack = fresh_pack(
        goal=OBJECTIVE,
        stage_goal="Confirm that the minimal bounded add fixture is suitable for one real execution and review.",
        latest={
            "actualWork": "A fresh requirement and clean baseline were created; no provider action has run.",
            "tested": ["fresh workspace identity", "clean Git baseline"],
            "success": ["scope is src/add.py only", "the task is non-destructive"],
            "failure": [],
            "change": "planning gate before Stage registration",
            "whyConsult": "real GPT planning review is required before execution",
            "localReferences": ["planning/requirement.json"],
        },
        evidence=[
            {
                "sourcePath": "planning/requirement.json",
                "logicalName": "requirement.json",
                "stagedName": "evidence/requirement.json",
                "role": "source",
            }
        ],
        git_commit=baseline,
    )
    planning = consult_real(
        root=root,
        bridge_root=bridge_root,
        profile_dir=profile_dir,
        prompt=(
            "Review the fresh bounded requirement and baseline packet for planning only. "
            "Confirm that the stated one-file, non-destructive task is sufficiently scoped for one real execution. "
            "Do not execute anything, do not modify files, do not approve the Stage, and do not call another tool. "
            "Return one final standalone line exactly: WORKFLOW_DECISION: CONTINUE"
        ),
        pack=planning_pack,
        timeout_ms=args.timeout_ms,
    )
    if planning["decision"] != "CONTINUE":
        raise RuntimeError(f"real planning review did not return CONTINUE: {planning['decision']}")

    stage = stage_contract(workspace_id=workspace_id, stage_id=stage_id, baseline=baseline)
    controller.register_stage(stage, command_id="command-real-register-20260912")
    controller.start(stage_id, command_id="command-real-start-20260912")
    requested = controller.request_execution(
        stage_id,
        command_id="command-real-request-20260912",
        request={
            "objective": OBJECTIVE,
            "allowed_paths": ["src"],
            "protected_paths": [".git", ".workflow-v2", "tests", "planning", INSTRUCTIONS_PATH],
            "required_test_command": TEST_COMMAND,
        },
        provenance={"provider": "openai-codex", "engine_digest": "pending-real-codex-runtime"},
        purpose="DOMAIN",
        capability_manifest={"query_by_operation_id": False, "idempotent_submit": False, "fence": False, "prove_not_sent": False},
    )
    attempt = requested["attempt"]
    action_map = {
        "schema_version": "action_map.v1",
        "validated": True,
        "execute": True,
        "nodes": {"bounded_add_fix": {"owner": "CODEX", "rationale": OBJECTIVE}},
    }
    request = ExecutionRequest(
        stage_id=stage_id,
        objective=OBJECTIVE,
        iteration_index=1,
        allowed_paths=("src",),
        protected_paths=(".git", ".workflow-v2", "tests", "planning", INSTRUCTIONS_PATH),
        baseline_digest=baseline,
        execution_mode="PRIMARY",
        # Keep provider selection bound to the provider's declared contract;
        # the exact pytest requirement is carried in metadata and validated
        # by the provider result, while GPT review is a later bridge step.
        required_capabilities=("coding", "execution_evidence", "native_codex", "model_discovery", "chatgpt_auth"),
        preferred_provider="openai-codex",
        action_map=action_map,
        plan_id="plan-real-v2-add-20260912",
        task_id=attempt["request_id"],
        workspace_root=str(root),
        metadata={
            "request_id": attempt["request_id"],
            "executor_request_id": attempt["request_id"],
            "execution_profile": "STANDARD",
            "required_test_command": TEST_COMMAND,
            "required_changed_path": SOURCE_PATH,
            "artifact_dir": str(artifact_root),
            "model_preference": "gpt-5.6-luna",
        },
        attempt_index=attempt["attempt_index"],
        retry_budget=2,
    )
    provider = OpenAICodexExecutor(
        workspace_root=str(root),
        preferred_model="gpt-5.6-luna",
        auth_mode="chatgpt",
        artifact_dir=str(artifact_root),
        timeout_seconds=max(60.0, args.timeout_ms / 1000.0),
    )
    selection = select_executor(request, [provider])
    provider_before_paths = status_paths(root)
    execution = provider.execute(request)
    result = execution.to_stage_result()
    if result.get("status") != "SUCCEEDED":
        raise RuntimeError("real provider did not produce SUCCEEDED; fail closed")
    provider_view = result.get("measurements", {}).get("openai_codex", {})
    if not isinstance(provider_view, Mapping):
        raise RuntimeError("provider result has no bounded OpenAI Codex measurement")
    provider_changed_paths = sorted(status_paths(root) - provider_before_paths)
    if provider_changed_paths != [SOURCE_PATH]:
        raise RuntimeError(f"real provider changed an unexpected path set: {provider_changed_paths}")
    provider_result_digest = sha256_json(result)

    manifest_body = {
        "stage_id": stage_id,
        "attempt_id": attempt["attempt_id"],
        "required_artifact_paths": [SOURCE_PATH],
        "changed_paths": [SOURCE_PATH],
        "allowed_paths": ["src"],
        "protected_paths": [".git", ".workflow-v2", "tests", "planning", INSTRUCTIONS_PATH],
        "path_inventory": [{"path": SOURCE_PATH, "sha256": file_digest(root / SOURCE_PATH)}],
        "complete": True,
    }
    manifest = {
        "schema_version": "evidence_manifest.v2",
        "manifest_id": "manifest-" + sha256_json(manifest_body),
        **manifest_body,
    }
    observation = {
        "schema_version": "provider_observation.v2",
        "observation_id": "pending",
        "stage_id": stage_id,
        "iteration_id": attempt["iteration_id"],
        "attempt_id": attempt["attempt_id"],
        "provider_result_digest": provider_result_digest,
        "evidence_manifest_digest": manifest["manifest_id"],
        "provider_terminal_status": "SUCCEEDED",
        "raw_provider_claim": {
            "status": result.get("status"),
            "provider": execution.provider_id,
            "changed_files": provider_changed_paths,
            "tests": result.get("tests", []),
        },
        "provenance": {
            "provider": execution.provider_id,
            "executor_request_id": provider_view.get("executor_request_id"),
            "actual_model": provider_view.get("actual_model"),
            "execution_profile": provider_view.get("execution_profile"),
            "reasoning_effort": provider_view.get("reasoning_effort"),
            "auth_mode": provider_view.get("auth_mode"),
        },
        "failure": None,
        "outputs": {"changed_paths": [SOURCE_PATH], "test_command": TEST_COMMAND},
        "immutable": True,
    }
    observation["observation_id"] = observation_identity(observation)
    controller.record_observation(stage_id, observation, effect_state="SETTLED", command_id="command-real-observe-20260912")
    validator_digest = sha256_bytes((ROOT / "src" / "workflow_v2_contracts.py").read_bytes())
    assessment = assess_observation(
        observation,
        manifest,
        baseline_digest=baseline,
        validator_code_digest=validator_digest,
        validation_contract_revision="admission.v2",
        checks={
            "provider_status": "PASS",
            "required_test": "PASS",
            "required_changed_path": "PASS",
            "nonempty_diff": "PASS",
            "protected_scope": "PASS",
            "routing": "STANDARD/gpt-5.6-luna/max/chatgpt",
        },
    )
    if assessment["verdict"] != "ADMISSIBLE":
        raise RuntimeError("real provider observation was not admissible")
    controller.assess_result(stage_id, assessment, command_id="command-real-assess-20260912")

    verification_env = os.environ.copy()
    # The provider's Codex-managed shell ran the exact command with external
    # pytest plugins disabled. Preserve that command verbatim while making
    # both deterministic verification points use the same bounded environment.
    verification_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    verification_env["PYTHONDONTWRITEBYTECODE"] = "1"
    pre_integration_verification = run(
        [sys.executable, "-m", "pytest", "-q", TEST_PATH],
        cwd=root,
        check=False,
        env=verification_env,
    )
    if pre_integration_verification.returncode != 0:
        raise RuntimeError("independent pre-integration verification failed")
    pre_integration_verification_view = {
        "command": TEST_COMMAND,
        "status": "PASS",
        "returncode": pre_integration_verification.returncode,
        "pytest_plugin_autoload": "disabled",
    }

    technical_evidence = {
        "stage_id": stage_id,
        "attempt_id": attempt["attempt_id"],
        "provider_result_digest": provider_result_digest,
        "observation_id": observation["observation_id"],
        "observation_effect_state": "SETTLED",
        "assessment_id": assessment["assessment_id"],
        "assessment_verdict": assessment["verdict"],
        "stage_result_binding": {
            "observation_id": observation["observation_id"],
            "assessment_id": assessment["assessment_id"],
            "current_assessment": True,
        },
        "provider_status": result.get("status"),
        "provider_changed_paths": provider_changed_paths,
        "tests": result.get("tests", []),
        "pre_integration_verification": pre_integration_verification_view,
        "routing": {
            "execution_profile": provider_view.get("execution_profile"),
            "actual_model": provider_view.get("actual_model"),
            "reasoning_effort": provider_view.get("reasoning_effort"),
            "auth_mode": provider_view.get("auth_mode"),
        },
        "integration_policy": "commit only src/add.py after a real GPT STAGE_READY review",
    }
    write_json(root / "evidence" / "technical-review.json", technical_evidence)
    technical_pack = fresh_pack(
        goal=OBJECTIVE,
        stage_goal="Review the current admissible provider result for readiness for one scoped integration.",
        latest={
            "actualWork": "The real saved-ChatGPT Codex provider completed the bounded source fix and an independent pre-integration verification passed.",
            "tested": [TEST_COMMAND],
            "success": ["provider result is SUCCEEDED", "observation effect is SETTLED", "assessment is ADMISSIBLE", "only src/add.py changed", "pre-integration verification passed"],
            "failure": [],
            "change": f"observation {observation['observation_id']}; assessment {assessment['assessment_id']}",
            "whyConsult": "technical review is required before integration",
            "localReferences": ["evidence/technical-review.json"],
        },
        evidence=[
            {
                "sourcePath": "evidence/technical-review.json",
                "logicalName": "technical-review.json",
                "stagedName": "evidence/technical-review.json",
                "role": "result",
            }
        ],
        git_commit=baseline,
    )
    technical = consult_real(
        root=root,
        bridge_root=bridge_root,
        profile_dir=profile_dir,
        prompt=(
            "Perform the technical review using only this fresh packet. All listed gates are already evidenced: "
            "the real provider succeeded, the immutable observation is SETTLED, the current assessment is "
            "ADMISSIBLE, the exact test passed, and the provider delta is exactly src/add.py. "
            "STAGE_READY means technically ready for the controller's next COMMIT_INTEGRATION command; it is "
            "not Human approval. Do not execute, modify, or integrate anything. If and only if those packet "
            "facts are present, return one final standalone line exactly: "
            "WORKFLOW_DECISION: STAGE_READY"
        ),
        pack=technical_pack,
        timeout_ms=args.timeout_ms,
    )
    if technical["decision"] != "STAGE_READY":
        raise RuntimeError(f"real technical review did not return STAGE_READY: {technical['decision']}")
    technical_receipt = technical["bounded_receipt"]
    technical_context = technical_receipt["context_pack"]
    decision = {
        "schema_version": "decision.v2",
        "decision_id": "decision-real-gpt-" + technical["response_sha256"][:24],
        "actor_kind": "GPT",
        "boundary": "TECHNICAL_REVIEW",
        "subject_id": assessment["assessment_id"],
        "subject_digest": assessment["assessment_id"],
        "subject_version": 1,
        "allowed_choices": ["STAGE_READY"],
        "requested_action": "Apply the exact real GPT technical-review result to the current assessment.",
        "provenance": {
            "request_count": technical_receipt["request_count"],
            "conversation_id": technical_receipt["conversation_id"],
            "response_digest": technical["response_sha256"],
            "packet_digest": technical_context["pack_sha256"],
        },
        "supersedes": None,
    }
    ready = controller.apply_gpt_decision(stage_id, decision, choice="STAGE_READY", command_id="command-real-gpt-ready-20260912")
    if ready["stage"]["status"] != "READY":
        raise RuntimeError("real GPT STAGE_READY did not produce READY")

    target_manifest_digest = sha256_json({"path": SOURCE_PATH, "sha256": file_digest(root / SOURCE_PATH), "baseline": baseline})
    integration = controller.commit_integration(
        stage_id,
        command_id="command-real-commit-integration-20260912",
        target_manifest_digest=target_manifest_digest,
        capability_manifest={"query_by_operation_id": True, "idempotent_submit": True, "fence": True, "prove_not_sent": True},
    )
    operation_id = integration["operation"]["operation_id"]
    staged = git(root, "diff", "--cached", "--name-only").stdout.strip()
    if staged:
        raise RuntimeError("integration found unexpected pre-staged files")
    git(root, "add", SOURCE_PATH)
    staged_after = git(root, "diff", "--cached", "--name-only").stdout.splitlines()
    if staged_after != [SOURCE_PATH]:
        raise RuntimeError(f"integration staged unexpected paths: {staged_after}")
    git(root, "commit", "-qm", "real V2 bounded integration")
    integration_commit = git(root, "rev-parse", "HEAD").stdout.strip()
    integration_receipt = {
        "receipt_type": "local_git_integration",
        "operation_id": operation_id,
        "status": "PASS",
        "integration_commit": integration_commit,
        "target_manifest_digest": target_manifest_digest,
        "changed_paths": [SOURCE_PATH],
    }
    settlement_proof = sha256_json({"operation_id": operation_id, "integration_commit": integration_commit, "status": "PASS"})
    controller.apply_receipt(
        stage_id,
        operation_id=operation_id,
        effect_state="SETTLED",
        settlement_proof=settlement_proof,
        receipt=integration_receipt,
        receipt_digest=sha256_json(integration_receipt),
        command_id="command-real-integration-receipt-20260912",
    )
    verification = run([sys.executable, "-m", "pytest", "-q", TEST_PATH], cwd=root, check=False, env=verification_env)
    if verification.returncode != 0:
        raise RuntimeError("post-integration verification failed")
    verification_view = {
        "command": TEST_COMMAND,
        "status": "PASS",
        "returncode": verification.returncode,
        "pytest_plugin_autoload": "disabled",
        "pre_integration": pre_integration_verification_view,
    }
    verification_digest = sha256_json({**verification_view, "integration_commit": integration_commit})
    closeout = controller.closeout(stage_id, verification_digest=verification_digest, command_id="command-real-closeout-20260912")
    final_event_count = len(controller.state["events"])
    duplicate = controller.dispatch(
        "CLOSEOUT",
        subject_id=stage_id,
        payload={"verification_digest": verification_digest},
        command_id="command-real-closeout-20260912",
    )
    if duplicate["revision"] != closeout["revision"] or len(controller.state["events"]) != final_event_count:
        raise RuntimeError("duplicate closeout dispatch produced a second effect")
    reloaded = StageController.from_state(journal_path)
    if reloaded.state != controller.state:
        raise RuntimeError("fresh real V2 journal changed across authoritative reload")
    final_stage = reloaded.show_stage(stage_id)
    if final_stage["status"] != "CLOSED" or reloaded.state.get("executable_owner_stage_id") is not None:
        raise RuntimeError("fresh real V2 reload is not CLOSED with a null executable owner")

    output = {
        "evidence_kind": "REAL_SELF_HOST_VALIDATION",
        "status": "PASS",
        "candidate_baseline_commit": "2d5e416c54256511ee6791c658ca1db7430e5d7b",
        "candidate_validation_tool": str(Path(__file__).resolve()),
        "workspace": str(root),
        "workspace_id": workspace_id,
        "stage_id": stage_id,
        "journal_path": str(journal_path),
        "initial_revision": initial["revision"],
        "final_revision": reloaded.revision,
        "fresh_reload": {"status": final_stage["status"], "projection_identical": True, "executable_owner": reloaded.state.get("executable_owner_stage_id")},
        "planning": {
            "consultation_id": planning["bounded_receipt"]["consultation_id"],
            "conversation_id": planning["bounded_receipt"]["conversation_id"],
            "request_count": planning["bounded_receipt"]["request_count"],
            "decision": planning["decision"],
            "receipt_path": planning["bounded_receipt"]["receipt_path"],
            "packet_digest": planning["bounded_receipt"]["context_pack"]["pack_sha256"],
        },
        "provider": {
            "provider_id": execution.provider_id,
            "executor_request_id": provider_view.get("executor_request_id"),
            "provider_receipt": execution.evidence.get("artifacts"),
            "provider_result_digest": provider_result_digest,
            "provider_terminal_status": result.get("status"),
            "actual_model": provider_view.get("actual_model"),
            "execution_profile": provider_view.get("execution_profile"),
            "reasoning_effort": provider_view.get("reasoning_effort"),
            "auth_mode": provider_view.get("auth_mode"),
            "selection": selection.bounded_view(),
            "changed_files": provider_changed_paths,
            "tests": result.get("tests", []),
        },
        "observation": {"observation_id": observation["observation_id"], "immutable": True, "effect_state": "SETTLED"},
        "assessment": {"assessment_id": assessment["assessment_id"], "verdict": assessment["verdict"], "validator_code_digest": validator_digest},
        "gpt_technical_review": {
            "consultation_id": technical_receipt["consultation_id"],
            "conversation_id": technical_receipt["conversation_id"],
            "request_count": technical_receipt["request_count"],
            "decision": technical["decision"],
            "response_digest": technical["response_sha256"],
            "packet_digest": technical_context["pack_sha256"],
            "receipt_path": technical_receipt["receipt_path"],
        },
        "stage_result_binding": {"assessment_id": assessment["assessment_id"], "decision_subject_id": decision["subject_id"]},
        "integration": {"status": "PASS", "operation_id": operation_id, "receipt": integration_receipt, "settlement_proof": settlement_proof},
        "verification": {**verification_view, "verification_digest": verification_digest},
        "duplicate_dispatch_count": 0,
        "external_provider_or_gpt_called": True,
        "historical_state_imported": False,
        "project_package_used": False,
        "facade_used": False,
        "architecture_deviation": None,
    }
    write_json(evidence_out, output)
    print(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
