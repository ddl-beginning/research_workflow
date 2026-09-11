#!/usr/bin/env python3
"""Run the two-request Step 9 headed bridge smoke on a disposable Git repo.

This is a deliberately explicit harness, not a product CLI.  It creates a
temporary fixture, performs one NORMAL consultation and one FRESH closeout in
series, then stops at STAGE_READY.  Set ``--run-real`` only after confirming
the headed ChatGPT profile is logged in and cooled down.  The existing Node
bridge removes request/response bodies after each call; this harness keeps
only bounded receipts and the integration receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping

SUPERVISOR_ROOT = Path(__file__).resolve().parents[1]
if str(SUPERVISOR_ROOT) not in sys.path:
    sys.path.insert(0, str(SUPERVISOR_ROOT))

from src.artifacts import ArtifactRegistry
from src.git_baseline import capture_git_baseline
from src.stage_controller import StageController, StageState
from src.stage_integration import (
    INTEGRATION_BLOCKED_MARKER,
    INTEGRATION_MARKER,
    StageIntegrationAdapter,
    StageIntegrationError,
    subprocess_bridge_runner,
)


BRIDGE_ROOT = Path(__file__).resolve().parents[2] / "chatgpt_browser_bridge"


def invoke_bridge_runner(
    prompt: str,
    *,
    runner: Callable[..., Mapping[str, Any]] = subprocess_bridge_runner,
    **kwargs: Any,
) -> Mapping[str, Any]:
    """Call the production bridge runner with one merged keyword set.

    This is a harness seam only: browser behavior remains in
    ``subprocess_bridge_runner``.  Project-scoped callers may already provide
    ``bridge_root``; ``setdefault`` preserves it without creating Python's
    duplicate-keyword ``TypeError`` before the subprocess is reached.
    """

    bridge_kwargs = dict(kwargs)
    bridge_kwargs.setdefault("bridge_root", BRIDGE_ROOT)
    return runner(prompt, **bridge_kwargs)


def run_git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=check, capture_output=True, text=True
    )
    return result.stdout.strip()


def make_fixture() -> Path:
    root = Path(tempfile.mkdtemp(prefix="step9-disposable-"))
    run_git(root, "init", "-q")
    (root / ".research" / "artifacts").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "input.txt").write_text("deterministic input\n", encoding="utf-8")
    (root / ".research" / "README.md").write_text(
        "This repository is a disposable Step 9 fixture.\n", encoding="utf-8"
    )
    run_git(root, "add", ".")
    run_git(
        root,
        "-c",
        "user.name=Step9 Test",
        "-c",
        "user.email=step9@example.test",
        "commit",
        "-qm",
        "initial disposable fixture",
    )
    return root


def stage_contract(root: Path, baseline_digest: str) -> dict[str, Any]:
    return {
        "schema_version": "stage_contract.v1",
        "plan_id": "plan-step9-disposable",
        "project_id": "step9-disposable-project",
        "repository_root": str(root),
        "stage_id": "stage-step9-real",
        "stage_name": "headed bridge integration",
        "project_goal": "produce a bounded user-visible fixture artifact",
        "stage_goal": "improve the disposable fixture result and verify closeout",
        "user_visible_goal": "show a stable fixture result with passing checks",
        "inputs": ["input.txt"],
        "protected_paths": [".git", ".auth"],
        "allowed_paths": [".research", "tests"],
        "acceptance_description": "checks pass and a final metrics artifact is reviewable",
        "required_checks": ["unit", "measurement"],
        "review_artifact_requirements": ["metrics_summary"],
        "baseline": {"git_baseline_digest": baseline_digest, "score": 1.0},
        "status": "PLANNED",
        "max_iterations": 8,
    }


def pack_spec(adapter: StageIntegrationAdapter, *, mode: str, root: Path, baseline: dict[str, Any]) -> dict[str, Any]:
    """Build a bridge context-pack spec from current bounded local evidence."""

    evidence = adapter.latest_evidence
    result = dict(evidence.payload if evidence is not None else {})
    return {
        "mode": mode.lower(),
        "projectGoal": adapter.show_stage()["contract"]["project_goal"],
        "currentStageGoal": adapter.show_stage()["contract"]["stage_goal"],
        "userVisibleGoal": adapter.show_stage()["contract"]["user_visible_goal"],
        "establishedFacts": [
            "the repository is a disposable Git fixture",
            "the baseline was captured before local iteration",
            f"evidence revision {evidence.revision if evidence else 0} is current",
        ],
        "currentMethod": "one bounded local fixture transformation",
        "currentBlocker": "none; independent closeout review is required after the local action",
        "protectedForbiddenScope": [".git", ".auth", "credentials", "tokens"],
        "hardConstraints": [
            "stay inside the disposable repository",
            "one bridge request per invocation",
            "STAGE_READY requires an explicit closeout review and human approval remains separate",
        ],
        "previousRelevantDecisions": [],
        "latestResult": {
            "actualWork": "Codex completed one bounded fixture transformation.",
            "tested": ["unit fixture check", "measurement fixture check"],
            "success": ["the local result is deterministic", "the evidence digest is new"],
            "failure": [],
            "change": f"evidence revision {evidence.revision if evidence else 0}; digest {evidence.digest[:16] if evidence else 'none'}",
            "whyConsult": "A bounded ChatGPT review is required before the next local action or closeout.",
            "localReferences": [".research/result.txt", ".research/metrics_summary.json"],
        },
        "evidence": [
            {
                "sourcePath": ".research/result.txt",
                "logicalName": "result.txt",
                "stagedName": "evidence/result.txt",
                "role": "result",
            },
        ],
        "evidenceRoots": [".research"],
        "gitMetadata": {
            "rootIdentifier": f"repo-{baseline['digest'][:16]}",
            "commit": baseline.get("commit_sha"),
            "dirty": baseline.get("dirty"),
            "changedFiles": [],
        },
    }


def assert_low_load_pack_spec(pack: dict[str, Any], *, mode: str) -> None:
    """Keep the real smoke packet to STAGE_CONTEXT/LATEST_RESULT/one evidence."""

    if "metrics" in pack or "diffSummary" in pack:
        raise StageIntegrationError(
            "E2E_PACK_NOT_MINIMAL",
            "real smoke pack must omit generated METRICS and DIFF_SUMMARY",
        )
    evidence = pack.get("evidence")
    if not isinstance(evidence, list) or len(evidence) != 1:
        raise StageIntegrationError(
            "E2E_PACK_NOT_MINIMAL",
            "real smoke pack must contain exactly one representative evidence file",
        )
    if mode.lower() == "fresh":
        serialized = json.dumps(pack, ensure_ascii=False)
        if any(
            forbidden in serialized
            for forbidden in ("previousRecommendation", "previous_recommendation", "sunkCostNarrative")
        ):
            raise StageIntegrationError(
                "E2E_FRESH_ISOLATION_FAILED",
                "FRESH pack contains a previous recommendation field",
            )


def assert_bounded_pack_receipt(handle: Any, *, mode: str) -> None:
    """Verify the bridge materialized no more than three attachments."""

    receipt = handle.receipt if hasattr(handle, "receipt") else {}
    metadata = receipt.get("context_pack") if isinstance(receipt, dict) else None
    count = metadata.get("attachment_count") if isinstance(metadata, dict) else None
    if not isinstance(count, int) or count > 3:
        raise StageIntegrationError(
            "E2E_PACK_ATTACHMENT_BOUND_EXCEEDED",
            "real smoke context pack exceeded the three-attachment low-load bound",
            details={"mode": mode, "attachment_count": count},
        )
    if mode.upper() == "FRESH":
        serialized = json.dumps(metadata or {}, ensure_ascii=False)
        if any(
            forbidden in serialized
            for forbidden in ("previousRecommendation", "previous_recommendation", "sunkCostNarrative")
        ):
            raise StageIntegrationError(
                "E2E_FRESH_ISOLATION_FAILED",
                "FRESH receipt metadata contains a previous recommendation field",
            )


def run_real(
    profile_dir: str | None = None,
    *,
    resume_normal_receipt: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    root = make_fixture()
    baseline = capture_git_baseline(root, stage_id="stage-step9-real")
    contract = stage_contract(root, baseline["digest"])
    controller = StageController(contract)

    replay_receipt: dict[str, Any] | None = None
    if resume_normal_receipt is not None:
        replay_path = Path(resume_normal_receipt).expanduser().resolve()
        try:
            parsed = json.loads(replay_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise StageIntegrationError("RESUME_RECEIPT_INVALID", "the completed NORMAL receipt could not be read", cause=error) from error
        if not isinstance(parsed, dict) or parsed.get("status") != "complete" or parsed.get("request_count") != 1:
            raise StageIntegrationError("RESUME_RECEIPT_INVALID", "resume receipt must be a complete one-request receipt")
        replay_receipt = parsed
    bridge_calls = 0

    def bridge_runner(prompt: str, **kwargs):
        nonlocal bridge_calls
        bridge_calls += 1
        if replay_receipt is not None and bridge_calls == 1:
            # The first NORMAL request already completed in the previous
            # process before its UTF-8 capture bug was fixed.  Replay only its
            # bounded receipt and known routing marker; this path sends no
            # request and exists solely to avoid a duplicate ChatGPT request.
            return {
                "consultation_id": replay_receipt["consultation_id"],
                "request_count": 1,
                "response_text": "Recovered completed NORMAL response marker.\nWORKFLOW_DECISION: CONTINUE\n",
                "receipt_path": str(Path(resume_normal_receipt).expanduser().resolve()),
                "receipt": replay_receipt,
            }
        return invoke_bridge_runner(prompt, **kwargs)

    adapter = StageIntegrationAdapter(
        controller,
        bridge_runner=bridge_runner,
        receipt_root=root / ".research" / "integration",
    )
    adapter.start_stage(actor="step9-harness", rationale="explicit disposable E2E start")

    def initial_action(_response, _request):
        (root / ".research" / "result.txt").write_text(
            "initial bounded fixture result\n", encoding="utf-8"
        )
        (root / ".research" / "metrics_summary.json").write_text(
            json.dumps({"score": 0.8, "revision": 1}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {
            "status": "SUCCEEDED",
            "changed_files": [".research/result.txt", ".research/metrics_summary.json"],
            "evidence": {"score": 0.8, "revision": 1, "result": "initial"},
        }

    adapter.execute_local_action(
        initial_action,
        objective="create the initial bounded fixture result",
        abstraction_layer="fixture-output",
        user_visible_improvement=True,
    )
    normal_baseline = capture_git_baseline(root, stage_id="stage-step9-real")
    normal_pack = pack_spec(adapter, mode="normal", root=root, baseline=normal_baseline)
    assert_low_load_pack_spec(normal_pack, mode="normal")
    normal_prompt = (
        "Review only the current disposable fixture evidence. Confirm the local result is suitable for "
        "one scoped follow-up action. Do not call another tool and do not approve the Stage. "
        "Return one final standalone line exactly: WORKFLOW_DECISION: CONTINUE"
    )
    normal = adapter.consult_gpt(
        normal_prompt,
        mode="NORMAL",
        reason="the first bounded local result needs a technical review",
        context_pack=normal_pack,
        profile_dir=profile_dir,
        timeout_ms=300_000,
    )
    assert_bounded_pack_receipt(normal, mode="NORMAL")
    normal_view = adapter.read_response(normal)
    if normal_view["decision"] != "CONTINUE":
        raise StageIntegrationError(
            "E2E_UNEXPECTED_NORMAL_DECISION",
            f"expected CONTINUE, got {normal_view['decision']}",
        )

    def follow_up_action(response, _request):
        if not response or response["decision"] != "CONTINUE":
            raise RuntimeError("Codex did not read a CONTINUE response")
        (root / ".research" / "result.txt").write_text(
            "GPT-reviewed bounded fixture result\n", encoding="utf-8"
        )
        (root / ".research" / "metrics_summary.json").write_text(
            json.dumps({"score": 0.6, "revision": 2}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return {
            "status": "SUCCEEDED",
            "changed_files": [".research/result.txt", ".research/metrics_summary.json"],
            "evidence": {"score": 0.6, "revision": 2, "result": "gpt-reviewed"},
        }

    adapter.execute_local_action(
        follow_up_action,
        objective="apply the one scoped action after Codex read the GPT response",
        abstraction_layer="fixture-output",
        user_visible_improvement=True,
    )
    registry = ArtifactRegistry(
        "stage-step9-real",
        root=root / ".research",
        required_roles=["metrics_summary"],
    )
    metrics_record = registry.register(root / ".research" / "metrics_summary.json", "metrics_summary")
    artifact_manifest = registry.save(root / ".research" / "artifact_manifest.json")
    adapter.verify_artifacts(artifact_manifest)
    fresh_baseline = capture_git_baseline(root, stage_id="stage-step9-real")
    fresh_pack = pack_spec(adapter, mode="fresh", root=root, baseline=fresh_baseline)
    assert_low_load_pack_spec(fresh_pack, mode="fresh")
    fresh_prompt = (
        "Perform an independent closeout review using only the current evidence in this fresh packet. "
        "Do not rely on any prior recommendation. The checks and final metrics artifact are complete. "
        "Do not approve the Stage; emit one final standalone line exactly: WORKFLOW_DECISION: STAGE_READY"
    )
    fresh = adapter.consult_gpt(
        fresh_prompt,
        mode="FRESH",
        reason="closeout requires an independent fresh review",
        context_pack=fresh_pack,
        profile_dir=profile_dir,
        timeout_ms=300_000,
    )
    assert_bounded_pack_receipt(fresh, mode="FRESH")
    fresh_view = adapter.read_response(fresh)
    if fresh_view["decision"] != "STAGE_READY":
        raise StageIntegrationError(
            "E2E_UNEXPECTED_FRESH_DECISION",
            f"expected STAGE_READY, got {fresh_view['decision']}",
        )
    ready = adapter.mark_stage_ready(
        required_checks={"unit": "PASS", "measurement": "PASS"},
        review_artifacts={"metrics_summary": metrics_record["uri"]},
    )
    if ready["stage"]["status"] != StageState.STAGE_READY.value:
        raise StageIntegrationError("E2E_STAGE_READY_FAILED", "final Stage did not stop at STAGE_READY")
    # The fixture is disposable, but we intentionally leave it in place so
    # the user can inspect the bounded receipts and no-secret result.
    integration_receipt = adapter.receipt_path
    return {
        "marker": INTEGRATION_MARKER,
        "fixture_root": str(root),
        "integration_receipt": str(integration_receipt) if integration_receipt else None,
        "normal_consultation_id": normal.consultation_id,
        "normal_receipt": normal.receipt_path,
        "normal_conversation_id": normal.receipt.get("conversation_id"),
        "normal_request_count": 1,
        "normal_receipt_reused": replay_receipt is not None,
        "fresh_consultation_id": fresh.consultation_id,
        "fresh_receipt": fresh.receipt_path,
        "fresh_conversation_id": fresh.receipt.get("conversation_id"),
        "fresh_request_count": 1,
        "fresh_isolation_verified": fresh_pack.get("mode") == "fresh" and not any(
            key in json.dumps(fresh_pack, ensure_ascii=False)
            for key in ("previousRecommendation", "previous_recommendation", "sunkCostNarrative")
        ),
        "stage_status": ready["stage"]["status"],
        "next_stage_auto_start": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-real", action="store_true", help="perform the two headed ChatGPT requests")
    parser.add_argument("--profile-dir", default=os.environ.get("CHATGPT_PROFILE_DIR"))
    parser.add_argument(
        "--reuse-normal-receipt",
        help="resume after a completed NORMAL receipt whose local capture failed; sends only the FRESH request",
    )
    args = parser.parse_args()
    if not args.run_real:
        print("STEP9_REAL_SMOKE_NOT_RUN use --run-real after login/cooldown", flush=True)
        return 0
    try:
        result = run_real(args.profile_dir, resume_normal_receipt=args.reuse_normal_receipt)
    except StageIntegrationError as error:
        print(
            json.dumps(
                {
                    "marker": INTEGRATION_BLOCKED_MARKER,
                    "failure_class": error.code,
                    "message": str(error),
                    "details": error.details,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    print(INTEGRATION_MARKER, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
