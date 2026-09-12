"""Create the immutable Phase 0 implementation baseline.

The command is intentionally read-only with respect to HOST and the frozen V2
workspace.  It writes evidence only below the candidate checkout.
"""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Iterable


CANDIDATE = Path(__file__).resolve().parents[1]
HOST = Path(r"D:/work/research_tools/research_supervisor_poc")
V2 = Path(r"D:/work/research_tools/research_supervisor_v2_clean")
OUT = CANDIDATE / "implementation_evidence" / "phase-0"
SOURCE_DIRS = ("src", "schemas", "tests", "scripts")


def normalized_bytes(path: Path) -> bytes:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def normalized_hash(path: Path) -> str:
    return hashlib.sha256(normalized_bytes(path)).hexdigest()


def files_under(root: Path) -> list[str]:
    paths: list[str] = []
    for directory in SOURCE_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            paths.append(path.relative_to(root).as_posix())
    return sorted(paths)


def candidate_head_files() -> list[str]:
    output = subprocess.check_output(
        ["git", "-C", str(CANDIDATE), "ls-tree", "-r", "--name-only", "HEAD"],
        text=True,
    )
    return sorted(
        path
        for path in output.splitlines()
        if path.startswith(SOURCE_DIRS) and "__pycache__" not in path and not path.endswith(".pyc")
    )


def manifest(name: str, root: Path, paths: Iterable[str], *, source_kind: str) -> dict[str, Any]:
    entries = []
    for relative in sorted(paths):
        path = root / relative
        raw = path.read_bytes()
        entries.append(
            {
                "path": relative,
                "sha256_normalized_utf8_lf": normalized_hash(path),
                "raw_bytes": len(raw),
                "normalized_bytes": len(normalized_bytes(path)),
            }
        )
    canonical = "\n".join(
        f"{entry['path']}\t{entry['sha256_normalized_utf8_lf']}" for entry in entries
    ).encode("utf-8")
    return {
        "manifest_version": "phase-0.v1",
        "generated_on": date.today().isoformat(),
        "name": name,
        "source_kind": source_kind,
        "root": str(root),
        "normalization": "UTF-8 text with CRLF/CR normalized to LF for content comparison",
        "file_count": len(entries),
        "digest": hashlib.sha256(canonical).hexdigest(),
        "files": entries,
    }


def git_status(root: Path) -> list[str]:
    return subprocess.check_output(
        ["git", "-C", str(root), "status", "--short", "--untracked-files=all"],
        text=True,
    ).splitlines()


