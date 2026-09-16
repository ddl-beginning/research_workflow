from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import portable_startup
from scripts import workflow
from src.execution_profile import EXECUTION_PROFILE_NAME, ensure_execution_profile
from src.portable_startup import (
    HEALTH_PROMPT,
    bridge_source_identity,
    default_runtime_config,
    ensure_machine_config,
    ensure_machine_directories,
    machine_paths,
    probe_browser,
    provision_bridge,
    secret_scan_paths,
)
from src.project_intake import IntakeMode, ProjectRequirementsIntake


def _brief(root: Path) -> None:
    ProjectRequirementsIntake(root).initialize(
        mode=IntakeMode.USER_CONFIRMED_BRIEF,
        brief={"goal": "keep a bounded project resumable", "success_criteria": ["resume works"]},
    )


def test_machine_config_is_portable_and_rejects_secret_fields(tmp_path: Path):
    paths = machine_paths(tmp_path / "machine")
    ensure_machine_directories(paths)
    bridge = tmp_path / "bridge"
    (bridge / "scripts").mkdir(parents=True)
    (bridge / "scripts" / "consult-pack.mjs").write_text("// bridge", encoding="utf-8")
    config, changed = ensure_machine_config(paths, bridge_root=bridge)

    assert changed is True
    assert config["bridge"]["profile_dir"] == paths.browser_profile.as_posix()
    assert config["executor"]["auth_mode"] == "chatgpt"
    assert "cookie" not in json.dumps(config).casefold()
    assert secret_scan_paths([paths.config])["passed"] is True
    paths.config.write_text(json.dumps({"token": "should fail"}), encoding="utf-8")
    with pytest.raises(Exception) as caught:
        ensure_machine_config(paths, bridge_root=bridge)
    assert getattr(caught.value, "code", None) == "MACHINE_CONFIG_SECRET"


