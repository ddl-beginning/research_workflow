"""Deterministic guidance artifacts for the frozen Workflow V2 lifecycle.

The module renders reusable templates and validates bindings. It never writes
the V2 journal, advances a Stage, or treats Markdown as lifecycle authority.
"""
from __future__ import annotations

import hashlib
import json
import re
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .workflow_v2_contracts import canonical_json


class GuidanceArtifactError(ValueError):
    """A bounded fail-closed guidance validation error."""


_ROOT = Path(__file__).resolve().parents[1]
_TEMPLATE_ROOT = _ROOT / "templates"
_TOKEN = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")
_TASK_ID = re.compile(r"^T\d{3}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_GLOB = re.compile(r"[*?\[\]{}]")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def digest_file(path: str | os.PathLike[str]) -> str:
    """Digest exact UTF-8/file bytes used by the derived artifact chain."""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def accepted_intent_digest(
    brief: Mapping[str, Any],
    *,
    workflow_state: Mapping[str, Any] | None = None,
) -> str:
    """Return the accepted intent digest.

    Existing callers without a checkpoint retain the historical value-level
    digest.  A production bundle must pass the canonical checkpoint so that
    the machine authority is explicit and cannot be silently redefined by a
    human-facing JSON copy.
    """

    if not isinstance(brief, Mapping) or brief.get("state") != "APPROVED":
        raise GuidanceArtifactError("accepted Project Brief is required")
    if workflow_state is not None:
        digest = workflow_state.get("brief_digest")
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            raise GuidanceArtifactError("canonical workflow brief_digest is invalid")
        if workflow_state.get("brief_state") not in {None, "APPROVED"}:
            raise GuidanceArtifactError("canonical workflow checkpoint is not approved")
        if workflow_state.get("project_id") not in {None, brief.get("project_id")}:
            raise GuidanceArtifactError("workflow checkpoint project identity differs")
        if workflow_state.get("brief_revision") not in {None, brief.get("revision")}:
            raise GuidanceArtifactError("workflow checkpoint brief revision differs")
        return digest
    return digest_json(brief)


def accepted_intent_binding(
    brief: Mapping[str, Any],
    workflow_state: Mapping[str, Any],
    *,
    source_path: str = ".research/PROJECT_BRIEF.json",
) -> dict[str, Any]:
    """Validate and return the explicit machine-authority identity binding."""

    digest = accepted_intent_digest(brief, workflow_state=workflow_state)
    return {
        "project_id": brief.get("project_id"),
        "brief_revision": brief.get("revision"),
        "brief_digest": digest,
        "source_path": source_path,
    }


def render_template(template_name: str, values: Mapping[str, Any]) -> str:
    """Render one generic template with strict, deterministic tokens."""

    if not isinstance(template_name, str) or Path(template_name).name != template_name:
        raise GuidanceArtifactError("template name must be a single file name")
    path = (_TEMPLATE_ROOT / template_name).resolve()
    try:
        path.relative_to(_TEMPLATE_ROOT.resolve())
    except ValueError as exc:
        raise GuidanceArtifactError("template path escapes Engine templates") from exc
    if not path.is_file():
        raise GuidanceArtifactError("template does not exist")
    text = path.read_text(encoding="utf-8")

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise GuidanceArtifactError(f"missing template value: {key}")
        value = values[key]
        if isinstance(value, (dict, list, tuple)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if not isinstance(value, (str, int, float, bool)):
            raise GuidanceArtifactError(f"template value is not renderable: {key}")
        if isinstance(value, float) and not math.isfinite(value):
            raise GuidanceArtifactError(f"template value is non-finite: {key}")
        return str(value)

    rendered = _TOKEN.sub(replace, text)
    if _TOKEN.search(rendered):
        raise GuidanceArtifactError("unresolved template token")
    return rendered.replace("\r\n", "\n")


def validate_spec_binding(
    spec: Mapping[str, Any],
    accepted_brief: Mapping[str, Any],
    *,
    workflow_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise GuidanceArtifactError("spec must be an object")
    actual = spec.get("accepted_intent_digest")
    expected = accepted_intent_digest(accepted_brief, workflow_state=workflow_state)
    if actual != expected:
        raise GuidanceArtifactError("spec does not bind the accepted Project Brief digest")
    version = spec.get("spec_version")
    if not isinstance(version, int) or version < 1:
        raise GuidanceArtifactError("spec_version must be a positive integer")
    return {
        "accepted_intent_digest": expected,
        "spec_version": version,
        "project_id": accepted_brief.get("project_id"),
        "brief_revision": accepted_brief.get("revision"),
    }


def _safe_relative_path(path: str, field: str) -> str:
    if not isinstance(path, str) or not path or "\x00" in path:
        raise GuidanceArtifactError(f"{field} must be a non-empty relative path")
    value = path.replace("\\", "/")
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value) or value.startswith("//"):
        raise GuidanceArtifactError(f"{field} must be relative")
    if _GLOB.search(value):
        raise GuidanceArtifactError(f"{field} must not contain globs")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise GuidanceArtifactError(f"{field} contains unsafe path components")
    return "/".join(parts)


def _inside_scope(path: str, allowed_paths: Sequence[str]) -> bool:
    checked = _safe_relative_path(path, "path")
    for scope in allowed_paths:
        try:
            root = _safe_relative_path(scope, "allowed scope")
        except GuidanceArtifactError:
            continue
        if checked == root or checked.startswith(root + "/"):
            return True
    return False


def validate_plan_binding(plan: Mapping[str, Any], spec: Mapping[str, Any], *, allowed_paths: Sequence[str]) -> dict[str, Any]:
    if not isinstance(plan, Mapping) or not isinstance(spec, Mapping):
        raise GuidanceArtifactError("plan and spec must be objects")
    if plan.get("spec_digest") != digest_json(spec):
        raise GuidanceArtifactError("plan does not bind the spec digest")
    paths = plan.get("allowed_paths")
    if not isinstance(paths, list) or not paths:
        raise GuidanceArtifactError("plan allowed_paths must be non-empty relative paths")
    checked_paths = [_safe_relative_path(path, "plan allowed_paths") for path in paths]
    if len(set(checked_paths)) != len(checked_paths) or not all(_inside_scope(path, allowed_paths) for path in checked_paths):
        raise GuidanceArtifactError("plan expands the canonical Stage allowed scope")
    if not isinstance(plan.get("plan_version"), int) or plan["plan_version"] < 1:
        raise GuidanceArtifactError("plan_version must be a positive integer")
    return {"spec_digest": plan["spec_digest"], "allowed_paths": checked_paths, "plan_version": plan["plan_version"]}


def validate_tasks_binding(
    tasks: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    allowed_paths: Sequence[str],
    requirement_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(tasks, Mapping) or tasks.get("plan_digest") != digest_json(plan):
        raise GuidanceArtifactError("tasks do not bind the plan digest")
    items = tasks.get("tasks")
    if not isinstance(items, list) or not items:
        raise GuidanceArtifactError("tasks must be a non-empty list")
    ids: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) or not _TASK_ID.fullmatch(str(item.get("id", ""))):
            raise GuidanceArtifactError("each task requires a stable T### id")
        task_id = str(item["id"])
        if task_id in ids:
            raise GuidanceArtifactError("task IDs must be unique")
        ids.add(task_id)
        paths = item.get("paths")
        if not isinstance(paths, list) or not paths:
            raise GuidanceArtifactError("each task requires at least one path")
        checked_paths = [_safe_relative_path(path, "task path") for path in paths]
        if not all(_inside_scope(path, allowed_paths) for path in checked_paths):
            raise GuidanceArtifactError("task path exceeds canonical Stage scope")
        dependencies = item.get("depends_on", [])
        if not isinstance(dependencies, list) or any(dep == task_id for dep in dependencies):
            raise GuidanceArtifactError("task dependency references an unknown ID")
        known = {str(x.get("id")) for x in items if isinstance(x, Mapping)}
        if not all(isinstance(dep, str) and dep in known for dep in dependencies):
            raise GuidanceArtifactError("task dependency references an unknown ID")
        refs = item.get("requirement_refs")
        if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref.strip() for ref in refs):
            raise GuidanceArtifactError("each task requires requirement_refs")
        if requirement_ids is not None and not all(ref in set(requirement_ids) for ref in refs):
            raise GuidanceArtifactError("task requirement_refs must exist in the spec")
    graph = {str(item["id"]): [str(dep) for dep in item.get("depends_on", [])] for item in items}
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(node: str) -> None:
        if node in visiting:
            raise GuidanceArtifactError("task dependencies must be acyclic")
        if node in visited:
            return
        visiting.add(node)
        for dep in graph[node]:
            visit(dep)
        visiting.remove(node)
        visited.add(node)
    for node in graph:
        visit(node)
    if not isinstance(tasks.get("tasks_version"), int) or tasks["tasks_version"] < 1:
        raise GuidanceArtifactError("tasks_version must be a positive integer")
    return {"plan_digest": tasks["plan_digest"], "task_ids": sorted(ids), "tasks_version": tasks["tasks_version"]}


def validate_markdown_chain(
    bundle_dir: str | os.PathLike[str],
    *,
    accepted_brief: Mapping[str, Any],
    workflow_state: Mapping[str, Any],
    allowed_paths: Sequence[str],
    requirement_ids: Sequence[str],
) -> dict[str, Any]:
    """Validate actual spec/plan/tasks bytes and their single digest chain."""

    root = Path(bundle_dir).resolve(strict=True)
    spec_path, plan_path, tasks_path = (root / name for name in ("spec.md", "plan.md", "tasks.md"))
    for path in (spec_path, plan_path, tasks_path):
        if not path.is_file() or path.is_symlink():
            raise GuidanceArtifactError(f"bundle artifact is not a regular file: {path.name}")
    binding = accepted_intent_binding(accepted_brief, workflow_state)
    spec_text, plan_text, tasks_text = (p.read_text(encoding="utf-8") for p in (spec_path, plan_path, tasks_path))
    intent = _header(spec_text, "Accepted intent digest")
    if intent != binding["brief_digest"]:
        raise GuidanceArtifactError("spec Markdown does not bind canonical intent")
    spec_digest, plan_digest, tasks_digest = (digest_file(p) for p in (spec_path, plan_path, tasks_path))
    if _header(plan_text, "Spec reference") != spec_digest:
        raise GuidanceArtifactError("plan Markdown does not bind actual spec bytes")
    if _header(tasks_text, "Spec reference") != spec_digest or _header(tasks_text, "Plan reference") != plan_digest:
        raise GuidanceArtifactError("tasks Markdown does not bind actual upstream bytes")
    for path in allowed_paths:
        _safe_relative_path(path, "allowed scope")
    return {"intent_digest": intent, "spec_digest": spec_digest, "plan_digest": plan_digest, "tasks_digest": tasks_digest, "requirement_ids": sorted(set(requirement_ids))}


def _header(text: str, label: str) -> str:
    match = re.search(r"^" + re.escape(label) + r":\s*`?([^`\r\n]+)`?\s*$", text, flags=re.MULTILINE)
    if not match:
        raise GuidanceArtifactError(f"missing Markdown header: {label}")
    return match.group(1).strip()


def render_closeout(*, journal: Mapping[str, Any], stage_id: str, title: str, evidence: Sequence[str], remaining_gaps: Sequence[str], verification_digest: str | None = None, decision_ref: str | None = None, assessment_ref: str | None = None, integration_operation_id: str | None = None) -> str:
    stages = journal.get("stages", {}) if isinstance(journal, Mapping) else {}
    stage = stages.get(stage_id) if isinstance(stages, Mapping) else None
    if not isinstance(stage, Mapping):
        raise GuidanceArtifactError("canonical Stage is missing")
    if stage.get("status") != "CLOSED":
        raise GuidanceArtifactError("CLOSEOUT cannot claim success before canonical CLOSED state")
    runtime = (journal.get("stage_runtime") or {}).get(stage_id, {}) if isinstance(journal.get("stage_runtime"), Mapping) else {}
    closeout = runtime.get("closeout") if isinstance(runtime, Mapping) else None
    if not isinstance(closeout, Mapping):
        raise GuidanceArtifactError("canonical runtime closeout is required")
    if integration_operation_id is not None and closeout.get("integration_operation_id") != integration_operation_id:
        raise GuidanceArtifactError("closeout integration reference is stale")
    if verification_digest is not None and closeout.get("verification_digest") != verification_digest:
        raise GuidanceArtifactError("closeout verification digest is stale")
    for ref in (decision_ref, assessment_ref):
        if ref is not None and not isinstance(ref, str):
            raise GuidanceArtifactError("closeout references must be text")
    values = {"title": title, "stage_id": stage_id, "stage_status": "CLOSED", "journal_revision": journal.get("revision"), "result": "Canonical Stage CLOSED; see the bound receipts.", "evidence": "\n".join(f"- {x}" for x in evidence), "remaining_gaps": "\n".join(f"- {x}" for x in remaining_gaps)}
    return render_template("CLOSEOUT.md", values)


def render_handoff(*, projection: Mapping[str, Any], links: Mapping[str, str], recent_receipt: str | None = None, blocker: str | None = None, resume_command: str = "workflow_resume", projection_digest: str | None = None, revision: int | None = None) -> str:
    if not isinstance(projection, Mapping) or not projection.get("stage"):
        raise GuidanceArtifactError("canonical projection is required")
    stage = projection["stage"]
    if not isinstance(stage, Mapping):
        raise GuidanceArtifactError("projection stage is invalid")
    if revision is not None and projection.get("revision") != revision:
        raise GuidanceArtifactError("handoff revision is stale")
    if projection_digest is not None and digest_json(projection) != projection_digest:
        raise GuidanceArtifactError("handoff projection digest is stale")
    for name, path in links.items():
        if not isinstance(name, str) or not isinstance(path, str):
            raise GuidanceArtifactError("handoff links must be text")
        _safe_relative_path(path, "handoff link")
    lines = ["# Project Handoff", "", f"Current Stage: `{stage.get('stage_id')}`", f"Canonical status: `{stage.get('status')}`", f"Canonical next action: `{stage.get('next_action')}`"]
    if revision is not None:
        lines.append(f"Canonical revision: `{revision}`")
    if projection_digest is not None:
        lines.append(f"Projection digest: `{projection_digest}`")
    lines.extend(["", "## Current artifacts"])
    lines.extend(f"- {name}: {path}" for name, path in sorted(links.items()))
    lines.append(f"- Recent receipt: {recent_receipt or 'none'}")
    lines.append(f"- Current blocker: {blocker or 'none'}")
    lines.extend(["", f"Resume command: `{resume_command}`", ""])
    return "\n".join(lines)


__all__ = ["GuidanceArtifactError", "accepted_intent_binding", "accepted_intent_digest", "digest_file", "digest_json", "render_closeout", "render_handoff", "render_template", "validate_markdown_chain", "validate_plan_binding", "validate_spec_binding", "validate_tasks_binding"]
