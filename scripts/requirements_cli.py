#!/usr/bin/env python3
"""Thin command-line entry point for Step 11 Requirements Intake.

All state is project-local and canonical at ``.research/PROJECT_BRIEF.json``.
This wrapper never imports the Stage controller or a GPT/browser bridge.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.project_intake import requirements_cli_main  # noqa: E402


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(requirements_cli_main())
