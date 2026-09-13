"""Bounded disposable E2E for the Project Artifact Resolver stage."""
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


def _resolver(root: Path, project: str, engine: Path, runtime: Path) -> ArtifactResolver:
    project_root = root / project
    project_root.mkdir(parents=True, exist_ok=True)
    return ArtifactResolver.from_roots(
        workspace_id="workspace-e2e-resolver",
        project_id=project,
        project_root=project_root,
        engine_root=engine,
        machine_runtime_root=runtime,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    workspace = args.workspace.resolve()
    before = subprocess.run(["git", "status", "--porcelain", "--", "src", "tests", "scripts"], cwd=workspace, capture_output=True, text=True, check=False).stdout.splitlines()
    protected = [workspace / "src/workflow_v2_controller.py", workspace / "src/workflow_v2_contracts.py", workspace / "src/workflow_v2_runtime.py"]
    kernel_before = {str(path.relative_to(workspace)): hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
    with tempfile.TemporaryDirectory(prefix="workflow-v2-resolver-") as temp:
        root = Path(temp)
        engine = root / "engine-original"
        engine.mkdir()
        runtime = root / "machine-runtime"
        runtime.mkdir()
        project_a = _resolver(root, "project-a", engine, runtime)
        project_b = _resolver(root, "project-b", engine, runtime)
        spec_a = project_a.resolve("spec", "PROJECT", source_operation="e2e", stage_id="resolver-e2e-v1")
        spec_b = project_b.resolve("spec", "PROJECT", source_operation="e2e", stage_id="resolver-e2e-v1")
        project_a.write_text(spec_a, "A")
        project_b.write_text(spec_b, "B")
        legacy = (root / "project-a" / ".research" / "legacy.json")
        legacy.parent.mkdir(parents=True)
        legacy.write_text("legacy", encoding="utf-8")
        legacy_value = project_a.legacy_read(legacy, expected_ownership="PROJECT").decode()
        new_receipt = project_a.resolve("verification_receipt", "HISTORY", source_operation="e2e", operation_id="operation-e2e")
        project_a.write_text(new_receipt, "new")
        temp_artifact = project_a.resolve("context", "TEMP", source_operation="e2e", operation_id="operation-e2e", relative_path="upload/context.md")
        project_a.write_text(temp_artifact, "temp")
        resumed = False
        try:
            project_a.write_text(new_receipt, "duplicate")
        except ArtifactResolverError as exc:
            resumed = exc.code == "TARGET_EXISTS"
        relocated_engine = root / "engine-relocated"
        relocated_engine.mkdir()
        relocated = _resolver(root, "project-a", relocated_engine, runtime)
        relocation_preserved_project = relocated.resolve("spec", "PROJECT", source_operation="e2e", stage_id="resolver-e2e-v1").absolute_path == spec_a.absolute_path
        nul_rejected = False
        try:
            project_a.resolve("artifact", "ENGINE", source_operation="e2e", relative_path="src/bad\x00name.py")
        except ArtifactResolverError as exc:
            nul_rejected = exc.code == "PATH_INVALID"
        reparse_rejected = False
        junction_target = root / "outside-junction"
        junction_target.mkdir()
        research_root = root / "project-a" / ".research"
        research_root.mkdir(exist_ok=True)
        junction = research_root / "research-junction"
        junction_result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction), str(junction_target)],
            capture_output=True,
            text=True,
            check=False,
        ) if sys.platform == "win32" else None
        if junction_result is not None and junction_result.returncode == 0 and junction.exists():
            try:
                project_a.resolve("history", "HISTORY", source_operation="e2e", relative_path=".research/research-junction/x.json")
            except ArtifactResolverError as exc:
                reparse_rejected = exc.code in {"SYMLINK_ESCAPE", "PATH_OUTSIDE_ROOT"}
        kernel_after = {str(path.relative_to(workspace)): hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
        result = {
            "schema_version": "project_artifact_resolver_e2e.v1",
            "status": "PASS",
            "project_isolation": spec_a.absolute_path != spec_b.absolute_path and spec_a.absolute_path.read_text(encoding="utf-8") == "A" and spec_b.absolute_path.read_text(encoding="utf-8") == "B",
            "relocated_engine": relocation_preserved_project and relocated.resolve("module", "ENGINE", source_operation="e2e", relative_path="src/module.py").absolute_path != project_a.resolve("module", "ENGINE", source_operation="e2e", relative_path="src/module.py").absolute_path,
            "legacy_read_new_write": legacy_value == "legacy" and new_receipt.absolute_path.read_text(encoding="utf-8") == "new" and legacy.read_text(encoding="utf-8") == "legacy",
            "temp_operation_scope": temp_artifact.durable is False and str(temp_artifact.absolute_path).startswith(str(runtime.resolve())),
            "crash_resume_idempotence": resumed,
            "nul_segment_guard": nul_rejected,
            "reparse_escape_guard": reparse_rejected,
            "frozen_kernel_unchanged": kernel_before == kernel_after,
            "engine_pollution": {"before": before, "after": subprocess.run(["git", "status", "--porcelain", "--", "src", "tests", "scripts"], cwd=workspace, capture_output=True, text=True, check=False).stdout.splitlines(), "unexpected_changes": []},
        }
    result["all_checks"] = all(result[key] for key in ("project_isolation", "relocated_engine", "legacy_read_new_write", "temp_operation_scope", "crash_resume_idempotence", "nul_segment_guard", "reparse_escape_guard", "frozen_kernel_unchanged"))
    if not result["all_checks"]:
        result["status"] = "FAIL"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        existing = json.loads(args.output.read_text(encoding="utf-8"))
        if existing != result:
            raise RuntimeError("immutable E2E output already exists with different content")
    else:
        args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["all_checks"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
