#!/usr/bin/env python3
"""Run a real, serial Project-scoped Bootstrap E2E on a disposable fixture.

This harness is intentionally separate from the Stage workflow.  It creates a
new local fixture, runs exactly one FRESH Discovery consultation and exactly
one independent FRESH Blueprint consultation through the existing headed
bridge, then verifies the two receipts and canonical outputs before writing a
small bootstrap state record.  It never starts a Stage or copies prompt/
response bodies into the fixture.

The script is inert unless ``--run-real`` is supplied.  A failed bridge call
is reported with its retained receipt and is never retried by this process.
Run it only after the authenticated bridge profile has cooled down and no
other process owns that profile.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bridge_adapter import ProjectScopedBridgeConsultant  # noqa: E402
from src.contracts import canonical_json, sha256_json  # noqa: E402
from src.project_blueprint import (  # noqa: E402
    PROJECT_BLUEPRINT_MANIFEST_RELATIVE_PATH,
    PROJECT_BLUEPRINT_MARKER,
    PROJECT_BLUEPRINT_RELATIVE_PATH,
    build_project_blueprint,
    load_project_blueprint,
)
from src.project_context import build_project_context, verify_project_context  # noqa: E402
from src.project_discovery import (  # noqa: E402
    DISCOVERY_REPORT_MARKER,
    DISCOVERY_REPORT_RELATIVE_PATH,
    discover_project,
    load_discovery_report,
)
from src.project_intake import ProjectRequirementsIntake  # noqa: E402
from src.stage_integration import subprocess_bridge_runner  # noqa: E402


APPROVED_PROJECT_URL = "https://chatgpt.com/g/g-p-6a9a2d82aec881918b65e066b18b95d8-ce-shi/project"
BRIDGE_ROOT = ROOT.parents[0] / "chatgpt_browser_bridge"
BOOTSTRAP_STATE_RELATIVE_PATH = Path(".research") / "bootstrap_state.json"
BOOTSTRAP_RESEARCH_MARKER = "PROJECT_BOOTSTRAP_RESEARCH_PASS"
BOOTSTRAP_E2E_MARKER = "PROJECT_SCOPED_BOOTSTRAP_E2E_PASS"


class BootstrapE2EError(RuntimeError):
    """A stable local validation error for this one-shot harness."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(message)


def _run_git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise BootstrapE2EError("FIXTURE_GIT_FAILED", "disposable fixture git setup failed")
    return (result.stdout or "").strip()


def _fixture_root() -> Path:
    temp_root = ROOT / ".tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="real-bootstrap-e2e-", dir=str(temp_root)))
    (root / "tests").mkdir()
    (root / "sample.txt").write_text("alpha beta\ngamma delta epsilon\n", encoding="utf-8")
    (root / "word_summary.py").write_text(
        "from pathlib import Path\n\n"
        "def summarize(path: str) -> dict[str, int]:\n"
        "    text = Path(path).read_text(encoding=\"utf-8\")\n"
        "    return {\"lines\": len(text.splitlines()), \"words\": len(text.split()), \"characters\": len(text)}\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_word_summary.py").write_text(
        "from word_summary import summarize\n\n"
        "def test_fixture_summary():\n"
        "    assert summarize(\"sample.txt\") == {\"lines\": 2, \"words\": 5, \"characters\": 31}\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        "Disposable word-summary fixture for a bounded Project Bootstrap review.\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        ".research/\n.consultations/\n.pytest_cache/\n__pycache__/\n",
        encoding="utf-8",
    )
    _run_git(root, "init", "-q")
    _run_git(root, "add", "sample.txt", "word_summary.py", "tests", "README.md", ".gitignore")
    _run_git(
        root,
        "-c",
        "user.name=Bootstrap E2E",
        "-c",
        "user.email=bootstrap-e2e@example.test",
        "commit",
        "-qm",
        "initial disposable fixture",
    )
    return root


