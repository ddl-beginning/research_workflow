#!/usr/bin/env python3
"""Execute one real STANDARD provider request for release validation.

The controller remains outside this helper.  It receives a previously
committed execution request, runs the installed Product provider once, and
prints only the normalized result/evidence needed by the parent validator.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.executor import ExecutionRequest, select_executor  # noqa: E402
from src.openai_codex_executor import OpenAICodexExecutor  # noqa: E402


PROTECTED_PATHS = (".git", ".workflow-v2", ".research", "tests", "release", "specs", "AGENTS.override.md")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one bounded real Product STANDARD provider request")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--stage-id", required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--attempt-index", required=True, type=int)
    parser.add_argument("--test-command", required=True)
    args = parser.parse_args(argv)

    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    workspace = Path(args.workspace).expanduser().resolve(strict=True)
    artifact_dir = Path(args.artifact_dir).expanduser().resolve()
    action_map = {
        "schema_version": "action_map.v1",
        "validated": True,
        "execute": True,
        "nodes": {"bounded_release_fix": {"owner": "CODEX", "rationale": args.objective}},
    }
    request = ExecutionRequest(
        stage_id=args.stage_id,
        objective=args.objective,
        iteration_index=1,
        allowed_paths=("src",),
        protected_paths=PROTECTED_PATHS,
        baseline_digest=args.baseline,
        execution_mode="PRIMARY",
        required_capabilities=("coding", "execution_evidence", "native_codex", "model_discovery", "chatgpt_auth"),
        preferred_provider="openai-codex",
        action_map=action_map,
        plan_id="plan-" + args.stage_id,
        task_id=args.request_id,
        workspace_root=str(workspace),
        metadata={
            "request_id": args.request_id,
            "executor_request_id": args.request_id,
            "execution_profile": "STANDARD",
            "required_test_command": args.test_command,
            "required_changed_path": "src/add.py",
            "artifact_dir": str(artifact_dir),
            "model_preference": "gpt-5.6-luna",
        },
        attempt_index=args.attempt_index,
        retry_budget=2,
    )
    provider = OpenAICodexExecutor(
        workspace_root=str(workspace),
        preferred_model="gpt-5.6-luna",
        fallback_model=None,
        auth_mode="chatgpt",
        artifact_dir=str(artifact_dir),
        timeout_seconds=600.0,
    )
    selection = select_executor(request, [provider])
    execution = provider.execute(request)
    result = execution.to_stage_result()
    provider_view = result.get("measurements", {}).get("openai_codex", {})
    if not isinstance(provider_view, dict):
        provider_view = {}
    payload = {
        "schema_version": "workflow_v2_product_provider_probe.v1",
        "status": "PASS" if result.get("status") == "SUCCEEDED" else "FAIL",
        "result": result,
        "provider": {
            "provider_id": execution.provider_id,
            "executor_request_id": provider_view.get("executor_request_id"),
            "actual_model": provider_view.get("actual_model"),
            "execution_profile": provider_view.get("execution_profile"),
            "reasoning_effort": provider_view.get("reasoning_effort"),
            "auth_mode": provider_view.get("auth_mode"),
            "selection": selection.bounded_view(),
            "evidence": execution.evidence,
        },
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
