from __future__ import annotations

import sys
from pathlib import Path

from scripts.research_workflow_cli import (
    RuntimePaths,
    _copy_atomic,
    _ensure_mcp_approval_config,
    _mcp_entries,
    _mcp_approval_summary,
    _restore_registration_argv,
    registration_matches,
    skill_fingerprint,
)


def _paths(root: Path) -> RuntimePaths:
    launcher = root / "scripts" / "workflow_mcp.py"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("launcher", encoding="utf-8")
    source = root / ".agents" / "skills" / "research-workflow"
    source.mkdir(parents=True, exist_ok=True)
    (source / "SKILL.md").write_text("skill", encoding="utf-8")
    target = root / "user-skills" / "research-workflow"
    return RuntimePaths(
        runtime_root=root,
        launcher=launcher.resolve(),
        skill_source=source.resolve(),
        user_skill_root=target.parent.resolve(),
        skill_target=target.resolve(),
        codex_executable=Path(sys.executable).resolve(),
        python_executable=Path(sys.executable).resolve(),
        quick_validate=None,
    )


def test_registration_match_is_bounded_to_target_stdio_identity(tmp_path):
    paths = _paths(tmp_path)
    matching = {
        "name": "research-supervisor",
        "transport": {
            "type": "stdio",
            "command": str(paths.python_executable),
            "args": [str(paths.launcher)],
            "env": None,
        },
    }
    assert registration_matches(matching, paths)
    mismatched = {
        **matching,
        "transport": {**matching["transport"], "args": [str(tmp_path / "other.py")]},
    }
    assert not registration_matches(mismatched, paths)


def test_skill_copy_and_fingerprint_are_deterministic(tmp_path):
    paths = _paths(tmp_path)
    _copy_atomic(paths.skill_source / "SKILL.md", paths.skill_target / "SKILL.md")
    first = skill_fingerprint(paths.skill_target)
    _copy_atomic(paths.skill_source / "SKILL.md", paths.skill_target / "SKILL.md")
    second = skill_fingerprint(paths.skill_target)
    assert first == second
    assert first["files"] == ["SKILL.md"]


def test_mcp_entries_accepts_official_list_or_wrapped_payload():
    entries = [{"name": "one"}, {"name": "two"}]
    assert _mcp_entries(entries) == entries
    assert _mcp_entries({"servers": entries}) == entries
    assert _mcp_entries({"mcp_servers": entries}) == entries
    assert _mcp_entries({"unexpected": True}) == []


def test_failed_replacement_restore_is_conservative(tmp_path):
    paths = _paths(tmp_path)
    prior = {
        "name": "research-supervisor",
        "transport": {
            "type": "stdio",
            "command": str(paths.python_executable),
            "args": [str(paths.launcher), "--mode", "status"],
            "env": None,
            "cwd": None,
        },
    }
    restore = _restore_registration_argv(prior, "research-supervisor", paths.codex_executable)
    assert restore == [
        str(paths.codex_executable),
        "mcp",
        "add",
        "research-supervisor",
        "--",
        str(paths.python_executable),
        str(paths.launcher),
        "--mode",
        "status",
    ]

    secret_entry = {
        **prior,
        "transport": {**prior["transport"], "env": {"TOKEN": "redacted"}},
    }
    assert _restore_registration_argv(secret_entry, "research-supervisor", paths.codex_executable) is None
    runtime_config_entry = {
        **prior,
        "transport": {
            **prior["transport"],
            "env": {"RESEARCH_WORKFLOW_RUNTIME_CONFIG": str(tmp_path / "runtime.json")},
        },
    }
    assert _restore_registration_argv(runtime_config_entry, "research-supervisor", paths.codex_executable) == [
        str(paths.codex_executable),
        "mcp",
        "add",
        "research-supervisor",
        "--env",
        f"RESEARCH_WORKFLOW_RUNTIME_CONFIG={tmp_path / 'runtime.json'}",
        "--",
        str(paths.python_executable),
        str(paths.launcher),
        "--mode",
        "status",
    ]
    inherited_env_entry = {
        **prior,
        "transport": {**prior["transport"], "env_vars": ["TOKEN"]},
    }
    assert _restore_registration_argv(inherited_env_entry, "research-supervisor", paths.codex_executable) is None


def test_mcp_approval_upsert_is_parent_scoped_and_idempotent(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    config.write_text(
        """[mcp_servers.other]\ncommand = \"other\"\n\n[mcp_servers.research-supervisor]\ncommand = \"python\"\n\n[mcp_servers.research-supervisor.tools.workflow_resume]\napproval_mode = \"prompt\"\n\n[projects.'D:/work']\ntrust_level = \"trusted\"\n""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    first = _ensure_mcp_approval_config(paths)
    second = _ensure_mcp_approval_config(paths)
    text = config.read_text(encoding="utf-8")

    assert first["changed"] is True
    assert second["changed"] is False
    assert _mcp_approval_summary(paths)["valid"] is True
    assert text.count('default_tools_approval_mode = "approve"') == 1
    assert "[mcp_servers.research-supervisor.tools.workflow_resume]" in text
    assert 'approval_mode = "prompt"' in text
    assert "[mcp_servers.other]" in text
    assert "[projects.'D:/work']" in text
