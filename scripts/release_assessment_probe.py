#!/usr/bin/env python3
"""Build one immutable Stage assessment through installed V2 contracts."""

from __future__ import annotations

import json
import sys

from src.workflow_v2_contracts import assess_observation


def main() -> int:
    request = json.load(sys.stdin)
    assessment = assess_observation(
        request["observation"],
        request["manifest"],
        baseline_digest=request["baseline_digest"],
        validator_code_digest=request["validator_code_digest"],
        validation_contract_revision=request["validation_contract_revision"],
        checks=request["checks"],
    )
    print(json.dumps(assessment, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
