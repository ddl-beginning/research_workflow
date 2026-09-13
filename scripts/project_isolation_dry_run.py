#!/usr/bin/env python3
"""Read-only current scan and isolation dry-run manifest.

The script never moves, deletes, renames, or writes outside its evidence output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


EXCLUDED = {".git", "__pycache__", ".pytest_cache"}


def portability_projection(records: list[dict[str, object]]) -> dict[str, object]:
    """Validate that the manifest can be replayed from a disposable root.

    This is deliberately an in-memory projection: it never copies, moves, or
    rewrites a project artifact.  A future resolver must satisfy the same
    invariants before any Human-approved relocation.
    """
    paths = [str(item["path"]) for item in records]
    relative_safe = all(
        path and not Path(path).is_absolute() and ".." not in Path(path).parts
        and "\\" not in path and "\x00" not in path
        for path in paths
    )
    unique_paths = len(paths) == len(set(paths))
    replay = {path: str(item["sha256"]) for path, item in zip(paths, records)}
    replay_digest = hashlib.sha256(
        json.dumps(replay, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "project_isolation_portability_projection.v1",
        "relative_paths": "PASS" if relative_safe else "FAIL",
        "unique_paths": "PASS" if unique_paths else "FAIL",
        "symlink_policy": "PASS",
        "disposable_root_projection": "PASS" if relative_safe and unique_paths else "FAIL",
        "resume_projection_digest": replay_digest,
        "mutation": "NONE",
    }


def owner_action(rel: str) -> tuple[str, str, str]:
    top = rel.split("/", 1)[0]
    if rel.startswith(".workflow-v2/"):
        return "MACHINE_RUNTIME", "KEEP", "canonical V2 journal remains machine-local authority"
    if rel.startswith(".research/"):
        return "PROJECT_EVIDENCE", "KEEP", "project evidence and receipts remain readable"
    if rel.startswith(".consultations/"):
        return "MACHINE_STAGING", "MOVE_FUTURE", "machine-local bridge staging requires separate future gate"
    if rel.startswith("implementation_evidence/"):
        return "PROJECT_HISTORY", "KEEP", "implementation history is preserved; no migration authorized"
    if rel.startswith("specs/") or rel.startswith(".specify/") or rel in {"PROJECT_HANDOFF.md"}:
        return "PROJECT_GUIDANCE", "KEEP", "project-owned guidance and derived navigation"
    if top in {"src", "tests", "templates", "schemas", "scripts", "fixtures", "examples", "skills"}:
        return "ENGINE", "KEEP", "engine source, schemas, tests, and reusable tooling"
    return "PROJECT_ROOT", "KEEP", "unclassified root artifact retained pending ownership review"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--output", default=".research/project-isolation/current-dry-run.json")
    args = parser.parse_args()
    root = Path(args.workspace).resolve(strict=True)
    records = []
    counts = {key: 0 for key in ("KEEP", "MOVE_FUTURE", "ARCHIVE", "DELETE_CANDIDATE", "UNKNOWN")}
    dirs = set()
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if any(part in EXCLUDED for part in path.parts):
            continue
        rel = path.relative_to(root).as_posix()
        # Generated dry-run evidence is excluded from the candidate manifest;
        # otherwise the manifest would hash and count itself recursively.
        if rel.startswith(".research/project-isolation/"):
            continue
        if not rel or rel.startswith(".consultations/staging/") and path.is_dir():
            continue
        if path.is_dir():
            dirs.add(rel)
            continue
        if not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        owner, action, rationale = owner_action(rel)
        counts[action] += 1
        total_bytes += len(data)
        records.append({"path": rel, "owner": owner, "proposed_action": action, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "rationale": rationale})
    result = {
        "schema_version": "project_isolation_dry_run.v1",
        "workspace": root.as_posix(),
        "read_only": True,
        "scan_kind": "current_real_time",
        "file_count": len(records),
        "directory_count": len(dirs),
        "total_bytes": total_bytes,
        "counts": counts,
        "future_move_candidates": [r["path"] for r in records if r["proposed_action"] == "MOVE_FUTURE"],
        "unknown_policy": "KEEP_OR_QUARANTINE",
        "destructive_actions": [],
        "records": records,
    }
    result["portability"] = portability_projection(records)
    out = root / args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("file_count", "directory_count", "total_bytes", "counts", "destructive_actions")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
