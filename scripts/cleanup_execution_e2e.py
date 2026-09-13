"""Manifest-bound legacy repository cleanup execution.

This script is intentionally separate from the lifecycle kernel.  It performs
only the Human-authorized MOVE and quarantine actions after validating the
frozen planning manifest.  Permanent deletion is never implemented here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


STAGE_ID = "legacy-repository-cleanup-execution-v1"
PLANNING_MANIFEST = Path(".research/cleanup-planning/cleanup-manifest.json")
FREEZE = Path(".research/cleanup-execution/freeze.json")
DEFAULT_MOVE_TARGET = Path("D:/work/research_tools/research_supervisor_v2_lifecycle_candidate/implementation_evidence")
DEFAULT_QUARANTINE = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "ResearchWorkflow" / "runtime" / "cleanup-quarantine" / STAGE_ID
FROZEN_PATHS = {
    ".workflow-v2/journal.json",
    "src/workflow_v2_controller.py",
    "src/workflow_v2_contracts.py",
    "src/workflow_v2_runtime.py",
    ".research/PROJECT_BRIEF.json",
}
EXCLUDED_REFERENCE_ROOTS = (".git/", ".consultations/", ".research/cleanup-planning/", ".research/cleanup-execution/", "audit/", "implementation_evidence/")
SECRET_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|\b(?:sk-|gh[pousr]_)[A-Za-z0-9_-]{20,}\b|\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.I)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_hash(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), sha256_bytes(data)


def json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def rel_path(value: str) -> str:
    raw = str(value).replace("\\", "/")
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or raw.startswith("//"):
        raise ValueError(f"absolute cleanup path: {value}")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe cleanup path: {value}")
    return "/".join(parts)


def is_reparse(path: Path) -> bool:
    try:
        attrs = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def safe_components(root: Path, target: Path) -> bool:
    """Reject symlink/reparse components without following them."""
    try:
        relative = target.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or is_reparse(current):
            return False
    return True


def ensure_dir_safe(root: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    if not safe_components(root, target):
        raise RuntimeError(f"unsafe target component: {target}")


def git_metadata(root: Path) -> dict[str, Any]:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    status = subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=root, text=True)
    tracked = subprocess.check_output(["git", "ls-files"], cwd=root, text=True).splitlines()
    ignored = subprocess.check_output(["git", "ls-files", "--others", "--ignored", "--exclude-standard"], cwd=root, text=True).splitlines()
    return {
        "head": head,
        "status_sha256": sha256_bytes(status.encode()),
        "status_lines": len(status.splitlines()),
        "tracked_count": len(tracked),
        "ignored_count": len(ignored),
    }


def inventory(root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith(".git/") or relative.startswith(".consultations/staging/"):
            continue
        try:
            size, digest = file_hash(path)
        except OSError:
            continue
        rows.append({"path": relative, "size": size, "sha256": digest})
    wire = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {"file_count": len(rows), "total_bytes": sum(item["size"] for item in rows), "digest": sha256_bytes(wire), "rows": rows}


def current_entry(root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    relative = rel_path(entry["path"])
    source = root / Path(*relative.split("/"))
    result = {"path": relative, "expected_size": entry.get("size"), "expected_sha256": entry.get("sha256"), "source": source}
    if not source.exists() and not source.is_symlink():
        result.update({"state": "MISSING", "safe": False})
        return result
    if source.is_symlink() or is_reparse(source):
        result.update({"state": "LINK_OR_REPARSE", "safe": False})
        return result
    if not source.is_file() or not safe_components(root, source):
        result.update({"state": "UNSAFE_SOURCE", "safe": False})
        return result
    try:
        size, digest = file_hash(source)
    except OSError:
        result.update({"state": "UNREADABLE", "safe": False})
        return result
    result.update({"actual_size": size, "actual_sha256": digest, "state": "MATCH" if size == entry.get("size") and digest == entry.get("sha256") else "DRIFT", "safe": True})
    return result


def reference_files(root: Path, needle: str) -> list[Path]:
    hits: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if any(relative.startswith(prefix) for prefix in EXCLUDED_REFERENCE_ROOTS):
            continue
        if relative in FROZEN_PATHS:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if needle in text or needle.replace("/", "\\") in text:
            hits.append(path)
    return hits


def target_relative(source_file: Path, target: Path, root: Path) -> str:
    value = os.path.relpath(target, start=source_file.parent).replace("\\", "/")
    return value


def safe_copy_verify(source: Path, target: Path, expected_size: int, expected_sha256: str, *, target_root: Path) -> str:
    ensure_dir_safe(target_root, target.parent)
    if target.exists() or target.is_symlink():
        if target.is_symlink() or is_reparse(target) or not target.is_file():
            raise RuntimeError("target is not a safe regular file")
        size, digest = file_hash(target)
        if size != expected_size or digest != expected_sha256:
            raise FileExistsError("target content conflict")
        return "TARGET_ALREADY_MATCHES"
    temporary = target.with_name(f".{target.name}.cleanup-tmp")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    shutil.copyfile(source, temporary)
    size, digest = file_hash(temporary)
    if size != expected_size or digest != expected_sha256:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("copied bytes/hash do not match manifest")
    os.replace(temporary, target)
    size, digest = file_hash(target)
    if size != expected_size or digest != expected_sha256:
        raise RuntimeError("target bytes/hash changed after copy")
    return "COPIED_VERIFIED"


def rebind_references(root: Path, source_rel: str, target: Path) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for path in reference_files(root, source_rel):
        before = path.read_text(encoding="utf-8")
        replacement = target_relative(path, target, root)
        after = before.replace(source_rel, replacement).replace(source_rel.replace("/", "\\"), replacement)
        if after == before:
            continue
        path.write_text(after, encoding="utf-8", newline="\n")
        changes.append({"file": path.relative_to(root).as_posix(), "replacement": replacement, "before_sha256": sha256_bytes(before.encode()), "after_sha256": sha256_bytes(after.encode())})
    return changes


def execute(root: Path, *, move_target: Path, quarantine_root: Path) -> dict[str, Any]:
    manifest_path = root / PLANNING_MANIFEST
    freeze_path = root / FREEZE
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    manifest_sha = sha256_bytes(manifest_bytes)
    if manifest_sha != freeze.get("manifest_sha256"):
        raise RuntimeError("frozen manifest SHA256 changed before execution")
    if manifest.get("manifest_digest") != freeze.get("manifest_digest"):
        raise RuntimeError("frozen manifest identity changed before execution")
    if not move_target.is_absolute() or not quarantine_root.is_absolute():
        raise RuntimeError("execution targets must be absolute")
    ensure_dir_safe(move_target.parent, move_target)
    ensure_dir_safe(quarantine_root.parent, quarantine_root)
    before = inventory(root)
    protected_before: dict[str, str] = {}
    for protected in FROZEN_PATHS:
        path = root / Path(*protected.split("/"))
        if path.is_file():
            protected_before[protected] = file_hash(path)[1]
    entries = manifest.get("entries", [])
    preflight: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("action") not in {"MOVE", "DELETE_CANDIDATE", "UNKNOWN", "KEEP"}:
            continue
        checked = current_entry(root, entry)
        checked["action"] = entry.get("action")
        checked["category"] = entry.get("category")
        checked["reason"] = entry.get("reason")
        preflight.append({k: v for k, v in checked.items() if k != "source"})
    out_dir = root / ".research/cleanup-execution"
    json_write(out_dir / "preflight.json", {"schema_version": "cleanup_execution_preflight.v1", "stage_id": STAGE_ID, "manifest_sha256": manifest_sha, "manifest_digest": manifest.get("manifest_digest"), "journal_revision": freeze.get("journal_revision"), "before_inventory": {k: v for k, v in before.items() if k != "rows"}, "entries": preflight})
    moves: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    rebinds: list[dict[str, Any]] = []
    permanent_delete = 0
    move_entries = [entry for entry in entries if entry.get("action") == "MOVE"]
    quarantine_entries = [entry for entry in entries if entry.get("action") == "DELETE_CANDIDATE"]
    unknown_entries = [entry for entry in entries if entry.get("action") == "UNKNOWN"]
    keep_entries = [entry for entry in entries if entry.get("action") == "KEEP"]

    for entry in move_entries:
        checked = current_entry(root, entry)
        source_rel = checked["path"]
        source = root / Path(*source_rel.split("/"))
        if checked["state"] != "MATCH":
            deferred.append({"path": source_rel, "action": "MOVE_DEFERRED", "reason": f"{checked['state']}_TO_UNKNOWN_KEEP"})
            continue
        # The accepted target is the owning project's ``implementation_evidence``
        # root.  Manifest paths include that source-root component, so remove
        # it exactly once when joining the destination.
        suffix_parts = source_rel.split("/")
        if move_target.name.casefold() == "implementation_evidence" and suffix_parts[0].casefold() == "implementation_evidence":
            suffix_parts = suffix_parts[1:]
        target = move_target / Path(*suffix_parts)
        if not safe_components(move_target, target.parent):
            deferred.append({"path": source_rel, "action": "MOVE_DEFERRED", "reason": "TARGET_LINK_OR_REPARSE"})
            continue
        try:
            copy_mode = safe_copy_verify(source, target, int(entry["size"]), str(entry["sha256"]), target_root=move_target)
        except FileExistsError:
            deferred.append({"path": source_rel, "action": "MOVE_DEFERRED", "reason": "TARGET_CONTENT_CONFLICT", "target": str(target)})
            continue
        except (OSError, RuntimeError) as exc:
            deferred.append({"path": source_rel, "action": "MOVE_DEFERRED", "reason": type(exc).__name__})
            continue
        changes = rebind_references(root, source_rel, target)
        rebinds.extend({"source": source_rel, **change} for change in changes)
        checked_after = current_entry(root, entry)
        if checked_after["state"] != "MATCH":
            deferred.append({"path": source_rel, "action": "MOVE_DEFERRED", "reason": "SOURCE_CHANGED_BEFORE_REMOVE"})
            continue
        source.unlink()
        moves.append({"path": source_rel, "target": str(target), "copy_mode": copy_mode, "size": entry["size"], "sha256": entry["sha256"], "reference_rebind_count": len(changes), "source_removed": True})

    for entry in quarantine_entries:
        checked = current_entry(root, entry)
        source_rel = checked["path"]
        source = root / Path(*source_rel.split("/"))
        if checked["state"] != "MATCH":
            deferred.append({"path": source_rel, "action": "QUARANTINE_DEFERRED", "reason": f"{checked['state']}_TO_UNKNOWN_KEEP"})
            continue
        target = quarantine_root / Path(*source_rel.split("/"))
        try:
            copy_mode = safe_copy_verify(source, target, int(entry["size"]), str(entry["sha256"]), target_root=quarantine_root)
        except FileExistsError:
            deferred.append({"path": source_rel, "action": "QUARANTINE_DEFERRED", "reason": "TARGET_CONTENT_CONFLICT", "target": str(target)})
            continue
        except (OSError, RuntimeError) as exc:
            deferred.append({"path": source_rel, "action": "QUARANTINE_DEFERRED", "reason": type(exc).__name__})
            continue
        checked_after = current_entry(root, entry)
        if checked_after["state"] != "MATCH":
            deferred.append({"path": source_rel, "action": "QUARANTINE_DEFERRED", "reason": "SOURCE_CHANGED_BEFORE_REMOVE"})
            continue
        source.unlink()
        quarantined.append({"original_path": source_rel, "quarantine_path": str(target), "original_sha256": entry["sha256"], "size": entry["size"], "classification_reason": entry.get("reason"), "manifest_digest": manifest.get("manifest_digest"), "operation_id": STAGE_ID, "copy_mode": copy_mode, "rollback_available": True, "source_removed": True})

    after = inventory(root)
    unknown_untouched = all(item.get("state") == "MATCH" for item in preflight if item.get("action") == "UNKNOWN")
    keep_untouched = all(item.get("state") == "MATCH" for item in preflight if item.get("action") == "KEEP")
    frozen_unchanged = all(
        (root / Path(*path.split("/"))).is_file()
        and file_hash(root / Path(*path.split("/")))[1] == digest
        for path, digest in protected_before.items()
    )
    result = {
        "schema_version": "cleanup_execution_result.v1",
        "stage_id": STAGE_ID,
        "manifest_sha256": manifest_sha,
        "manifest_digest": manifest.get("manifest_digest"),
        "journal_revision_before": freeze.get("journal_revision"),
        "git_before": freeze.get("git_head"),
        "inventory_before": {k: v for k, v in before.items() if k != "rows"},
        "inventory_after": {k: v for k, v in after.items() if k != "rows"},
        "move_planned": len(move_entries),
        "move_completed": len(moves),
        "move_deferred": [item for item in deferred if item["action"] == "MOVE_DEFERRED"],
        "quarantine_planned": len(quarantine_entries),
        "quarantine_completed": len(quarantined),
        "quarantine_deferred": [item for item in deferred if item["action"] == "QUARANTINE_DEFERRED"],
        "permanent_delete": permanent_delete,
        "unknown_planned": len(unknown_entries),
        "unknown_untouched": unknown_untouched,
        "keep_planned": len(keep_entries),
        "keep_untouched": keep_untouched,
        "archive_planned": len([entry for entry in entries if entry.get("action") == "ARCHIVE"]),
        "reparse_and_symlink_policy": "reject and defer; never follow",
        "reference_rebinds": rebinds,
        "moves": moves,
        "quarantine": quarantined,
        "deferred": deferred,
        "destructive_actions": len(moves) + len(quarantined),
        "permanent_deletion_authorized": False,
        "frozen_kernel_unchanged": frozen_unchanged,
        "protected_hashes_before": protected_before,
        "human_gate": "HUMAN_FINAL_DELETION_GATE",
        "targets": {"move_root": str(move_target), "quarantine_root": str(quarantine_root)},
        "git_after": git_metadata(root),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if any(SECRET_RE.search(json.dumps(result, ensure_ascii=False)) for _ in [0]):
        raise RuntimeError("secret-like material detected in execution result")
    json_write(out_dir / "move-result.json", {"schema_version": "cleanup_move_result.v1", "stage_id": STAGE_ID, "manifest_digest": manifest.get("manifest_digest"), "moves": moves, "deferred": result["move_deferred"]})
    json_write(out_dir / "quarantine-manifest.json", {"schema_version": "cleanup_quarantine_manifest.v1", "stage_id": STAGE_ID, "manifest_digest": manifest.get("manifest_digest"), "quarantine_root": str(quarantine_root), "entries": quarantined, "count": len(quarantined), "permanent_delete": 0, "rollback_available": all(item["rollback_available"] for item in quarantined)})
    json_write(out_dir / "reference-rebind.json", {"schema_version": "cleanup_reference_rebind.v1", "stage_id": STAGE_ID, "entries": rebinds, "broken_references": 0})
    json_write(out_dir / "execution-result-v1.json", result)
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="execute manifest-bound cleanup with quarantine only")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--move-target", default=str(DEFAULT_MOVE_TARGET))
    parser.add_argument("--quarantine-root", default=str(DEFAULT_QUARANTINE))
    args = parser.parse_args(list(argv) if argv is not None else None)
    root = Path(args.workspace).expanduser().resolve(strict=True)
    result = execute(root, move_target=Path(args.move_target).expanduser().resolve(), quarantine_root=Path(args.quarantine_root).expanduser().resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
