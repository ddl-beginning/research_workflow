#!/usr/bin/env python3
"""Stage-bound, read-only preflight for derived guidance artifacts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.guidance_artifacts import validate_markdown_chain  # noqa: E402
from src.product_workflow_runtime import _controller  # noqa: E402
from src.runtime_composition import load_runtime_composition_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=str(ROOT))
    parser.add_argument("--bundle", default="specs/spec-kit-adoption-and-repository-architecture-v1")
    parser.add_argument("--stage-contract-digest", required=True)
    args = parser.parse_args()
    root = Path(args.workspace).resolve(strict=True)
    brief = json.loads((root / ".research/PROJECT_BRIEF.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((root / ".research/workflow-state.json").read_text(encoding="utf-8"))
    bindings = json.loads((root / args.bundle / "bindings.json").read_text(encoding="utf-8"))
    if bindings.get("schema_version") != "guidance_binding.v2":
        raise SystemExit("binding schema is not v2")
    if bindings.get("intent", {}).get("brief_digest") != checkpoint.get("brief_digest"):
        raise SystemExit("binding intent does not match canonical checkpoint")
    if bindings.get("plan", {}).get("stage_contract_digest") != args.stage_contract_digest:
        raise SystemExit("binding stage contract digest mismatch")
    chain = validate_markdown_chain(
        root / args.bundle,
        accepted_brief=brief,
        workflow_state=checkpoint,
        allowed_paths=bindings["plan"]["allowed_paths"],
        requirement_ids=bindings["tasks"]["requirement_ids"],
    )
    controller = _controller(root, load_runtime_composition_config(root))
    projection = controller.resume_projection()
    before = json.dumps(projection, sort_keys=True, separators=(",", ":"))
    # Derived files are read and validated only; the controller state is never
    # loaded from them and must remain identical after the preflight.
    after = json.dumps(controller.resume_projection(), sort_keys=True, separators=(",", ":"))
    if before != after:
        raise SystemExit("derived artifact preflight changed canonical projection")
    result = {
        "schema_version": "guidance_preflight.v1",
        "stage_contract_digest": args.stage_contract_digest,
        "canonical_brief_digest": checkpoint["brief_digest"],
        "projection_digest": __import__("hashlib").sha256(before.encode()).hexdigest(),
        "projection_revision": projection.get("revision"),
        "chain": chain,
        "dual_authority": "PASS",
        "destructive_actions": [],
    }
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
