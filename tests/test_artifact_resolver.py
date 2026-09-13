from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from src.artifact_resolver import ArtifactResolver, ArtifactResolverError


def make_resolver(tmp_path: Path, *, engine: Path | None = None, project_id: str = "project-a") -> ArtifactResolver:
    engine = engine or (tmp_path / "engine")
    project = tmp_path / project_id
    runtime = tmp_path / "machine-runtime"
    engine.mkdir(parents=True, exist_ok=True)
    project.mkdir(parents=True, exist_ok=True)
    runtime.mkdir(parents=True, exist_ok=True)
    return ArtifactResolver.from_roots(
        workspace_id="workspace-test-123",
        project_id=project_id,
        project_root=project,
        engine_root=engine,
        machine_runtime_root=runtime,
    )


def test_guidance_history_and_temp_manifest(tmp_path: Path) -> None:
    resolver = make_resolver(tmp_path)
    constitution = resolver.resolve("constitution", "PROJECT", source_operation="guidance")
    spec = resolver.resolve("spec", "PROJECT", source_operation="guidance", stage_id="stage-a-v1")
    receipt = resolver.resolve("gpt_review_receipt", "HISTORY", source_operation="review", operation_id="operation-1")
    temp = resolver.resolve("context", "TEMP", source_operation="bridge", operation_id="operation-1", relative_path="upload/packet.md")
    assert constitution.relative_path == ".specify/memory/constitution.md"
    assert spec.relative_path == "specs/stage-a-v1/spec.md"
    assert receipt.relative_path == ".research/reviews/operation-1/receipt.json"
    assert temp.relative_path.endswith("/runs/operation-1/context/upload/packet.md")
    assert temp.durable is False
    assert constitution.manifest()["resolver_version"] == "artifact-resolver.v1"
    assert constitution.manifest()["resolved_root"] == str((tmp_path / "project-a").resolve())


def test_project_a_b_isolation_and_relocated_engine(tmp_path: Path) -> None:
    shared_engine = tmp_path / "engine"
    a = make_resolver(tmp_path, engine=shared_engine, project_id="project-a")
    b = make_resolver(tmp_path, engine=shared_engine, project_id="project-b")
    assert a.resolve("spec", "PROJECT", source_operation="plan", stage_id="stage-v1").absolute_path != b.resolve("spec", "PROJECT", source_operation="plan", stage_id="stage-v1").absolute_path
    assert a.resolve("module", "ENGINE", source_operation="build", relative_path="src/module.py").absolute_path == b.resolve("module", "ENGINE", source_operation="build", relative_path="src/module.py").absolute_path
    relocated = tmp_path / "relocated-engine"
    relocated.mkdir()
    moved = make_resolver(tmp_path, engine=relocated, project_id="project-a")
    assert moved.resolve("spec", "PROJECT", source_operation="plan", stage_id="stage-v1").absolute_path == a.resolve("spec", "PROJECT", source_operation="plan", stage_id="stage-v1").absolute_path
    assert moved.resolve("module", "ENGINE", source_operation="build", relative_path="src/module.py").absolute_path != a.resolve("module", "ENGINE", source_operation="build", relative_path="src/module.py").absolute_path


def test_legacy_read_and_single_new_write(tmp_path: Path) -> None:
    resolver = make_resolver(tmp_path)
    legacy = (tmp_path / "project-a" / ".research" / "legacy-receipt.json").resolve()
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy", encoding="utf-8")
    assert resolver.legacy_read(legacy, expected_ownership="PROJECT") == b"legacy"
    target = resolver.resolve("verification_receipt", "HISTORY", source_operation="verify", operation_id="op-1")
    resolver.write_text(target, "new")
    assert target.absolute_path.read_text(encoding="utf-8") == "new"
    assert legacy.read_text(encoding="utf-8") == "legacy"
    with pytest.raises(ArtifactResolverError) as exc:
        resolver.write_text(target, "changed")
    assert exc.value.code == "TARGET_EXISTS"


@pytest.mark.parametrize("owner,kwargs", [
    ("ENGINE", {"relative_path": "../escape.py"}),
    ("ENGINE", {"relative_path": ".research/hidden.json"}),
    ("PROJECT", {"relative_path": "../escape.json"}),
])
def test_path_and_required_identity_guards(tmp_path: Path, owner: str, kwargs: dict[str, str]) -> None:
    resolver = make_resolver(tmp_path)
    with pytest.raises(ArtifactResolverError):
        resolver.resolve("artifact", owner, source_operation="test", **kwargs)
    with pytest.raises(ArtifactResolverError) as exc:
        resolver.resolve("context", "TEMP", source_operation="test", relative_path="x")
    assert exc.value.code == "OPERATION_ID_REQUIRED"
    with pytest.raises(ArtifactResolverError) as exc:
        resolver.resolve("context", "TEMP", source_operation="test", operation_id="op", workspace_id="other")
    assert exc.value.code == "WORKSPACE_IDENTITY_MISMATCH"


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    resolver = make_resolver(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "project-a" / ".research"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ArtifactResolverError) as exc:
        resolver.resolve("history", "HISTORY", source_operation="test", relative_path=".research/x.json")
    assert exc.value.code in {"SYMLINK_ESCAPE", "PATH_OUTSIDE_ROOT"}


def test_json_write_is_deterministic(tmp_path: Path) -> None:
    resolver = make_resolver(tmp_path)
    target = resolver.resolve("history", "HISTORY", source_operation="test", relative_path=".research/history/one.json")
    resolver.write_json(target, {"b": 2, "a": 1})
    assert json.loads(target.absolute_path.read_text(encoding="utf-8")) == {"a": 1, "b": 2}


def test_nul_segment_rejected(tmp_path: Path) -> None:
    resolver = make_resolver(tmp_path)
    with pytest.raises(ArtifactResolverError) as exc:
        resolver.resolve("artifact", "ENGINE", source_operation="test", relative_path="src/bad\x00name.py")
    assert exc.value.code == "PATH_INVALID"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction evidence is platform-specific")
def test_reparse_junction_escape_rejected(tmp_path: Path) -> None:
    resolver = make_resolver(tmp_path)
    outside = tmp_path / "outside-junction"
    outside.mkdir()
    research = tmp_path / "project-a" / ".research"
    research.mkdir()
    junction = research / "research-junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not junction.exists():
        pytest.skip("junction creation is unavailable")
    with pytest.raises(ArtifactResolverError) as exc:
        resolver.resolve("history", "HISTORY", source_operation="test", relative_path=".research/research-junction/x.json")
    assert exc.value.code in {"SYMLINK_ESCAPE", "PATH_OUTSIDE_ROOT"}
