#!/usr/bin/env python3
"""Run the local Step 12 context-compaction/retention acceptance gate.

The gate is intentionally offline.  It creates a temporary project, exercises
the approved-brief gate and ephemeral cleanup boundary, then runs the complete
local unittest suite, schema checks, compile checks, and a high-confidence
secret scan.  It never imports or invokes the bridge, ChatGPT, discovery, or a
worker loop.
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

from src.artifact_retention import cleanup_ephemeral  # noqa: E402
from src.contracts import load_schema, validate_instance  # noqa: E402
from src.project_context import (  # noqa: E402
    PROJECT_CONTEXT_MARKER,
    ProjectContextError,
    build_project_context,
    verify_project_context,
)
from src.project_intake import ProjectRequirementsIntake  # noqa: E402


PASS_MARKER = PROJECT_CONTEXT_MARKER
BLOCKED_MARKER = "STEP12_NOT_READY"
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.I),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


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
    return {
        "passed": completed.returncode == 0,
        "command": command,
        "returncode": completed.returncode,
        "output_tail": output[-1200:].replace("\r", ""),
    }


def _scenario() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="step12-acceptance-") as directory:
        root = Path(directory)
        intake = ProjectRequirementsIntake(root)
        intake.initialize(
            mode="USER_CONFIRMED_BRIEF",
            brief={"goal": "bounded target", "success_criteria": ["user-visible result"]},
        )
        try:
            build_project_context(root)
        except ProjectContextError:
            pass
        else:
            raise RuntimeError("context was generated before explicit brief approval")
        intake.approve(rationale="local acceptance")
        (root / ".research" / "ephemeral").mkdir(parents=True)
        (root / ".research" / "ephemeral" / "scratch.txt").write_text("temporary", encoding="utf-8")
        result = build_project_context(
            root,
            target="bounded target",
            user_visible_success=["user-visible result"],
            selected_route={"route": "primary"},
            next_action="done",
        )
        validate_instance(result, load_schema("project_context"))
        if PROJECT_CONTEXT_MARKER not in result["markdown"]:
            raise RuntimeError("Step 12 marker is missing")
        verification = verify_project_context(root)
        cleanup = cleanup_ephemeral(root, [".research/ephemeral/scratch.txt"])
        if cleanup["removed"] != [".research/ephemeral/scratch.txt"]:
            raise RuntimeError("workflow-owned ephemeral cleanup was not recorded")
        return {
            "passed": True,
            "context_digest": result["context_digest"],
            "resource_count": verification["resource_count"],
            "marker": PROJECT_CONTEXT_MARKER,
            "inventory": sorted(path.name for path in (root / ".research").iterdir() if path.is_file()),
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
    suffixes = {".json", ".md", ".txt", ".yaml", ".yml"}
    for path in ROOT.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or ".pytest_cache" in path.parts:
            continue
        if path.suffix.casefold() not in suffixes:
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
        checks["approved_gate_and_retention"] = _scenario()
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
    keys = ["approved_gate_and_retention", "schema_validation", "no_secret_scan", "unit_regression", "python_compile"]
    passed = "failure" not in checks and all(bool(checks.get(key, {}).get("passed")) for key in keys)
    return (0 if passed else 2), {
        "schema_version": "step12_acceptance.v1",
        "marker": PASS_MARKER if passed else BLOCKED_MARKER,
        "passed": passed,
        "new_real_chatgpt_requests": 0,
        "discovery_started": False,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local Step 12 context-compaction acceptance")
    parser.parse_args(argv)
    code, result = run()
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
