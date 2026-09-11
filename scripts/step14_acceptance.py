#!/usr/bin/env python3
"""Run the offline Step 14 project-blueprint acceptance gate.

The scenario uses fake Step 13 consultation and an injected fake blueprint
consultant.  It never invokes the bridge, ChatGPT, a network transport, or a
Stage controller.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.contracts import load_schema, validate_instance  # noqa: E402
from src.project_blueprint import (  # noqa: E402
    BLUEPRINT_MANIFEST_RELATIVE_PATH,
    PROJECT_BLUEPRINT_MARKER,
    PROJECT_BLUEPRINT_RELATIVE_PATH,
    build_project_blueprint,
    load_project_blueprint,
)
from src.project_context import build_project_context  # noqa: E402
from src.project_discovery import discover_project  # noqa: E402
from src.project_intake import ProjectRequirementsIntake  # noqa: E402


PASS_MARKER = "GPT_CODEX_BLUEPRINT_REVIEW_PASS"
BLOCKED_MARKER = "STEP14_NOT_READY"
PROJECT_URL = "https://chatgpt.com/g/g-p-6a9a2d82aec881918b65e066b18b95d8-ce-shi/project"
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.I),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class _DiscoveryConsult:
    def __call__(self, **_: Any) -> dict[str, Any]:
        return {
            "real_goal": "choose a bounded route",
            "search_summary": "offline fake discovery",
            "candidate_repositories": [],
            "primary_recommendation": None,
            "why_primary": "",
            "simpler_alternative": "keep current route",
            "relevant_non_repo_methods": ["small experiment"],
            "important_risks": ["fit requires measurement"],
            "facts_needing_local_verification": [],
            "no_direct_match_found": True,
            "alternatives": [],
        }


class _BlueprintConsult:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "primary_route": {"route_id": "direct-measurement", "title": "direct measurement"},
            "alternatives": [
                {"route_id": "prototype", "title": "small prototype"},
                {"route_id": "existing", "title": "keep existing code"},
            ],
            "composition_decision": "BUILD_NEW",
            "base_decision": "BUILD_NEW",
            "available_assets_used": ["requirement-project-brief"],
            "why_primary": "it is directly measurable",
            "feasibility_summary": "the route is feasible as a bounded experiment",
            "validation_plan": ["run the local check"],
            "important_risks": ["measurement noise"],
            "open_questions": ["which sample is first"],
        }


def _run(command: list[str], *, timeout: int = 180) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"passed": False, "command": command, "error": type(exc).__name__}
    output = (completed.stdout or "") + (completed.stderr or "")
    return {"passed": completed.returncode == 0, "command": command, "returncode": completed.returncode, "output_tail": output[-1200:].replace("\r", "")}


def _scenario() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="step14-acceptance-") as directory:
        root = Path(directory)
        intake = ProjectRequirementsIntake(root)
        intake.initialize(
            mode="USER_CONFIRMED_BRIEF",
            brief={
                "goal": "choose a bounded route",
                "success_criteria": ["route can be checked"],
                "chatgpt_project_url": PROJECT_URL,
            },
        )
        approved = intake.approve(rationale="offline acceptance")
        build_project_context(root, target="choose a bounded route", next_action="discover")
        discover_project(root, consultant=_DiscoveryConsult(), verifier=lambda **_: {"verified": True}, brief=approved)
        consultant = _BlueprintConsult()
        result = build_project_blueprint(root, consultant=consultant, brief=approved)
        validate_instance(result, load_schema("project_blueprint"))
        loaded = load_project_blueprint(root)
        if len(consultant.calls) != 1:
            raise RuntimeError("blueprint did not make exactly one fake consultation")
        if consultant.calls[0].get("project_url") != PROJECT_URL:
            raise RuntimeError("approved Project URL was not forwarded")
        prompt = str(consultant.calls[0].get("prompt", "")).casefold()
        if "primary_recommendation" in prompt or "sunk-cost" in prompt:
            raise RuntimeError("blueprint prompt carried excluded prior-route material")
        if result["status"] != "PROJECT_BLUEPRINT_READY" or result["next_action"] != "READY_FOR_STAGE_PLANNING":
            raise RuntimeError("blueprint is not ready for Stage planning")
        if result["stage_created"] or result["stage_started"]:
            raise RuntimeError("Step 14 created or started a Stage")
        if loaded["marker"] != PROJECT_BLUEPRINT_MARKER:
            raise RuntimeError("blueprint marker missing")
        inventory = sorted(result.get("persistent_inventory", []))
        for expected in (PROJECT_BLUEPRINT_RELATIVE_PATH.as_posix(), BLUEPRINT_MANIFEST_RELATIVE_PATH.as_posix()):
            if expected not in inventory:
                raise RuntimeError(f"persistent inventory missing {expected}")
        return {
            "passed": True,
            "marker": result["marker"],
            "consultation_count": len(consultant.calls),
            "project_url_forwarded": consultant.calls[0]["project_url"],
            "primary_route": result["primary_route"],
            "alternative_count": len(result["alternatives"]),
            "persistent_inventory": inventory,
        }


def _schema_check() -> dict[str, Any]:
    names = sorted(path.name for path in (ROOT / "schemas").glob("*.schema.json"))
    for name in names:
        schema = load_schema(name)
        if schema.get("type") != "object":
            raise RuntimeError(f"schema is not an object schema: {name}")
    return {"passed": True, "schema_count": len(names)}


def _no_secret_check() -> dict[str, Any]:
    scanned = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or ".pytest_cache" in path.parts:
            continue
        if path.suffix.casefold() not in {".json", ".md", ".txt", ".yaml", ".yml"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        scanned += 1
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            raise RuntimeError(f"high-confidence secret pattern found in {path}")
    return {"passed": True, "files_scanned": scanned}


def run() -> tuple[int, dict[str, Any]]:
    checks: dict[str, Any] = {}
    try:
        checks["fake_blueprint_scenario"] = _scenario()
        checks["schema_validation"] = _schema_check()
        checks["no_secret_scan"] = _no_secret_check()
        checks["unit_regression"] = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"])
        checks["python_compile"] = _run(
            [
                sys.executable,
                "-m",
                "py_compile",
                *sorted(str(path.relative_to(ROOT)) for path in ROOT.rglob("*.py") if "__pycache__" not in path.parts),
            ]
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        checks["failure"] = {"class": type(exc).__name__, "message": str(exc)}
    keys = ["fake_blueprint_scenario", "schema_validation", "no_secret_scan", "unit_regression", "python_compile"]
    passed = "failure" not in checks and all(bool(checks.get(key, {}).get("passed")) for key in keys)
    return (0 if passed else 2), {
        "schema_version": "step14_acceptance.v1",
        "marker": PASS_MARKER if passed else BLOCKED_MARKER,
        "passed": passed,
        "new_real_chatgpt_requests": 0,
        "fake_consult_requests": 1 if checks.get("fake_blueprint_scenario", {}).get("passed") else 0,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local Step 14 project-blueprint acceptance")
    parser.parse_args(argv)
    code, result = run()
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
