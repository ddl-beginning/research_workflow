from __future__ import annotations

import json
from pathlib import Path

from scripts import product_doctor


def test_doctor_is_read_only_and_reports_unconfigured_clean_workspace(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "new-project"
    workspace.mkdir()
    monkeypatch.setattr(product_doctor, "_registration_check", lambda _config: {"name": "mcp.product_entry", "status": "PASS"})

    result = product_doctor.run_doctor(workspace, workspace / ".research" / "missing.json")

    assert result["ready"] is False
    assert result["writes_performed"] is False
    assert result["provider_dispatch_performed"] is False
    assert result["gpt_calls_performed"] is False
    assert not (workspace / ".workflow-v2").exists()
    assert any(item["name"] == "runtime.config" and item["status"] == "FAIL" for item in result["checks"])


def test_doctor_uses_real_machine_config_without_creating_stage(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "new-project"
    workspace.mkdir()
    machine = tmp_path / "machine"
    config = machine / "runtime.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "schema_version": "runtime_composition.v1",
                "lifecycle_version": "v2",
                "stage": {"state_path": ".workflow-v2/journal.json"},
                "executor": {
                    "providers": ["openai-codex"],
                    "preferred_model": "gpt-5.6-luna",
                    "fallback_model": None,
                    "auth_mode": "chatgpt",
                    "timeout_seconds": 60,
                },
                "bridge": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(product_doctor, "_registration_check", lambda _config: {"name": "mcp.product_entry", "status": "PASS"})
    monkeypatch.setattr(
        product_doctor,
        "doctor_product_runtime",
        lambda *_args, **_kwargs: {
            "lifecycle_version": "v2",
            "provider": {"available": True, "authenticated": True, "auth_mode": "chatgpt", "standard_model_available": True},
            "missing": [],
        },
    )

    result = product_doctor.run_doctor(workspace, config)

    assert result["runtime"]["lifecycle_version"] == "v2"
    assert result["writes_performed"] is False
    assert not (workspace / ".workflow-v2").exists()

