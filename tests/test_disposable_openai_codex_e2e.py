from __future__ import annotations

import os
import subprocess

from scripts.disposable_openai_codex_e2e import (
    ACTION_RATIONALE,
    PROJECT_INSTRUCTIONS_PATH,
    SOURCE_PATH,
    TASK_OBJECTIVE,
    TEST_COMMAND,
    TEST_PATH,
    _contract,
    _make_disposable_repo,
    gpt_review_hook,
)


def test_disposable_fixture_is_nontrivial_and_scoped(tmp_path):
    _make_disposable_repo(tmp_path)
    assert (tmp_path / SOURCE_PATH).read_text(encoding="utf-8") == "def add(a, b):\n    return a - b\n"
    assert "assert add(2, 3) == 5" in (tmp_path / TEST_PATH).read_text(encoding="utf-8")
    instructions = (tmp_path / PROJECT_INSTRUCTIONS_PATH).read_text(encoding="utf-8")
    assert "not the root workflow" in instructions
    assert "do not spawn subagents" in instructions
    assert f"`{SOURCE_PATH}`" in instructions
    assert f"`{TEST_COMMAND}`" in instructions
    assert "protected path" in instructions
    initial = subprocess.run(
        ["python", "-m", "pytest", "-q", TEST_PATH],
        cwd=tmp_path,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert initial.returncode != 0
    contract = _contract(tmp_path)
    assert contract["allowed_paths"] == [SOURCE_PATH]
    assert "tests" in contract["protected_paths"]
    assert PROJECT_INSTRUCTIONS_PATH in contract["protected_paths"]
    assert TEST_COMMAND == "pytest -q tests/test_add.py"
    assert TASK_OBJECTIVE == ACTION_RATIONALE
    assert SOURCE_PATH in TASK_OBJECTIVE
    assert TEST_COMMAND in TASK_OBJECTIVE
    assert "non-empty diff" in TASK_OBJECTIVE
    assert "protected paths" in TASK_OBJECTIVE
    tracked = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-files", "--error-unmatch", PROJECT_INSTRUCTIONS_PATH],
        capture_output=True,
        text=True,
        check=False,
    )
    assert tracked.returncode == 0
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip() == ""


def test_bounded_gpt_review_accepts_only_complete_coding_e2e_evidence():
    accepted = gpt_review_hook(
        {
            "status": "SUCCEEDED",
            "changed_files": [SOURCE_PATH],
            "tests": [{"name": TEST_COMMAND, "status": "PASS"}],
            "measurements": {
                "openai_codex": {
                    "acceptance": {
                        "coding_e2e_accepted": True,
                        "turn_completed": True,
                        "allowed_changed_file": True,
                        "required_changed_path": True,
                        "required_test_passed": True,
                        "nonempty_diff": True,
                    }
                }
            },
        }
    )
    assert accepted["decision"] == "ACCEPT"

    rejected = gpt_review_hook(
        {
            "status": "SUCCEEDED",
            "changed_files": [],
            "tests": [],
            "measurements": {"openai_codex": {"acceptance": {"coding_e2e_accepted": False}}},
        }
    )
    assert rejected["decision"] == "REJECT"
