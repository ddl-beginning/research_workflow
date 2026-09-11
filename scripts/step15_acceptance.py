#!/usr/bin/env python3
"""Run the offline Step 15 Stage-planning acceptance gate.

The default scenario builds a disposable, fully canonical Step 11--14
Bootstrap hand-off with fake consultants, then runs the real Step 15 planner.
Passing ``--repo`` instead consumes that repository's existing canonical
Bootstrap outputs; it never manufactures missing evidence and never calls a
bridge.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.contracts import load_schema, validate_instance  # noqa: E402
from src.project_blueprint import build_project_blueprint  # noqa: E402
from src.project_context import build_project_context  # noqa: E402
from src.project_discovery import discover_project  # noqa: E402
from src.project_intake import ProjectRequirementsIntake  # noqa: E402
from src.stage_controller import StageController, StageState, validate_stage_contract_v1  # noqa: E402
from src.stage_planning import (  # noqa: E402
    BOOTSTRAP_STATE_RELATIVE_PATH,
    BOOTSTRAP_STATE_MARKER,
    BOOTSTRAP_STATE_NEXT_ACTION,
    BOOTSTRAP_STATE_STATUS,
    BLUEPRINT_STATE_PATH,
    DISCOVERY_STATE_PATH,
    STAGE_PLAN_RELATIVE_PATH,
    STAGE_PLANNING_MARKER,
    STAGE_STATE_RELATIVE_PATH,
    bootstrap_state_digest,
    plan_stage,
    verify_stage_plan,
)


PASS_MARKER = STAGE_PLANNING_MARKER
BLOCKED_MARKER = "STEP15_NOT_READY"
PROJECT_URL = "https://chatgpt.com/g/g-p-6a9a2d82aec881918b65e066b18b95d8-ce-shi/project"


class _DiscoveryConsult:
    def __call__(self, **_: Any) -> dict[str, Any]:
        return {
            "real_goal": "choose a bounded route",
            "search_summary": "offline fake discovery",
            "candidate_repositories": [],
            "primary_recommendation": None,
            "why_primary": "the local candidate is sufficient for a bounded review",
            "simpler_alternative": "keep the current route",
            "relevant_non_repo_methods": ["small deterministic check"],
            "important_risks": ["measurement noise"],
            "facts_needing_local_verification": [],
            "no_direct_match_found": True,
            "alternatives": [],
        }


class _BlueprintConsult:
    def __call__(self, **_: Any) -> dict[str, Any]:
        return {
            "primary_route": {
                "route_id": "direct-measurement",
                "title": "direct measurement",
                "summary": "verify the existing deterministic fixture against its retained baseline",
                "steps": ["run the local test", "compare the bounded output with baseline metadata"],
            },
            "alternatives": [{"route_id": "documentation-only", "title": "documentation-only review"}],
            "composition_decision": "KEEP_EXISTING",
            "base_decision": "KEEP_EXISTING",
            "available_assets_used": ["requirement-project-brief"],
            "why_primary": "the existing candidate is already locally verifiable",
            "feasibility_summary": "the route is feasible as one bounded Stage",
            "validation_plan": ["run the local fixture check", "inspect the before/after evidence"],
            "important_risks": ["the retained baseline may drift"],
            "open_questions": [],
        }


def _bootstrap_fixture(root: Path) -> dict[str, Any]:
    intake = ProjectRequirementsIntake(root)
    intake.initialize(
        mode="USER_CONFIRMED_BRIEF",
        brief={
            "title": "Step 15 fixture",
            "goal": "validate a bounded deterministic route",
            "desired_outcome": "a reviewable local result",
            "success_criteria": ["the local check is reproducible"],
            "chatgpt_project_url": PROJECT_URL,
        },
    )
    intake.approve(actor="offline-acceptance", rationale="fixture approval")
    brief = intake.state
    if not isinstance(brief, dict):
        raise RuntimeError("offline fixture did not persist an approved brief")
    build_project_context(
        root,
        target="validate a bounded deterministic route",
        inputs=["sample.txt"],
        outputs=["bounded result"],
        user_visible_success=["a reviewable local result"],
        constraints=["do not modify business/source files"],
        next_action="discover",
    )
    discovery = discover_project(
        root,
        consultant=_DiscoveryConsult(),
        verifier=lambda **_: {"verified": True},
        brief=brief,
    )
    blueprint = build_project_blueprint(root, consultant=_BlueprintConsult(), brief=brief)
    state = {
        "schema_version": "bootstrap_state.v1",
        "status": BOOTSTRAP_STATE_STATUS,
        "markers": [BOOTSTRAP_STATE_MARKER, "BOOTSTRAP_E2E_PASS"],
        "next_action": BOOTSTRAP_STATE_NEXT_ACTION,
        "project_scope_verified": True,
        "project_id": brief["project_id"],
        "discovery": {
            "marker": "GPT_PROJECT_DISCOVERY_PASS",
            "status": "DISCOVERY_COMPLETE",
            "report_path": DISCOVERY_STATE_PATH,
            "report_digest": hashlib.sha256(
                (root / DISCOVERY_STATE_PATH).read_bytes()
            ).hexdigest(),
        },
        "blueprint": {
            "marker": "GPT_CODEX_BLUEPRINT_REVIEW_PASS",
            "status": "PROJECT_BLUEPRINT_READY",
            "blueprint_path": BLUEPRINT_STATE_PATH,
            "blueprint_digest": hashlib.sha256(
                (root / BLUEPRINT_STATE_PATH).read_bytes()
            ).hexdigest(),
            "manifest_path": ".research/blueprint/BLUEPRINT_MANIFEST.json",
        },
        "stage_created": False,
        "stage_started": False,
    }
    state["state_digest"] = bootstrap_state_digest(state)
    state_path = root / BOOTSTRAP_STATE_RELATIVE_PATH
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return state


def _scenario(root: Path | None = None) -> dict[str, Any]:
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if root is None:
        temporary = tempfile.TemporaryDirectory(prefix="step15-acceptance-")
        root = Path(temporary.name)
    try:
        if temporary is not None:
            bootstrap = _bootstrap_fixture(root)
        else:
            if not (root / ".research" / "PROJECT_BRIEF.json").is_file():
                raise RuntimeError("--repo must already contain the canonical Bootstrap outputs")
            bootstrap = json.loads((root / BOOTSTRAP_STATE_RELATIVE_PATH).read_text(encoding="utf-8"))
        result = plan_stage(root)
        validate_instance(result, load_schema("stage_planning"))
        contract_path = root / result["stage_contract_path"]
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        validate_stage_contract_v1(contract)
        state_path = root / STAGE_STATE_RELATIVE_PATH
        controller = StageController.from_state(state_path)
        stage = controller.show_stage(result["stage_id"])
        if stage["status"] != StageState.PLANNED.value:
            raise RuntimeError("Step 15 did not leave the Stage PLANNED")
        if controller.state.get("active_stage_id") is not None:
            raise RuntimeError("Step 15 unexpectedly activated a Stage")
        verification = verify_stage_plan(root)
        second = plan_stage(root)
        if not second.get("idempotent_reuse"):
            raise RuntimeError("unchanged Step 15 inputs were not reused")
        return {
            "passed": True,
            "marker": result["marker"],
            "project_id": result["project_id"],
            "stage_id": result["stage_id"],
            "stage_status": stage["status"],
            "stage_started": result["stage_started"],
            "stage_plan_path": STAGE_PLAN_RELATIVE_PATH.as_posix(),
            "stage_contract_path": result["stage_contract_path"],
            "stage_state_path": result["stage_state_path"],
            "baseline_digest": result["baseline_digest"],
            "contract_digest": result["contract_digest"],
            "bootstrap_marker": bootstrap.get("marker") or (
                BOOTSTRAP_STATE_MARKER if BOOTSTRAP_STATE_MARKER in bootstrap.get("markers", []) else None
            ),
            "verification": verification,
            "idempotent_reuse": second["idempotent_reuse"],
        }
    finally:
        if temporary is not None:
            temporary.cleanup()


def _run(command: list[str], *, cwd: Path = ROOT, timeout: int = 180) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"passed": False, "command": command, "error": type(exc).__name__}
    combined = (completed.stdout or "") + (completed.stderr or "")
    return {
        "passed": completed.returncode == 0,
        "command": command,
        "returncode": completed.returncode,
        "output_tail": combined[-1600:].replace("\r", ""),
    }


def run(repo: str | None = None) -> tuple[int, dict[str, Any]]:
    checks: dict[str, Any] = {}
    try:
        checks["stage_planning_scenario"] = _scenario(Path(repo).expanduser().resolve() if repo else None)
        checks["schema_validation"] = {
            "passed": all(
                load_schema(name).get("type") == "object"
                for name in ("bootstrap_state.v1", "stage_planning", "stage_planning.v1")
            ),
            "schema_names": [
                "bootstrap_state.v1.schema.json",
                "stage_planning.schema.json",
                "stage_planning.v1.schema.json",
            ],
        }
        checks["unit_regression"] = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"])
        checks["python_compile"] = _run(
            [
                sys.executable,
                "-m",
                "py_compile",
                "src/stage_planning.py",
                "scripts/stage_cli.py",
                "scripts/step15_acceptance.py",
            ]
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        checks["failure"] = {"class": type(exc).__name__, "message": str(exc)}
    passed = "failure" not in checks and all(bool(checks.get(key, {}).get("passed")) for key in checks if key != "failure")
    return (0 if passed else 2), {
        "schema_version": "step15_acceptance.v1",
        "marker": PASS_MARKER if passed else BLOCKED_MARKER,
        "passed": passed,
        "new_real_chatgpt_requests": 0,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local Step 15 Stage-planning acceptance")
    parser.add_argument("--repo", help="consume an existing Bootstrap repository instead of a temporary fixture")
    args = parser.parse_args(argv)
    code, result = run(args.repo)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
