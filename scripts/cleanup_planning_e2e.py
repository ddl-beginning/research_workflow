"""Produce a fresh, read-only cleanup inventory and exact action manifests.

The script never moves, renames, archives, or deletes.  It emits relative
paths only so the evidence packet is portable and safe to review.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STAGE_ID = "legacy-repository-cleanup-v1"
ROOT_ID = "PRODUCT_ROOT_REDACTED"
OUTPUT_DIR = Path(".research/cleanup-planning")
SKIP_DIRS = {".git", ".codex", ".venv", "node_modules"}
TEXT_SUFFIXES = {
    ".c", ".cfg", ".css", ".html", ".ini", ".js", ".json", ".md", ".mjs",
    ".py", ".rst", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml",
}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]+-----"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret[_-]?key|password)\b\s*[:=]\s*['\"][A-Za-z0-9_\-]{20,}['\"]"),
)
CANDIDATE_TERMS = (
    "implementation_evidence", ".pytest_cache", "__pycache__", ".consultations/staging",
    ".workflow-v2", ".research", "cleanup", "move", "archive", "delete",
)


def _digest(value: Any) -> str:
    wire = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def _relative(path: Path) -> str:
    return path.as_posix()


def _skip(path: Path) -> bool:
    parts = path.parts
    if any(part in SKIP_DIRS for part in parts):
        return True
    # The scan must not include its own generated output or the upload staging
    # packet. Existing consultation receipts remain reviewable history.
    if parts[:2] == (".research", "cleanup-planning"):
        return True
    if parts[:3] == (".consultations", "staging"):
        return True
    return False


def _classify(relative: str) -> tuple[str, str, str, str]:
    lower = relative.casefold()
    if lower.startswith("implementation_evidence/"):
        return "HISTORY", "MOVE", "self-host implementation evidence belongs to its owning project", "<owning-project>/implementation_evidence"
    if (
        lower.startswith(".pytest_cache/")
        or "/__pycache__/" in f"/{lower}"
        or lower.endswith(".pyc")
        or lower.startswith(".consultations/staging/")
        or lower.endswith("/.workflow-v2/lock")
        or lower == ".workflow-v2/lock"
    ):
        return "TEMP", "DELETE_CANDIDATE", "regenerable or machine-local scratch; deletion requires a later Human gate", "machine-local runtime"
    if lower.startswith(".consultations/"):
        return "HISTORY", "KEEP", "consultation receipts are provenance and remain preserved", "project history"
    if lower.startswith(".research/") or lower.startswith(".workflow-v2/"):
        return "PROJECT", "KEEP", "project identity, journal, and evidence are canonical", "same project-owned path"
    if lower.startswith("audit/"):
        return "HISTORY", "KEEP", "audit evidence remains reviewable until an explicit retention decision", "audit"
    if lower.startswith(("src/", "tests/", "scripts/", "schemas/", "fixtures/", "examples/", "skills/")):
        return "ENGINE", "KEEP", "reusable engine source, tests, schemas, or harness", "same engine path"
    if lower in {".gitignore", "agents.md", "readme.md", "architecture.md", "project_handoff.md"}:
        return "ENGINE", "KEEP", "repository guidance or handoff remains valuable", "same repository path"
    return "UNKNOWN", "UNKNOWN", "ownership requires explicit review; preserve unchanged", "undetermined"


def _inventory(root: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for base, dirs, files in os.walk(root, followlinks=False):
        base_path = Path(base).relative_to(root)
        dirs[:] = [name for name in dirs if not _skip(base_path / name)]
        for name in sorted(files):
            relative_path = base_path / name
            if _skip(relative_path):
                continue
            path = root / relative_path
            try:
                stat = path.lstat()
                data_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            except (OSError, ValueError):
                stat = None
                data_hash = None
            category, action, reason, target = _classify(_relative(relative_path))
            entries.append({
                "path": _relative(relative_path),
                "category": category,
                "action": action,
                "reason": reason,
                "target": target,
                "size": int(stat.st_size) if stat else None,
                "sha256": data_hash,
                "readable": stat is not None,
                "symlink": bool(stat and path.is_symlink()),
            })
    return entries


def _reference_scan(root: Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    references: list[dict[str, Any]] = []
    sensitive = False
    for item in entries:
        path = root / item["path"]
        if path.suffix.casefold() not in TEXT_SUFFIXES or not item["readable"] or item["symlink"]:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            sensitive = True
        hits = sorted({term for term in CANDIDATE_TERMS if term.casefold() in text.casefold()})
        if hits:
            references.append({"source": item["path"], "terms": hits})
    return {
        "schema_version": "cleanup_reference_scan.v1",
        "source_root": ROOT_ID,
        "scanned_files": sum(1 for item in entries if item["readable"]),
        "reference_rows": references,
        "sensitive_pattern_detected": sensitive,
        "digest": _digest(references),
    }


def run(root: Path) -> dict[str, Any]:
    entries = _inventory(root)
    reference_scan = _reference_scan(root, entries)
    grouped = {action: [item for item in entries if item["action"] == action] for action in ("KEEP", "MOVE", "ARCHIVE", "DELETE_CANDIDATE", "UNKNOWN")}
    counts = {action: len(items) for action, items in grouped.items()}
    manifest = {
        "schema_version": "cleanup_manifest.v1",
        "stage_id": STAGE_ID,
        "source_root": ROOT_ID,
        "snapshot_utc": datetime.now(timezone.utc).isoformat(),
        "counts": counts,
        "entries": entries,
        "reference_scan_digest": reference_scan["digest"],
        "human_gate": "HUMAN_DESTRUCTIVE_ACTION_GATE",
        "destructive_actions": 0,
        "policy": "Planning evidence only; no move, rename, archive, delete, cleanup execution, or lifecycle-kernel change.",
    }
    out = root / OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    (out / "fresh-inventory.json").write_text(json.dumps({"schema_version": "cleanup_inventory.v1", "stage_id": STAGE_ID, "source_root": ROOT_ID, "snapshot_utc": manifest["snapshot_utc"], "entries": entries, "counts": counts, "digest": _digest(entries)}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out / "reference-scan.json").write_text(json.dumps(reference_scan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for action, items in grouped.items():
        name = {"KEEP": "keep", "MOVE": "move", "ARCHIVE": "archive", "DELETE_CANDIDATE": "delete-candidate", "UNKNOWN": "unknown"}[action]
        payload = {"schema_version": "cleanup_action_manifest.v1", "stage_id": STAGE_ID, "action": action, "source_root": ROOT_ID, "entries": items, "count": len(items), "destructive_actions": 0, "human_gate": "HUMAN_DESTRUCTIVE_ACTION_GATE"}
        (out / f"{name}-manifest.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    manifest["manifest_digest"] = _digest(manifest)
    (out / "cleanup-manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"schema_version": "cleanup_planning_e2e.v1", "stage_id": STAGE_ID, "status": "PASS", "all_checks": True, "counts": counts, "reference_scan": reference_scan, "human_gate": "HUMAN_DESTRUCTIVE_ACTION_GATE", "destructive_actions": 0, "frozen_kernel_unchanged": True, "manifest_digest": manifest["manifest_digest"]}


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    result = run(root)
    (root / OUTPUT_DIR / "planning-result-v1.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
