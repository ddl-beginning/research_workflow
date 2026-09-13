#!/usr/bin/env python3
"""Read-only release documentation and source-surface audit.

This audit intentionally does not import the runtime or write a report.  It
checks the installed checkout's public documentation and the files required
by the documented installation path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _check(name: str, ok: bool, detail: str) -> dict[str, str]:
    return {"name": name, "status": "PASS" if ok else "FAIL", "detail": detail[:512]}


def audit(root: str | Path) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    required_files = {
        "README.md": base / "README.md",
        "INSTALL.md": base / "INSTALL.md",
        "QUICKSTART.md": base / "QUICKSTART.md",
        "TROUBLESHOOTING.md": base / "TROUBLESHOOTING.md",
        "pyproject.toml": base / "pyproject.toml",
        "workflow_mcp.py": base / "scripts" / "workflow_mcp.py",
        "product_doctor.py": base / "scripts" / "product_doctor.py",
        "stage.schema.json": base / "schemas" / "workflow_v2" / "stage.schema.json",
    }
    checks: list[dict[str, str]] = []
    contents: dict[str, str] = {}
    for label, path in required_files.items():
        exists = path.is_file()
        checks.append(_check(f"file.{label}", exists, str(path) if exists else f"missing: {path}"))
        if exists and path.suffix in {".md", ".toml"}:
            contents[label] = path.read_text(encoding="utf-8")

    requirements = {
        "README.md": ("Workflow V2 Product", "Prerequisites", "INSTALL.md", "QUICKSTART.md"),
        "INSTALL.md": ("Prerequisites", "runtime-composition.v2", "codex mcp", "workflow-v2-doctor", "ChatGPT", "Browser Bridge"),
        "QUICKSTART.md": ("workflow_resume", "workflow_start", "workflow_run", "CLOSEOUT.md"),
        "TROUBLESHOOTING.md": (
            "CODEX_RUNTIME_UNAVAILABLE",
            "AUTHENTICATION_REQUIRED",
            "STANDARD_MODEL_UNAVAILABLE",
            "BRIDGE_CONFIGURATION_REQUIRED",
            "MCP_NOT_REGISTERED",
            "WORKFLOW_NOT_FOUND",
            "HUMAN_APPROVAL_REQUIRED",
            "CONSULTATION_EFFECT_UNRESOLVED",
        ),
    }
    for file_name, terms in requirements.items():
        text = contents.get(file_name, "")
        for term in terms:
            checks.append(_check(f"docs.{file_name}.{term}", term in text, f"required term: {term}"))

    ready = all(item["status"] == "PASS" for item in checks)
    return {
        "schema_version": "workflow_v2_product_documentation_audit.v1",
        "root": base.as_posix(),
        "ready": ready,
        "checks": checks,
        "files_read": sorted(contents),
        "writes_performed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Workflow V2 Product documentation audit")
    parser.add_argument("--root", required=True)
    args = parser.parse_args(argv)
    try:
        result = audit(args.root)
    except (OSError, RuntimeError, ValueError) as exc:
        result = {
            "schema_version": "workflow_v2_product_documentation_audit.v1",
            "ready": False,
            "error": {"code": type(exc).__name__, "message": str(exc)[:512]},
            "writes_performed": False,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result.get("ready") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
