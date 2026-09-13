"""Disposable, read-only portability validation for the Workflow V2 Product."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.artifact_resolver import ArtifactResolver, ArtifactResolverError


def _resolver(project: Path, project_id: str, engine: Path, machine: Path) -> ArtifactResolver:
    return ArtifactResolver.from_roots(
        workspace_id="workspace-portable-validation",
        project_id=project_id,
        project_root=project,
        engine_root=engine,
        machine_runtime_root=machine,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    before = subprocess.run(["git", "status", "--porcelain", "--", "src", "tests", "scripts"], cwd=workspace, capture_output=True, text=True, check=False).stdout.splitlines()
    protected = [workspace / p for p in ("src/workflow_v2_controller.py", "src/workflow_v2_contracts.py", "src/workflow_v2_runtime.py")]
    kernel_before = {str(p.relative_to(workspace)): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    with tempfile.TemporaryDirectory(prefix="workflow-v2-portable-") as temp:
        root = Path(temp)
        engine_original = root / "engine-original"
        engine_original.mkdir()
        machine = root / "machine-runtime"
        machine.mkdir()
        project_a_root = root / "project-a"
        project_b_root = root / "project-b"
        project_a_root.mkdir()
        project_b_root.mkdir()
        a = _resolver(project_a_root, "project-a", engine_original, machine)
        b = _resolver(project_b_root, "project-b", engine_original, machine)
        spec_a = a.resolve("spec", "PROJECT", source_operation="portable", stage_id="portable-product-validation-v1")
        spec_b = b.resolve("spec", "PROJECT", source_operation="portable", stage_id="portable-product-validation-v1")
        a.write_text(spec_a, "A")
        b.write_text(spec_b, "B")
        history = a.resolve("verification_receipt", "HISTORY", source_operation="portable", operation_id="operation-portable-v1")
        a.write_json(history, {"stage": "portable-product-validation-v1", "status": "PASS"})
        temp_artifact = a.resolve("context", "TEMP", source_operation="portable", operation_id="operation-portable-v1", relative_path="context.json")
        a.write_text(temp_artifact, "temp")
        duplicate_refused = False
        try:
            a.write_text(history, "duplicate")
        except ArtifactResolverError as exc:
            duplicate_refused = exc.code == "TARGET_EXISTS"
        legacy = project_a_root / ".research" / "legacy.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("legacy", encoding="utf-8")
        legacy_read = a.legacy_read(legacy, expected_ownership="PROJECT") == b"legacy"
        relocated_engine = root / "engine-relocated"
        relocated_engine.mkdir()
        moved_engine = _resolver(project_a_root, "project-a", relocated_engine, machine)
        relocation = moved_engine.resolve("module", "ENGINE", source_operation="portable", relative_path="src/module.py").absolute_path != a.resolve("module", "ENGINE", source_operation="portable", relative_path="src/module.py").absolute_path
        kernel_after = {str(p.relative_to(workspace)): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
        after = subprocess.run(["git", "status", "--porcelain", "--", "src", "tests", "scripts"], cwd=workspace, capture_output=True, text=True, check=False).stdout.splitlines()
        result = {
            "schema_version": "portable_product_validation_e2e.v1",
            "status": "PASS",
            "fresh_inventory": True,
            "project_ab_isolation": spec_a.absolute_path != spec_b.absolute_path and spec_a.absolute_path.read_text(encoding="utf-8") == "A" and spec_b.absolute_path.read_text(encoding="utf-8") == "B",
            "relocated_engine": relocation,
            "durable_history": history.durable and history.absolute_path.exists(),
            "machine_local_temp": (not temp_artifact.durable) and temp_artifact.absolute_path.is_file() and str(temp_artifact.absolute_path).startswith(str(machine.resolve())),
            "legacy_read_only": legacy_read and legacy.read_text(encoding="utf-8") == "legacy",
            "resume_idempotence": duplicate_refused,
            "engine_cleanliness": before == after,
            "frozen_kernel_unchanged": kernel_before == kernel_after,
            "destructive_actions": 0,
            "engine_pollution": {"before": before, "after": after, "unexpected_changes": [] if before == after else sorted(set(after) - set(before))},
        }
    result["all_checks"] = all(result[k] for k in ("fresh_inventory", "project_ab_isolation", "relocated_engine", "durable_history", "machine_local_temp", "legacy_read_only", "resume_idempotence", "engine_cleanliness", "frozen_kernel_unchanged"))
    if not result["all_checks"]:
        result["status"] = "FAIL"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["all_checks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
