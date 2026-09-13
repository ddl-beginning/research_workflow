from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_portable_validation_e2e(tmp_path: Path) -> None:
    output = tmp_path / "validation.json"
    result = subprocess.run(
        [sys.executable, "scripts/portable_product_validation_e2e.py", "--workspace", str(Path.cwd()), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["all_checks"] is True
    assert payload["destructive_actions"] == 0
