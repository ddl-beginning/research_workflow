"""Disposable native OpenAI Codex executor E2E.

This script is intentionally independent of the browser bridge and CodexPro.
It creates a temporary Git repository with a deliberately failing ``add``
fixture, builds a bounded planning/ActionMap contract, discovers the native
provider, runs exactly one real ``codex exec`` turn, persists only redacted
run artifacts, and asks an injected bounded review hook to inspect the result.
It exits non-zero when Codex, ChatGPT auth, the requested runtime model, or
the coding-E2E evidence requirements are unavailable; it never fakes Luna and
never falls back to another provider.

Run from the repository root after the user has signed in with ChatGPT:

    python scripts/disposable_openai_codex_e2e.py

The script does not call the existing workflow facade or persist raw Codex
prompt/response material.  The disposable workspace is removed after the run;
artifacts are written to an external directory (or a retained temporary
directory when ``--artifacts-dir`` is omitted).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

SUPERVISOR_ROOT = Path(__file__).resolve().parents[1]
if str(SUPERVISOR_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERVISOR_ROOT))

from src.executor import ExecutionRequest, select_executor
from src.openai_codex_executor import OpenAICodexExecutor, OpenAICodexUnavailable
from src.stage_controller import StageController
from src.workflow_policy import ActionMap, NodeOwner


SOURCE_PATH = "src/add.py"
TEST_COMMAND = "pytest -q tests/test_add.py"
TEST_PATH = "tests/test_add.py"
PROJECT_INSTRUCTIONS_PATH = "AGENTS.override.md"
TASK_OBJECTIVE = (
    f"Only edit {SOURCE_PATH}; fix the add(a, b) subtraction bug; run {TEST_COMMAND} and require PASS; "
    "produce a non-empty diff and do not modify protected paths."
)
ACTION_RATIONALE = TASK_OBJECTIVE


def _contract(root: Path) -> dict[str, Any]:
    return {
        "schema_version": "stage_contract.v1",
        "project_id": "disposable-openai-codex-e2e",
        "repository_root": str(root),
        "stage_id": "stage-native-codex-e2e",
        "stage_name": "disposable native Codex executor",
        "project_goal": "prove subscription-backed native Codex execution",
        "stage_goal": "fix add(a, b) in src/add.py and make the specified pytest pass",
        "user_visible_goal": "verify native Codex evidence through the Stage contract",
        "inputs": [SOURCE_PATH, TEST_PATH],
        "protected_paths": [
            ".git",
            "tests",
            "README.md",
            "production",
            "STAGE_EXECUTION_PLAN.md",
            PROJECT_INSTRUCTIONS_PATH,
        ],
        "allowed_paths": [SOURCE_PATH],
        "acceptance_description": "the normalized native Codex result is accepted by StageController",
        "required_checks": ["native-codex-turn", "required-test-pass", "nonempty-diff"],
        "review_artifact_requirements": ["summary"],
        "baseline": {"fixture": "clean"},
        "status": "PLANNED",
    }


def _git(root: Path, *args: str) -> None:
    completed = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"git setup failed: {args[0] if args else 'command'}")


def _make_disposable_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / SOURCE_PATH).write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (root / TEST_PATH).write_text(
        "from src.add import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8"
    )
    (root / PROJECT_INSTRUCTIONS_PATH).write_text(
        """# Disposable native Codex executor boundary

This is one bounded non-interactive executor turn, not the root workflow
orchestration agent. Execute the task directly; do not spawn subagents,
delegate, or use collaboration tools.

- Modify only `src/add.py`.
- Fix the `add(a, b)` subtraction bug.
- Run exactly `pytest -q tests/test_add.py` and require PASS.
- Require a non-empty diff as evidence of the fix.
- Do not modify `tests/`, `.git/`, `README.md`, this instruction file, or any
  other protected path.
