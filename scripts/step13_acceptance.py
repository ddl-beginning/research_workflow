#!/usr/bin/env python3
"""Run the offline Step 13 Project Discovery acceptance gate.

The scenario uses an injected fake consultant and verifier only.  No browser,
ChatGPT transport, network request, or business/source write is permitted by
this script.  The gate also runs every local unit test, schema check, Python
compile check, and a high-confidence secret scan.
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
from src.project_context import build_project_context  # noqa: E402
from src.project_discovery import (  # noqa: E402
    DISCOVERY_MARKER,
    DISCOVERY_REPORT_RELATIVE_PATH,
    discover_project,
)
from src.project_intake import ProjectRequirementsIntake  # noqa: E402


PASS_MARKER = "GPT_PROJECT_DISCOVERY_PASS"
BLOCKED_MARKER = "STEP13_NOT_READY"
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.I),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class _FakeConsult:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "real_goal": "find a bounded reusable route",
            "search_summary": "offline fake consultation",
            "candidate_repositories": [{"repo_url": "https://github.com/example/step13"}],
            "primary_recommendation": "https://github.com/example/step13",
            "why_primary": "the candidate can be checked locally",
            "simpler_alternative": "keep the existing implementation",
            "relevant_non_repo_methods": ["small local experiment"],
            "important_risks": ["fit still requires user review"],
            "facts_needing_local_verification": ["default branch"],
            "no_direct_match_found": False,
            "alternatives": [],
        }


class _FakeVerifier:
    def verify(self, candidate: dict[str, Any], **_: Any) -> dict[str, Any]:
        return {"verified": True, "checked_read_only": True, "fixture": "fake"}


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
    with tempfile.TemporaryDirectory(prefix="step13-acceptance-") as directory:
        root = Path(directory)
        intake = ProjectRequirementsIntake(root)
        intake.initialize(
            mode="USER_CONFIRMED_BRIEF",
            brief={
                "goal": "find a bounded reusable route",
                "success_criteria": ["recommendation is locally checkable"],
                "chatgpt_project_binding": {"url": "https://chatgpt.com/g/g-p-6a9a2d82aec881918b65e066b18b95d8-ce-shi/project"},
            },
        )
        approved = intake.approve(rationale="offline acceptance")
        build_project_context(root, target="find a bounded reusable route", next_action="inspect candidates")
        consultant = _FakeConsult()
        report = discover_project(
            root,
            consultant=consultant,
            verifier=_FakeVerifier(),
            brief=approved,
        )
        validate_instance(report, load_schema("discovery_report"))
        if len(consultant.calls) != 1:
            raise RuntimeError("discovery did not make exactly one fake consultation")
        expected_url = "https://chatgpt.com/g/g-p-6a9a2d82aec881918b65e066b18b95d8-ce-shi/project"
        if consultant.calls[0].get("project_url") != expected_url:
            raise RuntimeError("project_url was not forwarded from approved binding")
        report_path = root / DISCOVERY_REPORT_RELATIVE_PATH
        if not report_path.is_file():
            raise RuntimeError("canonical discovery report was not persisted")
        persistent_inventory = sorted(report.get("persistent_inventory", []))
        if DISCOVERY_REPORT_RELATIVE_PATH.as_posix() not in persistent_inventory:
            raise RuntimeError("canonical report missing from persistent inventory")
        if any("cache" in path.casefold() for path in persistent_inventory):
            raise RuntimeError("temporary verifier cache leaked into project inventory")
        return {
            "passed": True,
            "marker": report.get("marker"),
            "consultation_count": len(consultant.calls),
            "project_url_forwarded": consultant.calls[0]["project_url"],
            "persistent_inventory": persistent_inventory,
            "report_bytes": report_path.stat().st_size,
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
        checks["fake_discovery_scenario"] = _scenario()
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
    keys = ["fake_discovery_scenario", "schema_validation", "no_secret_scan", "unit_regression", "python_compile"]
    passed = "failure" not in checks and all(bool(checks.get(key, {}).get("passed")) for key in keys)
    return (0 if passed else 2), {
        "schema_version": "step13_acceptance.v1",
        "marker": PASS_MARKER if passed else BLOCKED_MARKER,
        "passed": passed,
        "new_real_chatgpt_requests": 0,
        "fake_consult_requests": 1 if checks.get("fake_discovery_scenario", {}).get("passed") else 0,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run local Step 13 Project Discovery acceptance")
    parser.parse_args(argv)
    code, result = run()
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
