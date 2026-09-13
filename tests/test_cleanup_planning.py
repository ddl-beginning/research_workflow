from pathlib import Path

from scripts.cleanup_planning_e2e import _classify, run


def test_classification_keeps_unknown_and_fences_destructive_candidates():
    assert _classify("implementation_evidence/run.json")[1] == "MOVE"
    assert _classify(".pytest_cache/v/cache/nodeids")[1] == "DELETE_CANDIDATE"
    assert _classify(".research/PROJECT_BRIEF.json")[1] == "KEEP"
    assert _classify("unclassified.bin")[0] == "UNKNOWN"


def test_run_emits_exact_manifests_without_mutation(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "engine.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / "implementation_evidence").mkdir()
    (tmp_path / "implementation_evidence" / "history.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / ".pytest_cache").mkdir()
    (tmp_path / ".pytest_cache" / "nodeids").write_text("[]\n", encoding="utf-8")
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))

    result = run(tmp_path)

    assert result["status"] == "PASS"
    assert result["destructive_actions"] == 0
    assert result["human_gate"] == "HUMAN_DESTRUCTIVE_ACTION_GATE"
    assert result["counts"]["MOVE"] == 1
    assert result["counts"]["DELETE_CANDIDATE"] == 1
    assert (tmp_path / ".research/cleanup-planning/delete-candidate-manifest.json").is_file()
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert set(before).issubset(set(after))
    assert (tmp_path / "implementation_evidence" / "history.json").is_file()
    assert (tmp_path / ".pytest_cache" / "nodeids").is_file()