""",
        encoding="utf-8",
    )
    (root / "README.md").write_text("Disposable native Codex executor fixture.\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "codex-e2e@example.invalid")
    _git(root, "config", "user.name", "Codex E2E")
    _git(root, "add", SOURCE_PATH, TEST_PATH, "README.md", PROJECT_INSTRUCTIONS_PATH)
    _git(root, "commit", "-qm", "fixture baseline")


def gpt_review_hook(result: Mapping[str, Any]) -> dict[str, Any]:
    """Bounded review hook; a real GPT review can be injected by the caller."""

    status = str(result.get("status", "")).upper()
    measurements = result.get("measurements", {})
    native = measurements.get("openai_codex", {}) if isinstance(measurements, Mapping) else {}
    acceptance = native.get("acceptance", {}) if isinstance(native, Mapping) else {}
    accepted = status == "SUCCEEDED" and acceptance.get("coding_e2e_accepted") is True
    decision = "ACCEPT" if accepted else "REJECT"
    return {
        "review_hook": "bounded-native-codex-evidence",
        "decision": decision,
        "reason": "turn, required file, required pytest, and non-empty diff are all evidenced" if accepted else "coding E2E evidence is incomplete",
        "status": status,
        "changed_file_count": len(result.get("changed_files", [])),
        "test_count": len(result.get("tests", [])),
        "required_test_passed": acceptance.get("required_test_passed") is True,
        "nonempty_diff": acceptance.get("nonempty_diff") is True,
    }


def run(
    *,
    model_preference: str = "luna",
    fallback_model: str | None = None,
    artifacts_dir: str | None = None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="research-supervisor-native-codex-") as directory:
        root = (Path(directory) / "workspace").resolve()
        root.mkdir(parents=True, exist_ok=True)
        artifact_root = (
            Path(artifacts_dir).expanduser().resolve()
            if artifacts_dir
            else Path(tempfile.mkdtemp(prefix="research-supervisor-native-codex-artifacts-")).resolve()
        )
        _make_disposable_repo(root)
        controller = StageController(_contract(root))
        controller.start_stage(actor="disposable-e2e")

        # This fixture represents the output of a prior GPT planning step.  It
        # is explicit and validated locally before the native provider sees it.
        action_map = ActionMap(
            {
                "run_local_checks": {
                    "owner": NodeOwner.CODEX,
                    "rationale": ACTION_RATIONALE,
                }
            }
        )
        action_map.validate().enable_execution()
        raw_request = controller.request_executor(objective=TASK_OBJECTIVE)
        request = ExecutionRequest.from_stage_request(
            raw_request["request"],
            plan_id="plan-native-codex-e2e",
            task_id=raw_request["request"]["request_id"],
            required_capabilities=("coding", "execution_evidence", "native_codex", "model_discovery", "chatgpt_auth"),
            preferred_provider="openai-codex",
            action_map=action_map.to_dict(),
            workspace_root=str(root),
            metadata={
                "model_preference": model_preference,
                "fallback_model": fallback_model,
                "required_test_command": TEST_COMMAND,
                "required_changed_path": SOURCE_PATH,
                "artifact_dir": str(artifact_root),
            },
        )
        provider = OpenAICodexExecutor(
            workspace_root=str(root),
            preferred_model=model_preference,
            fallback_model=fallback_model,
            artifact_dir=str(artifact_root),
        )
        selection = select_executor(request, [provider])
        result = provider.execute(request)
        recorded = controller.record_executor_result(result.to_stage_result(), request_id=raw_request["request"]["request_id"])
        review = gpt_review_hook(result.to_stage_result())
        return {
            "planning": {"source": "fixture", "action_map_validated": True, "action_map_executable": True},
            "selection": selection.bounded_view(),
            "provider": result.evidence,
            "result": {
                "status": result.result["status"],
                "changed_files": result.result["changed_files"],
                "tests": result.result["tests"],
                "measurements": result.result["measurements"],
                "problems_discovered": result.result["problems_discovered"],
                "stage_ready": result.result["stage_ready"],
            },
            "stage": {"decision": recorded["decision"], "status": recorded["stage"]["status"]},
            "gpt_review": review,
            "artifacts_dir": str(artifact_root),
            "workspace_destroyed": True,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-preference", default="luna", help="runtime model alias or id; defaults to luna")
    parser.add_argument("--fallback-model", default=None, help="explicit runtime model fallback; never implicit")
    parser.add_argument("--artifacts-dir", default=None, help="external directory for bounded run artifacts")
    args = parser.parse_args(argv)
    try:
        payload = run(
            model_preference=args.model_preference,
            fallback_model=args.fallback_model,
            artifacts_dir=args.artifacts_dir,
        )
    except Exception as exc:
        # The broad guard keeps the disposable CLI fail-closed without dumping
        # subprocess stderr, prompts, responses, or auth material.
        print(json.dumps({"status": "UNAVAILABLE", "error": type(exc).__name__, "message": str(exc)[:256]}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("result", {}).get("status") == "SUCCEEDED" and payload.get("gpt_review", {}).get("decision") == "ACCEPT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
