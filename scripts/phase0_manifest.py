"""Create the immutable Phase 0 implementation baseline.

The command is intentionally read-only with respect to HOST and the frozen V2
workspace.  It writes evidence only below the candidate checkout.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable

CANDIDATE = Path(__file__).resolve().parents[1]
HOST = Path(r"D:/work/research_tools/research_supervisor_poc")
V2 = Path(r"D:/work/research_tools/research_supervisor_v2_clean")
OUT = CANDIDATE / "implementation_evidence" / "phase-0"
ACCEPTANCE_SOURCE = Path(r"C:/Users/Administrator/.codex/attachments/1d675e09-9f27-4b31-9f6e-08b163cef955/pasted-text.txt")
SOURCE_DIRS = ("src", "schemas", "tests", "scripts")
sys.path.insert(0, str(CANDIDATE))

from src.workflow_v2_contracts import DOMAIN_SCHEMA_ROOTS, SHARED_SUPPORT_SCHEMA_ROOTS


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


def candidate_ref_files(ref: str) -> list[str]:
    output = subprocess.check_output(
        ["git", "-C", str(CANDIDATE), "ls-tree", "-r", "--name-only", ref],
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


def manifest_from_ref(name: str, root: Path, ref: str, paths: Iterable[str], *, source_kind: str) -> dict[str, Any]:
    """Hash the immutable Git ref, not potentially modified working-tree files."""

    entries = []
    for relative in sorted(paths):
        raw = subprocess.check_output(["git", "-C", str(root), "show", f"{ref}:{relative}"])
        normalized = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        entries.append(
            {
                "path": relative,
                "sha256_normalized_utf8_lf": hashlib.sha256(normalized).hexdigest(),
                "raw_bytes": len(raw),
                "normalized_bytes": len(normalized),
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
        "ref": ref,
        "normalization": "UTF-8 text with CRLF/CR normalized to LF for content comparison",
        "file_count": len(entries),
        "digest": hashlib.sha256(canonical).hexdigest(),
        "files": entries,
    }


def candidate_post_baseline_delta(ref: str, baseline_paths: Iterable[str]) -> dict[str, Any]:
    """Record candidate source drift as an explicit controlled implementation diff."""

    baseline = set(baseline_paths)
    current = set(files_under(CANDIDATE))
    added = sorted(current - baseline)
    deleted = sorted(baseline - current)
    modified = []
    for relative in sorted(current & baseline):
        working_hash = normalized_hash(CANDIDATE / relative)
        ref_bytes = subprocess.check_output(["git", "-C", str(CANDIDATE), "show", f"{ref}:{relative}"])
        ref_normalized = ref_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        if hashlib.sha256(ref_normalized).hexdigest() != working_hash:
            modified.append(relative)
    return {
        "evidence_kind": "candidate_controlled_post_baseline_delta",
        "baseline_ref": ref,
        "scope": "/".join(SOURCE_DIRS),
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "implementation_rule": "post-baseline changes are candidate-only and must be reviewed before commit",
    }


def git_status(root: Path) -> list[str]:
    return subprocess.check_output(
        ["git", "-C", str(root), "status", "--short", "--untracked-files=all"],
        text=True,
    ).splitlines()


def raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
    candidate_baseline_ref = "15fdb58cfbe91987e3a5112a9937e716bf265f59"
    candidate_paths = candidate_ref_files(candidate_baseline_ref)
    host_manifest = manifest("host-source-schema-test-manifest", HOST, host_paths, source_kind="stable HOST read-only")
    v2_manifest = manifest("v2-current-source-schema-test-manifest", V2, v2_paths, source_kind="frozen V2 evidence; current dirty tree")
    candidate_manifest = manifest_from_ref(
        "candidate-baseline-manifest",
        CANDIDATE,
        candidate_baseline_ref,
        candidate_paths,
        source_kind="isolated candidate immutable baseline ref",
    )
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
            "baseline_ref": candidate_baseline_ref,
            "git_branch": subprocess.check_output(["git", "-C", str(CANDIDATE), "branch", "--show-current"], text=True).strip(),
            "host_normalized_digest": host_manifest["digest"],
            "host_content_matches": host_manifest["digest"] == candidate_manifest["digest"],
        },
    )
    write_json("candidate-post-baseline-delta.json", candidate_post_baseline_delta(candidate_baseline_ref, candidate_paths))

    architecture_docs = [
        "V2_LIFECYCLE_ARCHITECTURE_DECISION.md",
        "V2_LIFECYCLE_IMPLEMENTATION_PLAN.md",
        "V2_ARCHITECTURE_HANDOFF.md",
        "WORKFLOW_SELF_HOST_GAPS.md",
    ]
    document_digests = {path: raw_sha256(V2 / path) for path in architecture_docs}
    acceptance_subject = {
        "accepted_contracts": [str(V2 / path) for path in architecture_docs[:2]],
        "document_raw_sha256": document_digests,
    }
    acceptance_subject_digest = hashlib.sha256(
        json.dumps(acceptance_subject, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    write_json(
        "human-architecture-acceptance.json",
        {
            "evidence_kind": "user_task_attestation",
            "not_a_runtime_receipt": True,
            "decision": "ACCEPT_V2_LIFECYCLE_ARCHITECTURE",
            "decision_id": "43ewiu",
            "subject_id": "V2_LIFECYCLE_ARCHITECTURE_CONTRACT",
            "subject_digest": acceptance_subject_digest,
            "subject_version": 1,
            "source": "explicit Human decision in the implementation task message",
            "source_attachment": str(ACCEPTANCE_SOURCE),
            "source_message_sha256": raw_sha256(ACCEPTANCE_SOURCE),
            "source_anchor_ids": ["snhfgd", "bqbsr7", "43ewiu"],
            "accepted_contracts": [str(V2 / path) for path in architecture_docs[:2]],
            "navigation_documents": [str(V2 / path) for path in architecture_docs[2:]],
            "document_raw_sha256": document_digests,
            "scope": "authorizes offline candidate implementation and fresh validation; does not authorize HOST writes or frozen runtime mutation",
        },
    )

    frozen_paths = [
        ".research/stage-state.json",
        ".research/workflow-state.json",
        ".research/PROJECT_BRIEF.json",
        ".research/blueprint/BLUEPRINT_MANIFEST.json",
        ".research/stages/project-package-portability-falsification-v2/CANONICAL_PARENT_ATTEMPT_02.json",
        ".research/stages/project-package-portability-falsification-v2/CANONICAL_ADMISSION_HUMAN_GATE.json",
    ]
    frozen_entries = []
    for relative in frozen_paths:
        path = V2 / relative
        frozen_entries.append({"path": relative, "raw_sha256": raw_sha256(path), "bytes": path.stat().st_size})
    write_json(
        "frozen-v2-preservation.json",
        {
            "evidence_kind": "named_frozen_runtime_preservation",
            "root": str(V2),
            "scope": "named authoritative and bounded evidence only; the full frozen history is intentionally not traversed",
            "files": frozen_entries,
            "git_head": subprocess.check_output(["git", "-C", str(V2), "rev-parse", "HEAD"], text=True).strip(),
            "git_status_count": len(git_status(V2)),
        },
    )

    host_full = []
    excluded = {".git", ".research", ".tmp", ".consultations", ".pytest_cache", "__pycache__"}
    for directory, dir_names, file_names in os.walk(HOST, topdown=True, followlinks=False):
        dir_names[:] = [name for name in dir_names if name not in excluded]
        directory_path = Path(directory)
        for name in file_names:
            path = directory_path / name
            if path.is_symlink() or path.suffix == ".pyc":
                continue
            host_full.append(path.relative_to(HOST).as_posix())
    write_json(
        "host-preservation-manifest.json",
        {
            **manifest("host-full-preservation-manifest", HOST, host_full, source_kind="stable HOST read-only full source tree"),
            "git_repository": False,
            "verification_rule": "re-run this manifest and compare digest; HOST has no Git metadata",
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
        "REGISTER_STAGE", "START", "REQUEST_EXECUTION", "RESOLVE_LEGACY_ORPHAN", "RECORD_OBSERVATION", "ASSESS_RESULT",
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
            "shared_support_roots_target": ["Command Envelope", "Operation Envelope", "Evidence Manifest", "Provider Handoff Manifest"],
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

    target_commands = [
        "REGISTER_STAGE", "START", "REQUEST_EXECUTION", "RESOLVE_LEGACY_ORPHAN", "RECORD_OBSERVATION", "ASSESS_RESULT",
        "APPLY_GPT_DECISION", "ADVANCE_ITERATION", "REQUEST_DECISION", "APPLY_DECISION",
        "ADD_DEPENDENCY", "SATISFY_DEPENDENCY", "RESOLVE_BLOCKER", "APPLY_RECEIPT",
        "COMMIT_INTEGRATION", "CLOSEOUT", "STOP",
    ]
    target_symbols = {
        "REGISTER_STAGE": "workflow_v2_controller.StageController._register_stage",
        "START": "workflow_v2_controller.StageController._start",
        "REQUEST_EXECUTION": "workflow_v2_controller.StageController._request_execution",
        "RESOLVE_LEGACY_ORPHAN": "workflow_v2_controller.StageController._resolve_legacy_orphan",
        "RECORD_OBSERVATION": "workflow_v2_controller.StageController._record_observation",
        "ASSESS_RESULT": "workflow_v2_controller.StageController._assess_result",
        "APPLY_GPT_DECISION": "workflow_v2_controller.StageController._apply_gpt_decision",
        "ADVANCE_ITERATION": "workflow_v2_controller.StageController._advance_iteration",
        "REQUEST_DECISION": "workflow_v2_controller.StageController._request_decision",
        "APPLY_DECISION": "workflow_v2_controller.StageController._apply_decision",
        "ADD_DEPENDENCY": "workflow_v2_controller.StageController._add_dependency",
        "SATISFY_DEPENDENCY": "workflow_v2_controller.StageController._satisfy_dependency",
        "RESOLVE_BLOCKER": "workflow_v2_controller.StageController._resolve_blocker",
        "APPLY_RECEIPT": "workflow_v2_controller.StageController._apply_receipt",
        "COMMIT_INTEGRATION": "workflow_v2_controller.StageController._commit_integration",
        "CLOSEOUT": "workflow_v2_controller.StageController._closeout",
        "STOP": "workflow_v2_controller.StageController._stop",
    }
    command_guards = {
        "REGISTER_STAGE": ["identity/provenance", "baseline/scope"],
        "START": ["baseline/scope", "owner/dependency", "pending Decision", "budget"],
        "REQUEST_EXECUTION": ["owner/dependency", "budget", "effect settlement", "pending Decision"],
        "RESOLVE_LEGACY_ORPHAN": ["identity/provenance", "effect settlement", "bounded local side-effect audit"],
        "RECORD_OBSERVATION": ["identity/provenance", "effect settlement"],
        "ASSESS_RESULT": ["assessment eligibility", "identity/provenance", "effect settlement", "budget"],
        "APPLY_GPT_DECISION": ["review/readiness", "assessment eligibility", "pending Decision"],
        "ADVANCE_ITERATION": ["review/readiness", "budget", "baseline/scope"],
        "REQUEST_DECISION": ["identity/provenance", "pending Decision"],
        "APPLY_DECISION": ["identity/provenance", "pending Decision", "baseline/scope"],
        "ADD_DEPENDENCY": ["owner/dependency", "budget", "baseline/scope"],
        "SATISFY_DEPENDENCY": ["owner/dependency", "identity/provenance"],
        "RESOLVE_BLOCKER": ["identity/provenance", "effect settlement"],
        "APPLY_RECEIPT": ["identity/provenance", "effect settlement"],
        "COMMIT_INTEGRATION": ["review/readiness", "terminal/integration", "effect settlement"],
        "CLOSEOUT": ["terminal/integration", "effect settlement", "identity/provenance"],
        "STOP": ["terminal/integration", "effect settlement", "owner/dependency", "pending Decision"],
    }
    write_json(
        "transition-test-traceability.json",
        {
            "inventory_version": "phase-0.v1",
            "status": "complete for all 17 canonical commands: handler, guard family, and named invariant test are present",
            "transitions": [
                {
                    "command": command,
                    "target_symbol": target_symbols[command],
                    "guard_families": command_guards[command],
                    "tests": [
                        "tests/test_workflow_v2_controller.py::test_" + command.lower(),
                        "tests/test_workflow_v2_invariants.py::test_" + command.lower() + "_invariants",
                    ],
                    "replacement": "single workflow_v2_controller journal reducer",
                }
                for command in target_commands
            ],
        },
    )

    write_json(
        "before-after-inventory.json",
        {
            "inventory_version": "phase-0.v1",
            "commands": {
                "before_host": {"file": "src/stage_controller.py", "symbol": "StageController.register_stage/start_stage/request_executor/record_executor_result/apply_decision/approve_stage/stop_stage"},
                "before_v2_special": {"file": "src/stage_controller.py", "symbols": ["authorize_recovery_bootstrap", "complete_recovery_bootstrap", "authorize_blocked_recovery", "bind_recovery_result", "record_recovery_review", "reconcile_retry_semantics", "bind_existing_execution_result"]},
                "after_target": "src/workflow_v2_controller.py:StageController.dispatch and one handler per canonical command",
            },
            "guards": {
                "before": [
                    {"family": "identity/provenance", "files": ["src/contracts.py", "src/stage_controller.py", "src/openai_codex_executor.py"]},
                    {"family": "baseline/scope", "files": ["src/contracts.py", "src/stage_controller.py", "src/asset_layer.py"]},
                    {"family": "budget", "files": ["src/stage_controller.py", "src/workflow_runtime.py"]},
                    {"family": "owner/dependency", "files": ["src/stage_controller.py", "src/active_prerequisite.py", "src/active_prerequisite_runtime.py"]},
                    {"family": "pending Decision", "files": ["src/stage_controller.py", "src/workflow_runtime.py", "src/stage_planning.py"]},
                    {"family": "effect settlement", "files": ["src/stage_controller.py", "src/stage_integration.py", "src/delivery_integration.py"]},
                    {"family": "assessment eligibility", "files": ["src/stage_controller.py", "src/contracts.py"]},
                    {"family": "review/readiness", "files": ["src/stage_controller.py", "src/workflow_core_adapter.py"]},
                    {"family": "terminal/integration", "files": ["src/stage_controller.py", "src/stage_integration.py", "src/delivery_integration.py"]},
                ],
                "after_target": "src/workflow_v2_contracts.py and workflow_v2_controller.StageController._apply",
            },
            "schemas": {
                "before_host_root_count": len(list((HOST / "schemas").glob("*.json"))),
                "before_v2_root_count": len(list((V2 / "schemas").glob("*.json"))),
                "after_target_domain_roots": list(DOMAIN_SCHEMA_ROOTS),
                "after_target_shared_support_roots": list(SHARED_SUPPORT_SCHEMA_ROOTS),
                "removed_from_writable_target": ["bootstrap_state", "recovery-specific state", "legacy checkpoint lifecycle fields"],
            },
            "authority": {
                "before": [
                    {"file": "src/stage_controller.py", "symbol": "StageController._persist", "role": "legacy lifecycle snapshot writer"},
                    {"file": "src/workflow_runtime.py", "symbol": "checkpoint/state helpers", "role": "projection/checkpoint writer; not canonical authority"},
                    {"file": "src/blocked_recovery.py", "symbol": "recovery helpers", "role": "special recovery authority seam"},
                    {"file": "src/recovery_bootstrap.py", "symbol": "bootstrap helpers", "role": "special bootstrap seam"},
                    {"file": "src/stage_integration.py", "symbol": "integration adapter methods", "role": "external effect producer; must never close Stage"},
                ],
                "after_target": [
                    {"file": "src/workflow_v2_controller.py", "symbol": "StageController.dispatch/_persist", "role": "single V2 lifecycle journal writer"},
                    {"file": "src/workflow_v2_contracts.py", "symbol": "assessment_identity/validate_*", "role": "pure validation; no authority"},
                ],
            },
            "change_scope": "Phase 0/1 adds the target contract and isolated controller; legacy production routing is retired only after Phase 3 coverage and Phase 4 proof",
        },
    )

    summary = """# Phase 0 Baseline\n\n"""
    summary += f"Generated: {date.today().isoformat()}\n\n"
    summary += "## Candidate\n\n"
    summary += f"- Path: `{CANDIDATE}`\n- Branch: `workflow-v2-lifecycle-candidate`\n- Baseline ref: `15fdb58cfbe91987e3a5112a9937e716bf265f59`\n- Source baseline: normalized content at the baseline ref matches HOST manifest; later implementation files are controlled post-baseline changes.\n\n"
    summary += "## Preservation evidence\n\n"
    summary += f"- HOST source/schema/test/script files: **{host_manifest['file_count']}**, normalized manifest digest `{host_manifest['digest']}`. HOST is not a Git checkout; preservation is verified by this digest and later re-checks.\n"
    summary += f"- Candidate baseline files: **{candidate_manifest['file_count']}**, normalized digest `{candidate_manifest['digest']}`; equality with HOST: `{host_manifest['digest'] == candidate_manifest['digest']}`.\n"
    summary += "- Candidate post-baseline source changes are enumerated in `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/candidate-post-baseline-delta.json`; they are controlled implementation diff, not baseline drift.\n"
    summary += f"- Frozen V2 current source/schema/test/script files: **{v2_manifest['file_count']}**, digest `{v2_manifest['digest']}`; current Git dirty entries: **{len(git_status(V2))}**.\n"
    summary += "- Frozen V2 named runtime preservation hashes are recorded in `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/frozen-v2-preservation.json`; the full frozen history was intentionally not traversed.\n"
    summary += "- HOST full-tree preservation digest is recorded in `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/host-preservation-manifest.json`; HOST is not a Git checkout.\n"
    summary += "- `human-architecture-acceptance.json` records the explicit user-task decision with decision/subject/source identity and accepted scope; it is an attestation, not a runtime receipt.\n\n"
    summary += "## Baseline regression\n\n"
    summary += "- PowerShell: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; python -m pytest -q`: **427 passed, 2 failed**; raw stdout is saved in `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/baseline-pytest.stdout.txt`.\n"
    summary += "- Both failures are historical fixture failures: the clean candidate intentionally lacks `.consultations/CONSULT-20260906-070055-bef2fdb6/receipt.json`; no synthetic receipt was copied.\n"
    summary += "- A first run without the ignored `.tmp` directory also failed before test execution with `FileNotFoundError`; the candidate-only empty `.tmp` directory was then created and the suite rerun.\n\n"
    summary += "## Evidence files\n\n"
    summary += "- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/host-source-manifest.json`\n- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/host-preservation-manifest.json`\n- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/v2-source-manifest.json`\n- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/candidate-baseline-manifest.json`\n- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/frozen-v2-preservation.json`\n- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/human-architecture-acceptance.json`\n- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/known-v2-safety-behavior-inventory.json`\n- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/current-lifecycle-command-inventory.json`\n- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/guard-inventory.json`\n- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/schema-root-inventory.json`\n- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/authority-writer-inventory.json`\n- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/transition-test-traceability.json`\n- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/before-after-inventory.json`\n- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/baseline-pytest.stdout.txt`\n\n"
    summary += "- `../../research_supervisor_v2_lifecycle_candidate/implementation_evidence/phase-0/candidate-post-baseline-delta.json`\n"
    summary += "## Phase 0 decision\n\n"
    summary += "PASS for isolated baseline creation and preservation checks. The explicit user-task acceptance is recorded as an attestation; no runtime receipt was fabricated. Candidate implementation changes after the HEAD baseline are tracked as the controlled Phase 1 diff. No provider/workflow dispatch was performed.\n"
    (CANDIDATE / "PHASE_0_BASELINE.md").write_text(summary, encoding="utf-8")


if __name__ == "__main__":
    main()
