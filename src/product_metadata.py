"""Version and source provenance for the installed Workflow product.

The product version is intentionally defined once in the runtime package and
is mirrored by ``pyproject.toml`` for packaging. Git metadata is best-effort:
source checkouts expose commit/tag identity, while a copied wheel can still
report its installed version without requiring Git.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, Mapping


PRODUCT_NAME = "Research Workflow"
PRODUCT_VERSION = "2.1.7"
RELEASE_TAG = "workflow-v2.1.7-stable"


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def is_engine_checkout(root: str | Path) -> bool:
    """Return whether *root* has the public Engine checkout shape."""

    path = Path(root).expanduser().resolve()
    return all(
        (path / relative).is_file()
        for relative in (
            Path("pyproject.toml"),
            Path("scripts/workflow.py"),
            Path("src/product_workflow_runtime.py"),
        )
    )


def checkout_provenance(root: str | Path) -> dict[str, Any] | None:
    """Read non-secret version/commit facts from an Engine checkout."""

    path = Path(root).expanduser().resolve()
    if not is_engine_checkout(path):
        return None
    version: str | None = None
    try:
        with (path / "pyproject.toml").open("rb") as handle:
            document = tomllib.load(handle)
        project = document.get("project") if isinstance(document, Mapping) else {}
        candidate = project.get("version") if isinstance(project, Mapping) else None
        version = str(candidate) if candidate else None
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        version = None
    commit = _git(path, "rev-parse", "HEAD")
    exact_ref = _git(path, "describe", "--tags", "--exact-match", "HEAD")
    tags = _git(path, "tag", "--points-at", "HEAD")
    stable_tags = [
        item.strip()
        for item in (tags or "").splitlines()
        if item.strip().startswith("workflow-v") and item.strip().endswith("-stable")
    ]
    return {
        "root": path.as_posix(),
        "version": version,
        "commit": commit,
        "source_ref": exact_ref or _git(path, "branch", "--show-current") or commit,
        "stable_tags": stable_tags,
        "dirty": bool(_git(path, "status", "--porcelain")),
    }


def current_provenance(product_root: str | Path) -> dict[str, Any]:
    """Return provenance for the code that is currently executing."""

    root = Path(product_root).expanduser().resolve()
    checkout = checkout_provenance(root)
    return {
        "product": PRODUCT_NAME,
        "version": PRODUCT_VERSION,
        "release_tag": RELEASE_TAG,
        "install_root": root.as_posix(),
        "engine_commit": checkout.get("commit") if checkout else None,
        "source_ref": checkout.get("source_ref") if checkout else None,
        "stable_tags": checkout.get("stable_tags", []) if checkout else [],
        "checkout": checkout,
        "python": sys.executable,
    }


def format_version(product_root: str | Path) -> str:
    """Render a concise, copyable version/provenance report."""

    value = current_provenance(product_root)
    return " | ".join(
        [
            f"{PRODUCT_NAME} {value['version']}",
            f"Install root: {value['install_root']}",
            f"Engine commit: {value['engine_commit'] or 'unknown'}",
            f"Source ref: {value['source_ref'] or 'unknown'}",
        ]
    )


__all__ = [
    "PRODUCT_NAME",
    "PRODUCT_VERSION",
    "RELEASE_TAG",
    "checkout_provenance",
    "current_provenance",
    "format_version",
    "is_engine_checkout",
]
