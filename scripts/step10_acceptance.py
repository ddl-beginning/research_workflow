#!/usr/bin/env python3
"""Run the Step 10 local regression and summarize all workflow markers.

This script deliberately reuses the sanitized Step 9 integration receipt.  It
does not invoke a browser, ChatGPT, MCP connector, or any new external request.
The prior Step 1-8 markers are prerequisites supplied by their already-passed
Gates; this script verifies the Step 9 receipt plus the complete local
regression needed for the final productization marker.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

try:
    from stage_cli import SECRET_VALUE_PATTERNS, _assert_no_secrets
except ModuleNotFoundError:  # ``python -m scripts.step10_acceptance``
    from scripts.stage_cli import SECRET_VALUE_PATTERNS, _assert_no_secrets


ROOT = Path(__file__).resolve().parents[1]
BRIDGE_ROOT = ROOT.parent / "chatgpt_browser_bridge"
HISTORICAL_MARKERS = (
    "CODEX_MCP_GPT_ROUNDTRIP_PASS",
    "CODEX_MCP_GPT_DIALOGUE_PASS",
    "CODEX_MCP_GPT_ATTACHMENT_PASS",
    "CODEX_GPT_CONTEXT_PACK_PASS",
    "CODEX_GPT_DIALOGUE_POLICY_PASS",
    "STAGE_CONTROLLER_CORE_PASS",
    "STAGE_CONTEXT_AND_GIT_PASS",
)
STEP9_MARKER = "CODEX_GPT_STAGE_INTEGRATION_PASS"
FINAL_MARKER = "CODEX_GPT_STAGE_WORKFLOW_PASS"
BLOCKED_MARKER = "STEP10_NOT_READY"


def _find_default_receipt() -> Path | None:
    candidates = list(Path("D:/Temp").glob("step9-disposable-*/.research/integration/stage-integration-*.json"))
    if not candidates:
        candidates = list(Path("D:/Temp").glob("step9-*/.research/integration/stage-integration-*.json"))
    return max(candidates, key=lambda item: item.stat().st_mtime) if candidates else None


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read receipt: {path.name}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("receipt must contain an object")
    _assert_no_secrets(value, path="step9_receipt")
    return value


def _verify_receipt(path: Path) -> dict[str, Any]:
    payload = _load(path)
    if payload.get("marker") != STEP9_MARKER:
        raise RuntimeError("Step 9 receipt does not carry the required PASS marker")
    if payload.get("status") != "STAGE_READY" or payload.get("failure_code") is not None:
        raise RuntimeError("Step 9 receipt is not a clean STAGE_READY result")
    consultations = payload.get("consultations")
    if not isinstance(consultations, list) or not consultations:
        raise RuntimeError("Step 9 receipt has no consultation evidence")
    request_counts: list[int] = []
    packet_ids: list[str] = []
    attachment_counts: list[int] = []
    modes: list[str] = []
    material_root = path.parents[2] if len(path.parents) >= 3 else path.parent
    for item in consultations:
        if not isinstance(item, Mapping) or item.get("status") != "complete" or item.get("request_count") != 1:
            raise RuntimeError("Step 9 consultation evidence violates request_count=1")
        request_counts.append(1)
        context_pack = item.get("context_pack")
        if not isinstance(context_pack, Mapping):
            raise RuntimeError("Step 9 consultation is missing context-pack provenance")
        count = context_pack.get("attachment_count")
        packet_id = context_pack.get("packet_id")
        if not isinstance(count, int) or count < 1 or count > 9:
            raise RuntimeError("Step 9 context pack is outside the attachment bound")
        if not isinstance(packet_id, str) or not re.fullmatch(r"PACK-[A-Za-z0-9][A-Za-z0-9._-]{0,127}", packet_id):
            raise RuntimeError("Step 9 context pack id is invalid")
        packet_ids.append(packet_id)
        attachment_counts.append(count)
        pack_mode = context_pack.get("mode")
        if pack_mode not in {"normal", "fresh"}:
            raise RuntimeError("Step 9 context pack mode is invalid")
        modes.append(str(pack_mode))
        relative_manifest = context_pack.get("relative_manifest_path")
        manifest_sha = context_pack.get("manifest_sha256")
        if not isinstance(relative_manifest, str) or not isinstance(manifest_sha, str):
            raise RuntimeError("Step 9 context pack is missing manifest provenance")
        # Bridge receipt paths are relative to its ``.consultations`` root,
        # not to the disposable repository root.
        manifest_path = (material_root / ".consultations" / Path(relative_manifest)).resolve()
        try:
            manifest_path.relative_to(material_root.resolve())
        except ValueError as exc:
            raise RuntimeError("Step 9 manifest path escaped the disposable repository") from exc
        if not manifest_path.is_file() or hashlib.sha256(manifest_path.read_bytes()).hexdigest() != manifest_sha:
            raise RuntimeError("Step 9 context manifest hash does not match the receipt")
        manifest = _load(manifest_path)
        if manifest.get("packet_id") != packet_id or manifest.get("attachment_count") != count:
            raise RuntimeError("Step 9 context manifest disagrees with the receipt")
        if manifest.get("pack_sha256") != context_pack.get("pack_sha256"):
            raise RuntimeError("Step 9 context pack digest disagrees with the receipt")
    if "normal" not in modes or "fresh" not in modes:
        raise RuntimeError("Step 9 receipt must contain both NORMAL and FRESH consultation evidence")
    # A retained request/response file would mean the bridge hygiene contract
    # was not met.  Sanitized receipts and review summaries are allowed.
    forbidden = [candidate for candidate in material_root.rglob("*") if candidate.is_file() and candidate.name in {"request.txt", "response.txt"}]
    if forbidden:
        raise RuntimeError("Step 9 disposable evidence retained a transient prompt/response file")
    return {
        "path": str(path),
        "stage_id": payload.get("stage_id"),
        "status": payload.get("status"),
        "consultation_count": len(consultations),
        "request_counts": request_counts,
        "modes": modes,
        "packet_ids": packet_ids,
        "attachment_counts": attachment_counts,
    }


def _run(command: list[str], cwd: Path, *, timeout: int = 180) -> dict[str, Any]:
    try:
        result = subprocess.run(command, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "passed": False, "error": type(exc).__name__}
    output = (result.stdout or "") + (result.stderr or "")
    test_count = None
    match = re.search(r"Ran (\d+) tests?", output) or re.search(r"[#ℹ]\s*tests\s+(\d+)", output)
    if match:
        test_count = int(match.group(1))
    return {
        "command": command,
        "passed": result.returncode == 0,
        "returncode": result.returncode,
        "test_count": test_count,
        "output_tail": output[-500:].replace("\r", ""),
    }


def _schema_check() -> dict[str, Any]:
    # Import only after ROOT has been established; the script is executable
    # both from the repository and through an absolute path.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from src.contracts import load_schema  # pylint: disable=import-outside-toplevel

    names = sorted(path.name for path in (ROOT / "schemas").glob("*.schema.json"))
    for name in names:
        schema = load_schema(name)
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise RuntimeError(f"schema is not an object schema: {name}")
    return {"passed": True, "schema_count": len(names)}


def _no_secret_check(receipt_path: Path) -> dict[str, Any]:
    root = receipt_path.parents[2] if len(receipt_path.parents) >= 3 else receipt_path.parent
    scanned = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.name in {"request.txt", "response.txt"}:
            continue
        if path.suffix.lower() not in {".json", ".md", ".txt", ".yaml", ".yml"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        scanned += 1
        for pattern in SECRET_VALUE_PATTERNS:
            if pattern.search(text):
                raise RuntimeError(f"high-confidence secret pattern found in {path.name}")
    return {"passed": True, "files_scanned": scanned}


def run(receipt_path: Path) -> tuple[int, dict[str, Any]]:
    checks: dict[str, Any] = {}
    try:
        checks["step9_receipt"] = _verify_receipt(receipt_path)
        npm = "npm.cmd" if os.name == "nt" else "npm"
        checks["bridge_check"] = _run([npm, "run", "check"], BRIDGE_ROOT)
        checks["bridge_tests"] = _run([npm, "test"], BRIDGE_ROOT)
        checks["supervisor_tests"] = _run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"], ROOT)
        python_files = sorted(
            str(path.relative_to(ROOT))
            for path in ROOT.rglob("*.py")
            if "__pycache__" not in path.parts
        )
        checks["static_python"] = _run([sys.executable, "-m", "py_compile", *python_files], ROOT)
        checks["cli_help"] = _run([sys.executable, "scripts/stage_cli.py", "--help"], ROOT)
        checks["schema_validation"] = _schema_check()
        checks["no_secret_scan"] = _no_secret_check(receipt_path)
    except RuntimeError as exc:
        checks["failure"] = {"class": type(exc).__name__, "message": str(exc)}
    local_pass = all(
        bool(item.get("passed"))
        for key, item in checks.items()
        if key in {"bridge_check", "bridge_tests", "supervisor_tests", "static_python", "cli_help", "schema_validation", "no_secret_scan"}
    )
    receipt_pass = "step9_receipt" in checks and "failure" not in checks
    passed = receipt_pass and local_pass
    result = {
        "schema_version": "step10_acceptance.v1",
        "marker": FINAL_MARKER if passed else BLOCKED_MARKER,
        "passed": passed,
        "new_real_chatgpt_requests": 0,
        "historical_prerequisite_markers": {marker: "accepted prerequisite from completed Gate" for marker in HISTORICAL_MARKERS},
        "step9_marker": STEP9_MARKER if receipt_pass else None,
        "checks": checks,
    }
    return (0 if passed else 2), result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Step 10 local regression without new ChatGPT requests")
    parser.add_argument("--step9-receipt", help="sanitized Step 9 integration receipt; default: newest D:/Temp/step9-* receipt")
    args = parser.parse_args(argv)
    selected = Path(args.step9_receipt).expanduser().resolve() if args.step9_receipt else _find_default_receipt()
    if selected is None or not selected.is_file():
        print(json.dumps({"schema_version": "step10_acceptance.v1", "marker": BLOCKED_MARKER, "passed": False, "failure": "Step 9 receipt is required", "new_real_chatgpt_requests": 0}, ensure_ascii=True, indent=2))
        return 2
    code, result = run(selected)
    # Never print captured command output beyond the bounded tail included by
    # run(); no prompt, response, or browser state is handled here.
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