def test_packaged_bridge_identity_excludes_machine_runtime(tmp_path: Path):
    bridge = tmp_path / "bridge"
    (bridge / "scripts").mkdir(parents=True)
    (bridge / "src").mkdir()
    (bridge / "package.json").write_text(
        json.dumps({"name": "chatgpt-browser-bridge", "version": "9.9.9", "dependencies": {"playwright": "1.0.0"}}),
        encoding="utf-8",
    )
    (bridge / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (bridge / "scripts" / "consult-pack.mjs").write_text("// entry\n", encoding="utf-8")
    (bridge / "src" / "bridge.mjs").write_text("// source\n", encoding="utf-8")
    (bridge / "node_modules").mkdir()
    (bridge / "node_modules" / "secret.txt").write_text("machine-only\n", encoding="utf-8")
    first = bridge_source_identity(bridge)
    (bridge / "node_modules" / "secret.txt").write_text("changed-machine-only\n", encoding="utf-8")
    second = bridge_source_identity(bridge)

    assert first == second
    assert first["version"] == "9.9.9"
    assert first["entrypoint"] == "scripts/consult-pack.mjs"


def test_setup_provisions_one_bridge_root_and_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "packaged-bridge"
    (source / "scripts").mkdir(parents=True)
    (source / "package.json").write_text(
        json.dumps({"name": "chatgpt-browser-bridge", "version": "1.2.3", "dependencies": {"playwright": "1.0.0"}}),
        encoding="utf-8",
    )
    (source / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (source / "scripts" / "consult-pack.mjs").write_text("// entry\n", encoding="utf-8")
    paths = machine_paths(tmp_path / "machine")
    ensure_machine_directories(paths)
    npm_calls: list[Path] = []

    def fake_run(argv, *, cwd, **kwargs):
        npm_calls.append(Path(cwd))
        target = Path(cwd)
        (target / "node_modules" / "playwright").mkdir(parents=True)
        (target / "node_modules" / "@modelcontextprotocol" / "sdk").mkdir(parents=True)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(portable_startup.shutil, "which", lambda value: "node.exe" if value in {"node", "node.exe"} else "npm.exe")
    monkeypatch.setattr(portable_startup.subprocess, "run", fake_run)
    first_root, first_identity, first_changed = provision_bridge(paths, source)
    second_root, second_identity, second_changed = provision_bridge(paths, source)

    assert first_root == second_root == paths.root / "bridge"
    assert first_identity == second_identity
    assert first_changed is True and second_changed is False
    assert npm_calls == [paths.root / "bridge"]
    assert (first_root / "node_modules" / "playwright").is_dir()


def test_machine_config_refreshes_packaged_bridge_identity_after_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "packaged-bridge"
    (source / "scripts").mkdir(parents=True)
    (source / "package.json").write_text(
        json.dumps({"name": "chatgpt-browser-bridge", "version": "2.0.0", "dependencies": {"playwright": "1.0.0"}}),
        encoding="utf-8",
    )
    (source / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (source / "scripts" / "consult-pack.mjs").write_text("// updated entry\n", encoding="utf-8")
    paths = machine_paths(tmp_path / "machine")
    ensure_machine_directories(paths)
    old = dict(default_runtime_config(paths, bridge_root=source))
    old["bridge"] = dict(old["bridge"], source_digest="old-digest", version="0.1.0")
    paths.config.write_text(json.dumps(old), encoding="utf-8")

    refreshed, changed = ensure_machine_config(paths, bridge_root=source)

    assert changed is True
    assert refreshed["bridge"]["version"] == "2.0.0"
    assert refreshed["bridge"]["source_digest"] == bridge_source_identity(source)["source_digest"]


def test_profile_is_persisted_in_canonical_project_brief(tmp_path: Path):
    _brief(tmp_path)
    first = ensure_execution_profile(tmp_path)
    second = ensure_execution_profile(tmp_path)
    document = json.loads((tmp_path / ".research" / "PROJECT_BRIEF.json").read_text(encoding="utf-8"))

    assert first["name"] == EXECUTION_PROFILE_NAME
    assert second == first
    assert document["execution_profile"]["authority"] == "PROJECT_BRIEF"
    assert not (tmp_path / ".workflow-v2" / "journal.json").exists()


def test_browser_probe_maps_login_without_retaining_subprocess_output(tmp_path: Path):
    paths = machine_paths(tmp_path / "machine")
    ensure_machine_directories(paths)
    bridge = tmp_path / "bridge"
    (bridge / "scripts").mkdir(parents=True)
    (bridge / "scripts" / "consult.mjs").write_text("// bridge", encoding="utf-8")
    config = default_runtime_config(paths, bridge_root=bridge)

    calls = []

    def login_runner(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="LOGIN_REQUIRED account details must not be retained")

    result = probe_browser(config, paths, command_runner=login_runner)
    assert result.code == "GPT_AUTH_REQUIRED"
    health = json.loads(paths.health.read_text(encoding="utf-8"))
    assert health["code"] == "GPT_AUTH_REQUIRED"
    assert "account details" not in json.dumps(health)
    assert calls and HEALTH_PROMPT in calls[0][0][0]


def test_no_argument_command_gives_safe_init_guidance(tmp_path: Path):
    args = workflow.build_parser().parse_args(["--project", str(tmp_path), "--json"])
    code, result = workflow.run_command(args)
    assert code == 1
    assert result["code"] == "INIT_INPUT_REQUIRED"
    assert not (tmp_path / ".research").exists()
    assert not (tmp_path / ".workflow-v2").exists()


def test_launcher_skill_is_thin_and_validated():
    skill = Path(__file__).parents[1] / "skills" / "workflow-launcher" / "SKILL.md"
    text = skill.read_text(encoding="utf-8").casefold()
    assert "$workflow" in text
    assert "workflow resume" in text
    assert "stagecontroller" not in text
    assert len(text.splitlines()) < 30