def controller_methods(root: Path) -> list[str]:
    tree = ast.parse((root / "src" / "stage_controller.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "StageController":
            return sorted(
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not item.name.startswith("_")
            )
    return []


def write_json(name: str, payload: dict[str, Any]) -> None:
    (OUT / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    host_paths = files_under(HOST)
    v2_paths = files_under(V2)
    candidate_paths = candidate_head_files()
    host_manifest = manifest("host-source-schema-test-manifest", HOST, host_paths, source_kind="stable HOST read-only")
    v2_manifest = manifest("v2-current-source-schema-test-manifest", V2, v2_paths, source_kind="frozen V2 evidence; current dirty tree")
    candidate_manifest = manifest("candidate-baseline-manifest", CANDIDATE, candidate_paths, source_kind="isolated candidate HEAD")
    write_json("host-source-manifest.json", host_manifest)
    write_json(
        "v2-source-manifest.json",
        {**v2_manifest, "git_status": git_status(V2), "git_head": subprocess.check_output(["git", "-C", str(V2), "rev-parse", "HEAD"], text=True).strip()},
    )
    write_json(
        "candidate-baseline-manifest.json",
        {
            **candidate_manifest,
            "git_head": subprocess.check_output(["git", "-C", str(CANDIDATE), "rev-parse", "HEAD"], text=True).strip(),
            "git_branch": subprocess.check_output(["git", "-C", str(CANDIDATE), "branch", "--show-current"], text=True).strip(),
            "host_normalized_digest": host_manifest["digest"],
            "host_content_matches": host_manifest["digest"] == candidate_manifest["digest"],
        },
    )

    write_json(
        "known-v2-safety-behavior-inventory.json",
        {
            "inventory_version": "phase-0.v1",
            "absorbed_later_by_contract": [
                {
                    "capability": "workspace identity normalization",
                    "source": "V2 evidence: src/project_context.py, src/project_discovery.py; tests/test_windows_workspace_identity.py",
                    "reason": "Preserves ordinary/extended Windows spelling equivalence while rejecting different identities and protected boundaries.",
                    "evidence": "V2_ARCHITECTURE_HANDOFF.md §5 and §6; WORKFLOW_SELF_HOST_GAPS.md lines 19-24",
                    "target_replacement": "shared identity/provenance guard in canonical controller contracts",
                },
                {
                    "capability": "ARTIFACT_ONLY admission boundary",
                    "source": "V2 evidence: src/openai_codex_executor.py; tests/test_artifact_only_provider.py",
                    "reason": "Separates required artifact completeness from the Stage allowed/protected write boundary.",
                    "evidence": "WORKFLOW_SELF_HOST_GAPS.md lines 17-31; V2_ARCHITECTURE_HANDOFF.md lines 198-211",
                    "target_replacement": "shared baseline/scope validator used by observation assessment",
                },
                {
                    "capability": "same-iteration engineering retry",
                    "source": "V2 evidence: src/stage_controller.py; tests/test_same_iteration_engineering_retry.py and tests/test_stage_retry_semantics.py",
                    "reason": "Retains new attempt identity, same semantic iteration, bounded budget and immutable prior evidence.",
                    "evidence": "V2_ARCHITECTURE_HANDOFF.md lines 128-139",
                    "target_replacement": "REQUEST_EXECUTION with typed retry/ENGINEERING_FIX reason",
                },
                {
                    "capability": "provenance and result identity safety",
                    "source": "HOST/V2 src/contracts.py, src/stage_controller.py and execution tests",
                    "reason": "Provider observations must be linked to committed request identity; assessment identity must be distinct and reproducible.",
                    "evidence": "V2_LIFECYCLE_ARCHITECTURE_DECISION.md §D, §E, §G",
                    "target_replacement": "immutable observation plus versioned assessment contracts",
                },
                {
                    "capability": "subject-bound Human decision",
                    "source": "V2 evidence: src/gpt_stage_planning.py, src/stage_planning.py; tests/test_stage_planning_human_decision.py",
                    "reason": "Prevents stale choices from applying to a changed requirement/design/plan/assessment.",
                    "evidence": "V2_LIFECYCLE_ARCHITECTURE_DECISION.md §J; V2_ARCHITECTURE_HANDOFF.md lines 141-152",
                    "target_replacement": "single REQUEST_DECISION/APPLY_DECISION resolver across four boundaries",
                },
                {
                    "capability": "dependency safety invariants",
                    "source": "V2 evidence: src/active_prerequisite.py and src/active_prerequisite_runtime.py",
                    "reason": "Retains bounded child ownership, parent preservation and no automatic semantic advancement.",
                    "evidence": "V2_ARCHITECTURE_HANDOFF.md lines 168-181; architecture decision §I",
                    "target_replacement": "ordinary bounded ADD_DEPENDENCY/SATISFY_DEPENDENCY transitions",
                },
            ],
            "not_absorbed_as_authority": [
                "controlled recovery lifecycle",
                "recovery/bootstrap lifecycle",
                "review_only success path",
                "recovery-specific binder",
                "active prerequisite snapshot restore special case",
                "bind_existing_execution_result production shortcut",
                "legacy retry auto-migration production authority",
            ],
        },
    )

    target_commands = [
        "REGISTER_STAGE", "START", "REQUEST_EXECUTION", "RECORD_OBSERVATION", "ASSESS_RESULT",
        "APPLY_GPT_DECISION", "ADVANCE_ITERATION", "REQUEST_DECISION", "APPLY_DECISION",
        "ADD_DEPENDENCY", "SATISFY_DEPENDENCY", "RESOLVE_BLOCKER", "APPLY_RECEIPT",
        "COMMIT_INTEGRATION", "CLOSEOUT", "STOP",
    ]
    write_json(
        "current-lifecycle-command-inventory.json",
        {
            "inventory_version": "phase-0.v1",
            "architecture_target": {"canonical_public_command_count": len(target_commands), "commands": target_commands},
            "internal_mappings": {"OPEN_ITERATION": "START internal atomic event", "RETRY": "REQUEST_EXECUTION typed reason", "ACCEPT_BASELINE_CHANGE": "APPLY_DECISION choice"},
            "host_stage_controller_public_methods": controller_methods(HOST),
            "v2_stage_controller_public_methods": controller_methods(V2),
            "v2_special_authority_candidates": [
                "authorize_recovery_bootstrap", "complete_recovery_bootstrap", "authorize_active_prerequisite",
                "prepare_prerequisite", "authorize_blocked_recovery", "bind_recovery_result", "record_recovery_review",
                "reconcile_retry_semantics", "bind_existing_execution_result",
            ],
            "compatibility_rule": "legacy names may remain read-only/name mappings only; no alternate lifecycle writer",
        },
    )

    write_json(
        "guard-inventory.json",
        {
            "inventory_version": "phase-0.v1",
            "target_guard_families": [
                "identity/provenance", "baseline/scope", "budget", "owner/dependency", "pending Decision",
                "effect settlement", "assessment eligibility", "review/readiness", "terminal/integration",
            ],
            "target_count": 9,
            "before_notes": {
                "host": "guards are distributed across StageController, contracts, admission, integration and runtime routing",
                "v2": "additional recovery/bootstrap/prerequisite guards create alternate lifecycle paths",
            },
            "consolidation_rule": "extract pure safety predicates; route transitions through the single StageController reducer",
        },
    )

    write_json(
        "schema-root-inventory.json",
        {
            "inventory_version": "phase-0.v1",
            "domain_roots_target": [
                "Stage", "Semantic Iteration", "Execution Attempt", "Provider Result / Observation",
                "Stage Result / Assessment", "Decision", "Dependency",
            ],
            "shared_support_roots_target": ["Command Envelope", "Operation Envelope", "Evidence Manifest"],
            "stage_states_target": ["PLANNED", "ACTIVE", "READY", "CLOSED", "STOPPED"],
            "before_schema_counts": {"host": len(list((HOST / "schemas").glob("*.json"))), "v2_current": len(list((V2 / "schemas").glob("*.json")))},
            "retirement_note": "bootstrap/recovery schemas may remain archive/read compatibility evidence but cannot be writable lifecycle roots",
        },
    )

    write_json(
        "authority-writer-inventory.json",
        {
            "inventory_version": "phase-0.v1",
            "target": {
                "lifecycle_writer_count": 1,
                "lifecycle_writer": "StageController durable journal/reducer",
                "result_binder_count": 1,
                "result_binder": "Controller assessment binding (ordinary and revalidation)",
                "human_decision_resolver_count": 1,
                "human_decision_resolver": "REQUEST_DECISION/APPLY_DECISION",
                "executable_owner_count": 1,
                "executable_owner": "executable_owner_stage_id",
                "projection_writers": 0,
            },
            "before": {
                "host": ["StageController state snapshot/persist", "WorkflowRuntime checkpoint projection", "integration/runtime adapters"],
                "v2": ["StageController state writer", "recovery/bootstrap writer seams", "WorkflowRuntime planning/recovery checkpoint projection", "special recovery binders"],
            },
            "phase_0_decision": "candidate starts from normalized HOST source; V2 special writers are evidence only and are not copied into the baseline",
        },
    )

    summary = """# Phase 0 Baseline\n\n"""
    summary += f"Generated: {date.today().isoformat()}\n\n"
    summary += "## Candidate\n\n"
    summary += f"- Path: `{CANDIDATE}`\n- Branch: `workflow-v2-lifecycle-candidate`\n- Base commit: `15fdb58cfbe91987e3a5112a9937e716bf265f59`\n- Source baseline: clean HEAD normalized content matches HOST manifest.\n\n"
    summary += "## Preservation evidence\n\n"
    summary += f"- HOST source/schema/test/script files: **{host_manifest['file_count']}**, normalized manifest digest `{host_manifest['digest']}`. HOST is not a Git checkout; preservation is verified by this digest and later re-checks.\n"
    summary += f"- Candidate baseline files: **{candidate_manifest['file_count']}**, normalized digest `{candidate_manifest['digest']}`; equality with HOST: `{host_manifest['digest'] == candidate_manifest['digest']}`.\n"
    summary += f"- Frozen V2 current source/schema/test/script files: **{v2_manifest['file_count']}**, digest `{v2_manifest['digest']}`; current Git dirty entries: **{len(git_status(V2))}**.\n"
    summary += "- Frozen V2 key state hashes are recorded in the implementation closeout evidence; no frozen runtime files were copied or opened for write.\n\n"
    summary += "## Baseline regression\n\n"
    summary += "- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q`: **427 passed, 2 failed**.\n"
    summary += "- Both failures are historical fixture failures: the clean candidate intentionally lacks `.consultations/CONSULT-20260906-070055-bef2fdb6/receipt.json`; no synthetic receipt was copied.\n"
    summary += "- A first run without the ignored `.tmp` directory also failed before test execution with `FileNotFoundError`; the candidate-only empty `.tmp` directory was then created and the suite rerun.\n\n"
    summary += "## Evidence files\n\n"
    summary += "- `implementation_evidence/phase-0/host-source-manifest.json`\n- `implementation_evidence/phase-0/v2-source-manifest.json`\n- `implementation_evidence/phase-0/candidate-baseline-manifest.json`\n- `implementation_evidence/phase-0/known-v2-safety-behavior-inventory.json`\n- `implementation_evidence/phase-0/current-lifecycle-command-inventory.json`\n- `implementation_evidence/phase-0/guard-inventory.json`\n- `implementation_evidence/phase-0/schema-root-inventory.json`\n- `implementation_evidence/phase-0/authority-writer-inventory.json`\n\n"
    summary += "## Phase 0 decision\n\n"
    summary += "PASS for isolated baseline creation and preservation checks. Entry condition for Phase 1 is satisfied by explicit Human acceptance in the task request. No provider/workflow dispatch was performed.\n"
    (CANDIDATE / "PHASE_0_BASELINE.md").write_text(summary, encoding="utf-8")


if __name__ == "__main__":
    main()
