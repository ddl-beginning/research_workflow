from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import research_workflow_cli as cli


def test_init_uses_product_initializer_without_installer_dependencies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime_config = tmp_path / "runtime-composition.json"
    calls: list[tuple[str, str | None]] = []

    def initialize(workspace_value: str, *, config_path: str | None = None):
        calls.append((workspace_value, config_path))
        return {"ready": True, "missing": [], "workspace": workspace_value}

    monkeypatch.setattr(cli, "_initialize_product_runtime", initialize)
    monkeypatch.setattr(cli, "_paths_from_args", lambda _args: pytest.fail("init must bypass discover_paths"))
    monkeypatch.setattr(cli, "install_skill", lambda _paths: pytest.fail("init must not install the skill"))
    monkeypatch.setattr(cli, "ensure_registration", lambda _paths: pytest.fail("init must not register MCP"))

    args = cli.build_parser().parse_args(
        ["init", "--workspace", str(workspace), "--runtime-config", str(runtime_config)]
    )
    code, result = cli.run_command(args)

    assert code == 0
    assert calls == [(str(workspace), str(runtime_config))]
    assert result["ready"] is True
    assert result["operation"] == "init"
    assert result["schema_version"] == "research_workflow_init.v1"


def test_init_returns_a_bounded_missing_config_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = tmp_path / "missing-runtime-composition.json"
    diagnostic = {
        "ready": False,
        "missing": [
            {
                "component": "runtime.config",
                "code": "FILE_NOT_FOUND",
                "message": "configured runtime config is missing",
            }
        ],
    }
    monkeypatch.setattr(cli, "_initialize_product_runtime", lambda *_args, **_kwargs: diagnostic)

    args = cli.build_parser().parse_args(
        ["init", "--workspace", str(workspace), "--runtime-config", str(missing)]
    )
    code, result = cli.run_command(args)

    assert code == 1
    assert result["ready"] is False
    assert result["missing"] == diagnostic["missing"]
    assert result["operation"] == "init"


def test_init_requires_an_explicit_workspace():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["init"])


def test_init_direct_script_from_external_cwd_reports_missing_config(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = workspace / ".research" / "runtime-composition.json"
    script = Path(cli.__file__).resolve()

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "init",
            "--workspace",
            str(workspace),
            "--runtime-config",
            str(missing),
        ],
        cwd=str(tmp_path),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "Traceback" not in completed.stdout
    assert "Traceback" not in completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["error"]["code"] == "RUNTIME_CONFIG_INVALID"
    assert payload["error"]["message"] == "configured runtime composition file does not exist"
    assert payload["details"]["missing"][0]["code"] == "FILE_NOT_FOUND"
    assert not (workspace / ".workflow-v2").exists()
