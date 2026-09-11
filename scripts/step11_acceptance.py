#!/usr/bin/env python3
"""Run the Step 11 local acceptance gate without any external request.

The gate exercises the rough Chinese requirement, schema/secret hygiene, the
thin CLI, and the complete local regression suite.  It deliberately does not
invoke ``stage_integration``, a browser, ChatGPT, or Project Discovery.
"""

from __future__ import annotations

import argparse
import json
import os
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
from src.project_intake import (  # noqa: E402
    CODEX_REQUIREMENTS_INTERVIEW,
    NEEDS_MORE_USER_INPUT,
    ProjectRequirementsIntake,
    assert_gpt_calls_zero,
    natural_language_entry,
)


PASS_MARKER = "CODEX_NATIVE_REQUIREMENTS_INTAKE_PASS"
BLOCKED_MARKER = "STEP11_NOT_READY"
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.I),
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"passed": False, "command": command, "error": type(exc).__name__}
    output = (completed.stdout or "") + (completed.stderr or "")
    return {
        "passed": completed.returncode == 0,
        "command": command,
        "returncode": completed.returncode,
        "output_tail": output[-800:].replace("\r", ""),
    }


def _scenario_a() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="step11-rough-") as directory:
        root = Path(directory)
        result = natural_language_entry(root, "想做成这个工作流")
        if result["state"] != "DRAFT" or result["status"] != NEEDS_MORE_USER_INPUT:
            raise RuntimeError("rough requirement did not enter DRAFT/NEEDS_MORE_USER_INPUT")
        if result["question_count"] != 1 or len(result["questions"]) != 1:
            raise RuntimeError("rough requirement did not produce exactly one question")
        if result["project_brief"]["intake_mode"] != CODEX_REQUIREMENTS_INTERVIEW:
            raise RuntimeError("rough requirement did not select interview mode")
        files = [path.name for path in (root / ".research").iterdir() if path.is_file()]
        if files != ["PROJECT_BRIEF.json"]:
            raise RuntimeError("intake retained an unexpected file")
        assert_gpt_calls_zero(result["project_brief"])
        return {"passed": True, "state": result["state"], "status": result["status"], "question_count": 1, "gpt_calls": 0}


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
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        if path.suffix.casefold() not in {".json", ".md", ".txt", ".yaml", ".yml"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        scanned += 1
        if any(pattern.search(content) for pattern in SECRET_PATTERNS):
            raise RuntimeError(f"high-confidence secret pattern found in {path.name}")
    return {"passed": True, "files_scanned": scanned}


def _skill_check() -> dict[str, Any]:
    skill = ROOT / "skills" / "codex-native-requirements-intake" / "SKILL.md"
    if not skill.is_file():
        raise RuntimeError("requirements intake skill is missing")
    text = skill.read_text(encoding="utf-8")
    for required in ("USER_CONFIRMED_BRIEF", "CODEX_REQUIREMENTS_INTERVIEW", "DRAFT", "WAITING_USER_APPROVAL", "APPROVED", "CANCELLED", "gpt_calls"):
        if required not in text:
            raise RuntimeError(f"workflow skill is missing {required}")
    return {"passed": True, "path": str(skill.relative_to(ROOT))}


def run() -> tuple[int, dict[str, Any]]:
    checks: dict[str, Any] = {}
    try:
        checks["scenario_a_rough_requirement"] = _scenario_a()
        checks["schema_validation"] = _schema_check()
        checks["no_secret_scan"] = _no_secret_check()
        checks["workflow_skill_validation"] = _skill_check()
        checks["unit_regression"] = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"])
        checks["python_compile"] = _run([sys.executable, "-m", "py_compile", *sorted(str(path.relative_to(ROOT)) for path in ROOT.rglob("*.py") if "__pycache__" not in path.parts)])
        checks["requirements_cli_help"] = _run([sys.executable, "scripts/requirements_cli.py", "--help"])
        checks["stage_cli_help"] = _run([sys.executable, "scripts/stage_cli.py", "--help"])
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        checks["failure"] = {"class": type(exc).__name__, "message": str(exc)}
    keys = ["scenario_a_rough_requirement", "schema_validation", "no_secret_scan", "workflow_skill_validation", "unit_regression", "python_compile", "requirements_cli_help", "stage_cli_help"]
    passed = "failure" not in checks and all(bool(checks.get(key, {}).get("passed")) for key in keys)
    return (0 if passed else 2), {
        "schema_version": "step11_acceptance.v1",
        "marker": PASS_MARKER if passed else BLOCKED_MARKER,
        "passed": passed,
        "new_real_chatgpt_requests": 0,
        "gpt_calls": 0,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local Step 11 requirements-intake acceptance")
    parser.parse_args(argv)
    code, result = run()
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