def _prepare_project(root: Path) -> dict[str, Any]:
    baseline = {
        "schema_version": "fixture_baseline.v1",
        "fixture": "disposable-word-summary",
        "checked_read_only": True,
        "commit": _run_git(root, "rev-parse", "HEAD"),
        "expected": {"lines": 2, "words": 5, "characters": 31},
    }
    research = root / ".research"
    research.mkdir(parents=True, exist_ok=True)
    (research / "baseline-result.json").write_text(
        json.dumps(baseline, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    intake = ProjectRequirementsIntake(root)
    intake.initialize(
        mode="USER_CONFIRMED_BRIEF",
        brief={
            "title": "Disposable word-summary bootstrap",
            "problem_statement": "A tiny local fixture needs a bounded, reviewable route before any Stage work.",
            "goal": "Choose a bounded, reviewable way to summarize the tiny local fixture without changing the existing user project.",
            "desired_outcome": "A bounded recommendation and blueprint grounded in the local fixture.",
            "success_criteria": [
                "Candidate 0 is explicitly verified from local read-only evidence",
                "the recommendation is bounded and locally checkable",
                "no Stage is created during Discovery or Blueprint",
            ],
            "constraints": [
                "stay inside the disposable repository",
                "do not modify business/source code",
                "use one FRESH Project-scoped consultation per phase",
            ],
            "non_goals": ["creating or starting a Stage", "selecting a production architecture"],
            "preferences": ["prefer simple deterministic checks", "keep evidence metadata-only"],
            "chatgpt_project_binding": {"scope": "project", "url": APPROVED_PROJECT_URL},
            "chatgpt_project_url": APPROVED_PROJECT_URL,
        },
    )
    approved = intake.approve(rationale="explicit disposable Project-scoped Bootstrap E2E")
    build_project_context(
        root,
        target="boundedly summarize the tiny local fixture",
        input_refs=[{"path": "sample.txt", "description": "small deterministic text fixture"}],
        output_refs=[{"path": ".research/baseline-result.json", "description": "bounded baseline metrics"}],
        user_visible_success=["reviewable recommendation", "baseline remains unchanged"],
        constraints=["metadata-only evidence", "no source modification", "no Stage"],
        non_goals=["production architecture", "Stage creation"],
        preferences=["simple deterministic checks"],
        decisions=[{"decision": "keep Candidate 0 explicit", "source": "Codex local audit"}],
        next_action="DISCOVER_PROJECT",
    )
    verify_project_context(root)
    return approved


def _consultant(root: Path, profile_dir: str | None, timeout_ms: int) -> ProjectScopedBridgeConsultant:
    return ProjectScopedBridgeConsultant(
        root,
        bridge_runner=subprocess_bridge_runner,
        profile_dir=profile_dir,
        bridge_root=BRIDGE_ROOT,
        timeout_ms=timeout_ms,
    )


def _load_receipt(root: Path, path_value: Any) -> tuple[Path, dict[str, Any]]:
    if not isinstance(path_value, str) or not path_value.strip():
        raise BootstrapE2EError("RECEIPT_PATH_MISSING", "successful consultation did not expose a receipt path")
    path = Path(path_value).expanduser().resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise BootstrapE2EError("RECEIPT_PATH_OUTSIDE_ROOT", "consultation receipt escaped the fixture") from exc
    if path.name != "receipt.json" or not path.is_file() or path.is_symlink():
        raise BootstrapE2EError("RECEIPT_INVALID", "consultation receipt is missing or not a regular file")
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapE2EError("RECEIPT_INVALID", "consultation receipt is not valid JSON") from exc
    if not isinstance(receipt, dict):
        raise BootstrapE2EError("RECEIPT_INVALID", "consultation receipt must be an object")
    return path, receipt


def _verify_receipt(root: Path, path_value: Any, *, expected_mode: str) -> tuple[Path, dict[str, Any]]:
    path, receipt = _load_receipt(root, path_value)
    if receipt.get("status") != "complete" or receipt.get("request_count") != 1:
        raise BootstrapE2EError("RECEIPT_NOT_COMPLETE", "consultation receipt is not a complete one-request call")
    if receipt.get("mode") != expected_mode or receipt.get("project_url") != APPROVED_PROJECT_URL:
        raise BootstrapE2EError("RECEIPT_SCOPE_MISMATCH", "consultation receipt has the wrong Project or mode")
    if receipt.get("project_scope_requested") is not True or receipt.get("project_scope_verified") is not True:
        raise BootstrapE2EError("PROJECT_SCOPE_UNVERIFIED", "consultation receipt does not prove Project scope")
    if receipt.get("conversation_validated") is not True:
        raise BootstrapE2EError("CONVERSATION_UNVERIFIED", "consultation receipt does not prove conversation identity")
    conversation_id = receipt.get("conversation_id")
    consultation_id = receipt.get("consultation_id")
    if not isinstance(conversation_id, str) or not conversation_id.strip() or not isinstance(consultation_id, str) or not consultation_id.strip():
        raise BootstrapE2EError("CONVERSATION_ID_MISSING", "consultation receipt lacks a validated identity")
    return path, receipt


def _file_digest(root: Path, relative: Path) -> str:
    path = root / relative
    if path.is_symlink() or not path.is_file():
        raise BootstrapE2EError("CANONICAL_ARTIFACT_MISSING", f"canonical artifact is missing: {relative.as_posix()}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_receipt_view(receipt: Mapping[str, Any], *, relative_path: str) -> dict[str, Any]:
    context_pack = receipt.get("context_pack")
    pack_view: dict[str, Any] = {}
    if isinstance(context_pack, Mapping):
        for key in ("packet_id", "mode", "attachment_count", "pack_sha256", "manifest_sha256", "relative_manifest_path"):
            value = context_pack.get(key)
            if isinstance(value, (str, int)):
                pack_view[key] = value
    return {
        "consultation_id": receipt.get("consultation_id"),
        "conversation_id": receipt.get("conversation_id"),
        "mode": receipt.get("mode"),
        "request_count": receipt.get("request_count"),
        "status": receipt.get("status"),
        "project_url": receipt.get("project_url"),
        "project_scope_requested": receipt.get("project_scope_requested"),
        "project_scope_verified": receipt.get("project_scope_verified"),
        "conversation_validated": receipt.get("conversation_validated"),
        "relative_receipt_path": relative_path,
        "context_pack": pack_view,
    }


def _write_bootstrap_state(
    root: Path,
    *,
    approved: Mapping[str, Any],
    discovery_receipt_path: Path,
    discovery_receipt: Mapping[str, Any],
    blueprint_receipt_path: Path,
    blueprint_receipt: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    discovery_relative_receipt = discovery_receipt_path.relative_to(root).as_posix()
    blueprint_relative_receipt = blueprint_receipt_path.relative_to(root).as_posix()
    discovery_report_digest = _file_digest(root, DISCOVERY_REPORT_RELATIVE_PATH)
    blueprint_digest = _file_digest(root, PROJECT_BLUEPRINT_RELATIVE_PATH)
    blueprint_manifest_digest = _file_digest(root, PROJECT_BLUEPRINT_MANIFEST_RELATIVE_PATH)
    retention_relative = Path(".research") / "ARTIFACT_RETENTION_MANIFEST.json"
    retention_digest = _file_digest(root, retention_relative)
    state: dict[str, Any] = {
        "schema_version": "bootstrap_state.v1",
        "status": "BOOTSTRAP_COMPLETE",
        "project_id": str(approved.get("project_id", "")),
        "project_url": APPROVED_PROJECT_URL,
        "project_scope_verified": True,
        "discovery": {
            "marker": DISCOVERY_REPORT_MARKER,
            "status": "DISCOVERY_COMPLETE",
            "receipt": _safe_receipt_view(discovery_receipt, relative_path=discovery_relative_receipt),
            "report_path": DISCOVERY_REPORT_RELATIVE_PATH.as_posix(),
            "report_digest": discovery_report_digest,
        },
        "blueprint": {
            "marker": PROJECT_BLUEPRINT_MARKER,
            "status": "PROJECT_BLUEPRINT_READY",
            "next_action": "READY_FOR_STAGE_PLANNING",
            "receipt": _safe_receipt_view(blueprint_receipt, relative_path=blueprint_relative_receipt),
            "blueprint_path": PROJECT_BLUEPRINT_RELATIVE_PATH.as_posix(),
            "blueprint_digest": blueprint_digest,
            "manifest_path": PROJECT_BLUEPRINT_MANIFEST_RELATIVE_PATH.as_posix(),
            "manifest_digest": blueprint_manifest_digest,
        },
        "conversation_ids_distinct": discovery_receipt.get("conversation_id") != blueprint_receipt.get("conversation_id"),
        "request_counts": {"discovery": discovery_receipt.get("request_count"), "blueprint": blueprint_receipt.get("request_count")},
        "retention_manifest_path": retention_relative.as_posix(),
        "retention_manifest_digest": retention_digest,
        "stage_created": False,
        "stage_started": False,
        "markers": [BOOTSTRAP_RESEARCH_MARKER, BOOTSTRAP_E2E_MARKER],
        "next_action": "READY_FOR_STAGE_PLANNING",
    }
    if not state["conversation_ids_distinct"]:
        raise BootstrapE2EError("CONVERSATION_REUSED", "Discovery and Blueprint reused the same conversation")
    state["state_digest"] = sha256_json(state)
    path = root / BOOTSTRAP_STATE_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise BootstrapE2EError("BOOTSTRAP_STATE_WRITE_FAILED", "bootstrap state could not be written") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return path, state


def run_real(*, profile_dir: str | None, timeout_ms: int) -> dict[str, Any]:
    root = _fixture_root()
    approved = _prepare_project(root)
    discovery_consultant = _consultant(root, profile_dir, timeout_ms)
    discovery = discover_project(root, consultant=discovery_consultant, brief=approved)
    discovery_receipt_path, discovery_receipt = _verify_receipt(
        root,
        discovery.get("bridge_receipt", {}).get("receipt_path"),
        expected_mode="fresh",
    )
    if discovery.get("marker") != DISCOVERY_REPORT_MARKER or discovery.get("status") != "DISCOVERY_COMPLETE":
        raise BootstrapE2EError("DISCOVERY_CANONICAL_INVALID", "Discovery canonical report is not complete")
    load_discovery_report(root)
    # A separate adapter instance makes the boundary explicit.  Each call is
    # still one FRESH request and cannot continue from Discovery.
    blueprint_consultant = _consultant(root, profile_dir, timeout_ms)
    blueprint = build_project_blueprint(root, consultant=blueprint_consultant, brief=approved)
    blueprint_receipt_path, blueprint_receipt = _verify_receipt(
        root,
        blueprint.get("bridge_receipt", {}).get("receipt_path"),
        expected_mode="fresh",
    )
    if blueprint.get("marker") != PROJECT_BLUEPRINT_MARKER or blueprint.get("status") != "PROJECT_BLUEPRINT_READY" or blueprint.get("next_action") != "READY_FOR_STAGE_PLANNING":
        raise BootstrapE2EError("BLUEPRINT_CANONICAL_INVALID", "Blueprint canonical output is not ready")
    loaded_blueprint = load_project_blueprint(root)
    if loaded_blueprint.get("project_id") != approved.get("project_id"):
        raise BootstrapE2EError("BLUEPRINT_PROJECT_MISMATCH", "Blueprint is not bound to the approved project")
    if discovery_receipt.get("conversation_id") == blueprint_receipt.get("conversation_id"):
        raise BootstrapE2EError("CONVERSATION_REUSED", "Discovery and Blueprint reused the same conversation")
    state_path, state = _write_bootstrap_state(
        root,
        approved=approved,
        discovery_receipt_path=discovery_receipt_path,
        discovery_receipt=discovery_receipt,
        blueprint_receipt_path=blueprint_receipt_path,
        blueprint_receipt=blueprint_receipt,
    )
    return {
        "marker": BOOTSTRAP_E2E_MARKER,
        "research_marker": BOOTSTRAP_RESEARCH_MARKER,
        "fixture_root": str(root),
        "bootstrap_state": str(state_path),
        "discovery_receipt": str(discovery_receipt_path),
        "discovery_consultation_id": discovery_receipt["consultation_id"],
        "discovery_conversation_id": discovery_receipt["conversation_id"],
        "discovery_request_count": discovery_receipt["request_count"],
        "blueprint_receipt": str(blueprint_receipt_path),
        "blueprint_consultation_id": blueprint_receipt["consultation_id"],
        "blueprint_conversation_id": blueprint_receipt["conversation_id"],
        "blueprint_request_count": blueprint_receipt["request_count"],
        "project_url": APPROVED_PROJECT_URL,
        "project_scope_verified": state["project_scope_verified"],
        "conversation_ids_distinct": state["conversation_ids_distinct"],
        "stage_created": False,
        "stage_started": False,
        "canonical_artifacts": [
            DISCOVERY_REPORT_RELATIVE_PATH.as_posix(),
            PROJECT_BLUEPRINT_RELATIVE_PATH.as_posix(),
            PROJECT_BLUEPRINT_MANIFEST_RELATIVE_PATH.as_posix(),
            BOOTSTRAP_STATE_RELATIVE_PATH.as_posix(),
        ],
        "state_digest": state["state_digest"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run serial real Project-scoped Bootstrap E2E")
    parser.add_argument("--run-real", action="store_true", help="perform exactly one Discovery and one Blueprint request")
    parser.add_argument("--profile-dir", default=os.environ.get("CHATGPT_PROFILE_DIR"))
    parser.add_argument("--timeout-ms", type=int, default=300_000)
    args = parser.parse_args(argv)
    if not args.run_real:
        print("BOOTSTRAP_REAL_E2E_NOT_RUN use --run-real after login/cooldown", flush=True)
        return 0
    try:
        result = run_real(profile_dir=args.profile_dir, timeout_ms=args.timeout_ms)
    except BootstrapE2EError as exc:
        print(
            json.dumps(
                {"marker": "PROJECT_SCOPED_BOOTSTRAP_E2E_BLOCKED", "failure_code": exc.code, "details": exc.details},
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        return 2
    except Exception as exc:  # pragma: no cover - transport/runtime-specific
        print(
            json.dumps(
                {"marker": "PROJECT_SCOPED_BOOTSTRAP_E2E_BLOCKED", "failure_code": type(exc).__name__},
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2), flush=True)
    print(BOOTSTRAP_RESEARCH_MARKER, flush=True)
    print(BOOTSTRAP_E2E_MARKER, flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
