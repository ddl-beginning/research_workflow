"""One-shot, identity-first entry point for the existing workflow.

The entry point is intentionally a read-only resolver.  It does not own a
workflow checkpoint, a Stage state machine, a capability registry, or an
agent framework.  It reads the durable artifacts that already exist, derives
the current lifecycle position, and returns one transient routing result.

Paths supplied by a caller are locators only.  A workspace becomes
authoritative only after its project/workflow identity and canonical runtime
evidence agree with the project handoff and with one another.  This keeps a
project checkout and a disposable ``.tmp`` runtime workspace distinct while
still allowing an existing handoff to point from one to the other.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .contracts import ContractValidationError, canonical_json, sha256_json
from .contract_integration import CAPABILITIES, CAPABILITY_OWNERS, canonical_capability_contracts, resolve_canonical_contract


ENTRY_SCHEMA_VERSION = "unified_workflow_entry.v1"
ENTRY_OUTCOMES = frozenset({"ROUTE", "HUMAN_INPUT_REQUIRED", "BLOCKED"})
ENTRY_MODES = frozenset({"NEW", "RESUME"})

_BINDING_FIELDS = ("schema_version", "project_id", "workflow_id", "project_anchor", "workspace_ref")


def bind_workflow_workspace(project_anchor: str | os.PathLike[str], workspace: str | os.PathLike[str]) -> dict[str, Any]:
    """Generate the existing handoff/runtime binding from real runtime identity.

    This does not create a Workflow. Call only after the existing Runtime has
    created and verified its checkpoint. The binding has no lifecycle fields.
    """
    anchor, runtime = Path(project_anchor).resolve(strict=True), Path(workspace).resolve(strict=True)
    brief = json.loads((runtime / ".research/PROJECT_BRIEF.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((runtime / ".research/workflow-state.json").read_text(encoding="utf-8"))
    identity = brief["project_id"]
    if checkpoint.get("project_id") != identity:
        raise UnifiedEntryError("PROJECT_IDENTITY_CONFLICT", "runtime identity differs from brief")
    body = {"schema_version": "workflow_identity_binding.v1", "project_id": identity,
            "workflow_id": checkpoint.get("workflow_id") or f"workflow-{identity}",
            "project_anchor": anchor.as_posix(), "workspace_ref": runtime.as_posix()}
    body["binding_digest"] = sha256_json(body)
    for target in (runtime / ".research/workflow-identity.json", anchor / "PROJECT_HANDOFF.json"):
        if target.is_file():
            old = json.loads(target.read_text(encoding="utf-8"))
            if old.get("project_id") not in (None, identity) or old.get("workflow_id") not in (None, body["workflow_id"]):
                raise UnifiedEntryError("WORKFLOW_IDENTITY_CONFLICT", "existing binding belongs to another workflow")
    for target in (runtime / ".research/workflow-identity.json", anchor / "PROJECT_HANDOFF.json"):
        encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if target.is_file() and target.read_text(encoding="utf-8") == encoded:
            continue
        pending = target.with_suffix(target.suffix + ".pending")
        pending.write_text(encoded, encoding="utf-8")
        pending.replace(target)
    return body

# These are the ownership labels used by the existing canonical capability
# model.  They are only a compatibility fallback for the current PoC, whose
# capability list predates explicit owner metadata.  When metadata contains
# an owner, the metadata always wins; this is not a second capability table.

_WORKSPACE_KEYS = frozenset(
    {
        "workspace_ref",
        "workspace_root",
        "authoritative_workspace",
        "authoritative_workspace_ref",
        "runtime_workspace",
        "runtime_workspace_ref",
        "runtime_workspace_path",
        "successful_runtime_workspace",
        "running_workspace",
        "repository_root",
    }
)
_PROJECT_KEYS = frozenset({"project_id", "project_identity", "project_ref"})
_WORKFLOW_KEYS = frozenset(
    {"workflow_id", "workflow_identity", "workflow_ref", "workflow_key", "workflow"}
)
_GOAL_KEYS = frozenset(
    {
        "goal",
        "project_goal",
        "workflow_goal",
        "task",
        "objective",
        "desired_outcome",
        "project_final_goal",
    }
)
_TERMINAL_STATES = frozenset({"CLOSED", "COMPLETE", "COMPLETED", "TERMINAL"})
_SUCCESS_STATES = frozenset({"APPROVED", "STAGE_READY", "COMPLETE", "COMPLETED", "SUCCEEDED"})
_MAX_ARTIFACT_BYTES = 512 * 1024
_MAX_TEXT = 4000


class UnifiedEntryError(RuntimeError):
    """A bounded, fail-closed entry error.

    Normal semantic ambiguity is represented as a ``HUMAN_INPUT_REQUIRED``
    result.  This exception is reserved for invalid API inputs that cannot
    be represented as a routing result at all.
    """

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.details = copy.deepcopy(dict(details or {}))
        super().__init__(message)


# Compatibility aliases make this small seam easy to adopt from integrations
# that use either ``EntryError`` or the longer product-facing name.
EntryError = UnifiedEntryError
UnifiedWorkflowEntryError = UnifiedEntryError


@dataclass(frozen=True)
class EntryRequest:
    """Normalized human input accepted by :func:`resolve_unified_entry`."""

    project_anchor: str | os.PathLike[str]
    intent: str = "continue"
    goal: str | None = None
    project_id: str | None = None
    workflow_ref: str | None = None
    workspace_ref: str | os.PathLike[str] | None = None


@dataclass
class _Source:
    name: str
    payload: Mapping[str, Any] | str
    base: Path
    kind: str = "metadata"


@dataclass
class _Candidate:
    path: Path
    source_names: list[str]
    payloads: list[Mapping[str, Any]]
    project_ids: list[str]
    workflow_refs: list[str]
    goals: list[str]
    workspace_refs: list[Path]
    evidence: dict[str, Mapping[str, Any]]
    status: str = "INVALID"
    code: str = "BINDING_INCOMPLETE"
    reason: str = "candidate has not been validated"
    superseded_by: str | None = None

    def bounded_view(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "workspace_ref": self.path.as_posix(),
            "sources": list(dict.fromkeys(self.source_names)),
            "status": self.status,
            "code": self.code,
        }
        if self.project_ids:
            result["project_id"] = self.project_ids[0]
        if self.workflow_refs:
            result["workflow_ref"] = self.workflow_refs[0]
        if self.reason:
            result["reason"] = self.reason
        return result


def _bounded_text(value: Any, field: str, *, required: bool = True, maximum: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        raise UnifiedEntryError("ENTRY_INPUT_INVALID", f"{field} must be text")
    text = value.strip()
    if required and not text:
        raise UnifiedEntryError("ENTRY_INPUT_INVALID", f"{field} must not be empty")
    if len(text) > maximum or "\x00" in text:
        raise UnifiedEntryError("ENTRY_INPUT_INVALID", f"{field} is outside the bounded range")
    return text


def _safe_copy(value: Any, *, depth: int = 0) -> Any:
    """Copy bounded metadata without retaining transport material."""

    if depth > 12:
        raise UnifiedEntryError("ENTRY_METADATA_INVALID", "metadata nesting is too deep")
    sensitive = {
        "cookie",
        "cookies",
        "token",
        "tokens",
        "authorization",
        "password",
        "secret",
        "prompt",
        "raw_response",
        "raw_dom",
        "dom",
        "transcript",
        "messages",
        "turns",
        "chat_history",
    }
    if isinstance(value, Mapping):
        return {
            str(key): _safe_copy(child, depth=depth + 1)
            for key, child in value.items()
            if str(key).strip().casefold().replace("-", "_") not in sensitive
        }
    if isinstance(value, list):
        if len(value) > 512:
            raise UnifiedEntryError("ENTRY_METADATA_INVALID", "metadata list is too large")
        return [_safe_copy(item, depth=depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_safe_copy(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        if len(value) > _MAX_TEXT * 8 or "\x00" in value:
            raise UnifiedEntryError("ENTRY_METADATA_INVALID", "metadata text is outside the bounded range")
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return copy.deepcopy(value)
    raise UnifiedEntryError("ENTRY_METADATA_INVALID", "metadata contains an unsupported value")


def _normal_key(value: Any) -> str:
    return str(value).strip().casefold().replace("-", "_").replace(" ", "_")


def _clean_ref(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().strip("`").strip()
    return text or None


def _identity_scalar(value: Any, *, field: str) -> str | None:
    """Extract an ID from the small identity objects used by the PoC."""

    if isinstance(value, str):
        text = _clean_ref(value)
        return text[:_MAX_TEXT] if text else None
    if isinstance(value, Mapping):
        keys = {
            "project": ("project_id", "id", "ref", "key"),
            "workflow": ("workflow_id", "id", "ref", "key", "name"),
        }.get(field, ("id", "ref", "key"))
        for key in keys:
            if key in value:
                extracted = _identity_scalar(value[key], field=field)
                if extracted:
                    return extracted
    return None


def _path_scalar(value: Any) -> str | None:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if isinstance(value, str):
        return _clean_ref(value)
    if isinstance(value, Mapping):
        for key in (
            "path",
            "workspace_ref",
            "workspace_root",
            "runtime_workspace",
            "runtime_workspace_path",
            "repository_root",
            "ref",
        ):
            if key in value:
                extracted = _path_scalar(value[key])
                if extracted:
                    return extracted
    return None


def _source_fields(source: _Source) -> dict[str, Any]:
    """Extract only identity/lifecycle hints from one bounded source."""

    payload = source.payload
    if isinstance(payload, str):
        return _markdown_fields(payload)
    fields: dict[str, Any] = {
        "project_ids": [],
        "workflow_refs": [],
        "workspace_refs": [],
        "goals": [],
        "statuses": [],
        "superseded_by": [],
    }

    def add_unique(name: str, value: Any) -> None:
        if isinstance(value, str) and value and value not in fields[name]:
            fields[name].append(value)

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(value, Mapping):
            for raw_key, child in value.items():
                key = _normal_key(raw_key)
                if key in {"project_id", "project_ref"}:
                    item = _identity_scalar(child, field="project")
                    if item:
                        add_unique("project_ids", item)
                elif key == "project_identity":
                    item = _identity_scalar(child, field="project")
                    if item:
                        add_unique("project_ids", item)
                    if isinstance(child, Mapping):
                        path = _path_scalar(child.get("repository_root"))
                        if path:
                            add_unique("workspace_refs", path)
                elif key in _WORKFLOW_KEYS:
                    item = _identity_scalar(child, field="workflow")
                    if item:
                        add_unique("workflow_refs", item)
                elif key in _WORKSPACE_KEYS:
                    path = _path_scalar(child)
                    if path:
                        add_unique("workspace_refs", path)
                elif key in _GOAL_KEYS:
                    if isinstance(child, str) and child.strip():
                        add_unique("goals", child.strip())
                elif key in {"status", "state", "phase", "workflow_status", "stage_status"}:
                    if isinstance(child, str) and child.strip():
                        add_unique("statuses", child.strip().upper())
                elif key in {"superseded_by", "supersedes", "replaced_by"}:
                    item = _identity_scalar(child, field="workflow")
                    if item:
                        add_unique("superseded_by", item)
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:512]:
                walk(child, depth + 1)

    walk(payload)
    return fields


def _normalised_source_fields(source: _Source) -> dict[str, Any]:
    """Return source fields with locator paths resolved against that source."""

    fields = _source_fields(source)
    resolved: list[str] = []
    for raw in fields.get("workspace_refs", []):
        try:
            resolved_path = _resolve_path(raw, base=source.base, field="workspace_ref")
        except UnifiedEntryError:
            continue
        resolved.append(resolved_path.as_posix())
    fields["workspace_refs"] = resolved
    return fields


def _markdown_fields(markdown: str) -> dict[str, Any]:
    """Read identity labels from handoff prose without trusting prose paths."""

    fields: dict[str, Any] = {
        "project_ids": [],
        "workflow_refs": [],
        "workspace_refs": [],
        "goals": [],
        "statuses": [],
        "superseded_by": [],
    }

    def add(name: str, value: str) -> None:
        cleaned = _clean_ref(value)
        if cleaned and cleaned not in fields[name]:
            fields[name].append(cleaned)

    # Labels are deliberately explicit.  A random path in prose is never a
    # candidate; only a handoff/runtime label can contribute a locator.
    patterns: tuple[tuple[str, str], ...] = (
        (
            "project_ids",
            r"(?im)^\s*(?:[-*]\s*)?(?:project[_ ]?id|项目\s*(?:ID|id))\s*[:：=]\s*`?([^`\r\n]+)",
        ),
        (
            "workflow_refs",
            r"(?im)^\s*(?:[-*]\s*)?(?:workflow[_ ]?(?:id|identity|ref)|工作流\s*(?:ID|身份|引用))\s*[:：=]\s*`?([^`\r\n]+)",
        ),
        (
            "workspace_refs",
            r"(?im)^\s*(?:[-*]\s*)?(?:workspace[_ ]?ref|authoritative[_ ]workspace|runtime[_ ]workspace|成功运行工作区|当前真实运行工作区|真实运行工作区)\s*[:：=]\s*`?([^`\r\n]+)",
        ),
        (
            "workspace_refs",
            r"(?im)^\s*[-*]?\s*(?:[-\w ]*workspace|工作区)[^:\r\n]*[:：]\s*`?([^`\r\n]+)",
        ),
        (
            "goals",
            r"(?im)^\s*(?:[-*]\s*)?(?:goal|workflow[_ ]goal|task|objective|目标)\s*[:：=]\s*`?([^`\r\n]+)",
        ),
    )
    for name, pattern in patterns:
        for match in re.finditer(pattern, markdown):
            add(name, match.group(1).strip())
    return fields


def _resolve_path(value: Any, *, base: Path, field: str = "path", must_exist: bool = False) -> Path:
    raw = _path_scalar(value)
    if raw is None:
        raise UnifiedEntryError("ENTRY_PATH_INVALID", f"{field} must contain a path")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise UnifiedEntryError("ENTRY_PATH_INVALID", f"{field} cannot be resolved") from exc
    return resolved


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
    except (OSError, RuntimeError):
        return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _read_json_file(path: Path, *, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise UnifiedEntryError("ENTRY_ARTIFACT_INVALID", f"{label} is not a regular file")
    try:
        if path.stat().st_size > _MAX_ARTIFACT_BYTES:
            raise UnifiedEntryError("ENTRY_ARTIFACT_TOO_LARGE", f"{label} exceeds the bounded size")
        value = json.loads(path.read_text(encoding="utf-8"))
    except UnifiedEntryError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UnifiedEntryError("ENTRY_ARTIFACT_INVALID", f"{label} cannot be read") from exc
    if not isinstance(value, Mapping):
        raise UnifiedEntryError("ENTRY_ARTIFACT_INVALID", f"{label} must contain an object")
    return _safe_copy(value)


def _source_from_value(
    value: Any,
    *,
    name: str,
    base: Path,
    kind: str = "metadata",
) -> _Source:
    if isinstance(value, Mapping):
        return _Source(name=name, payload=_safe_copy(value), base=base, kind=kind)
    if isinstance(value, (str, os.PathLike)):
        path = _resolve_path(value, base=base, field=name)
        if path.suffix.casefold() in {".md", ".markdown", ".txt"}:
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise UnifiedEntryError("ENTRY_ARTIFACT_INVALID", f"{name} cannot be read") from exc
            if len(text.encode("utf-8")) > _MAX_ARTIFACT_BYTES:
                raise UnifiedEntryError("ENTRY_ARTIFACT_TOO_LARGE", f"{name} exceeds the bounded size")
            return _Source(name=name, payload=text, base=path.parent, kind=kind)
        return _Source(name=name, payload=_read_json_file(path, label=name), base=path.parent, kind=kind)
    raise UnifiedEntryError("ENTRY_METADATA_INVALID", f"{name} must be a mapping or path")


def _load_anchor_sources(anchor: Path) -> list[_Source]:
    """Read only named handoff/runtime files from an explicit anchor."""

    names = (
        "PROJECT_HANDOFF.json",
        "LOCAL_BRIDGE_HANDOFF.json",
        "LOCAL_BRIDGE_HANDOFF.md",
        ".research/PROJECT_HANDOFF.json",
        ".research/workflow-identity.json",
        ".research/workflow-metadata.json",
        ".research/runtime-metadata.json",
        ".research/runtime-identity.json",
        ".research/runtime-composition.json",
    )
    sources: list[_Source] = []
    for relative in names:
        path = anchor / Path(*relative.split("/"))
        if not path.is_file() or path.is_symlink():
            continue
        try:
            sources.append(_source_from_value(path, name=relative, base=anchor, kind="handoff"))
        except UnifiedEntryError as exc:
            # A named but malformed authority document is evidence of a
            # broken binding.  Keep the failure visible to the caller rather
            # than silently treating the anchor as a new project.
            source = _Source(name=relative, payload={"_entry_error": exc.code}, base=path.parent, kind="handoff")
            sources.append(source)
    return sources


def _load_workspace_sources(path: Path) -> tuple[list[_Source], dict[str, Mapping[str, Any]]]:
    """Load the known canonical artifacts below one explicitly named path."""

    relative_names = (
        (".research/PROJECT_BRIEF.json", "project_brief"),
        (".research/workflow-state.json", "workflow_state"),
        (".research/stage-state.json", "stage_state"),
        (".research/DESIGN_PACKAGE.json", "design_package"),
        (".research/execution-handoff/EXECUTION_HANDOFF.json", "execution_handoff"),
        (".research/reviews/STAGE_CLOSEOUT.json", "stage_closeout"),
        (".research/reviews/index.json", "review_index"),
        (".research/workflow-identity.json", "workflow_identity"),
        (".research/workflow-metadata.json", "workflow_metadata"),
        (".research/runtime-metadata.json", "runtime_metadata"),
        (".research/runtime-identity.json", "runtime_identity"),
    )
    sources: list[_Source] = []
    evidence: dict[str, Mapping[str, Any]] = {}
    for relative, key in relative_names:
        file_path = path / Path(*relative.split("/"))
        if not file_path.is_file() or file_path.is_symlink():
            continue
        try:
            source = _source_from_value(file_path, name=relative, base=path, kind="runtime")
        except UnifiedEntryError:
            # Preserve a bounded invalid marker so the candidate is rejected
            # without exposing file contents or turning it into NEW.
            sources.append(_Source(name=relative, payload={"_entry_error": "ENTRY_ARTIFACT_INVALID"}, base=file_path.parent, kind="runtime"))
            continue
        sources.append(source)
        if isinstance(source.payload, Mapping):
            evidence[key] = source.payload
    return sources, evidence


def _normalise_intent(value: Any) -> str:
    if not isinstance(value, str):
        raise UnifiedEntryError("ENTRY_INPUT_INVALID", "intent must be text")
    normalized = value.strip().casefold().replace("_", "-")
    if normalized in {"continue", "resume", "reopen", "继续", "恢复", "接着"}:
        return "CONTINUE"
    if normalized in {"new", "start", "create", "新建", "开始", "新的任务"}:
        return "NEW"
    raise UnifiedEntryError("ENTRY_INPUT_INVALID", "intent must be continue/resume or new")


def _collect_identity(fields: Iterable[dict[str, Any]], *, base: Path) -> tuple[list[str], list[str], list[str], list[Path], str | None]:
    project_ids: list[str] = []
    workflows: list[str] = []
    goals: list[str] = []
    paths: list[Path] = []
    superseded_by: str | None = None
    for item in fields:
        for key, target in (("project_ids", project_ids), ("workflow_refs", workflows), ("goals", goals)):
            for value in item.get(key, []):
                if isinstance(value, str) and value not in target:
                    target.append(value)
        if superseded_by is None:
            values = item.get("superseded_by", [])
            if values and isinstance(values[0], str):
                superseded_by = values[0]
        for raw in item.get("workspace_refs", []):
            try:
                path = _resolve_path(raw, base=base, field="workspace_ref")
            except UnifiedEntryError:
                continue
            if not any(_same_path(path, existing) for existing in paths):
                paths.append(path)
    return project_ids, workflows, goals, paths, superseded_by


def _candidate_for_path(
    path: Path,
    *,
    sources: Sequence[_Source],
    anchor: Path,
    candidate_metadata: Mapping[str, Any] | None = None,
) -> _Candidate:
    source_names = [source.name for source in sources]
    workspace_sources, evidence = _load_workspace_sources(path) if path.is_dir() else ([], {})
    source_list = list(sources) + list(workspace_sources)
    if candidate_metadata is not None:
        source_list.append(_Source(name="explicit_candidate", payload=_safe_copy(candidate_metadata), base=anchor, kind="explicit"))
    # A handoff may describe more than one candidate.  Bind each source to
    # the candidate it names; otherwise a valid A plus a valid B would look
    # like a false path conflict when validating either one.
    selected_sources: list[_Source] = []
    field_items: list[dict[str, Any]] = []
    for source in source_list:
        fields = _normalised_source_fields(source)
        if candidate_metadata is not None and _source_contains_candidate_list(source.payload):
            # The per-candidate mapping is attached separately below.  The
            # wrapper's aggregate identity fields (A and B together) must not
            # create a synthetic conflict inside either individual candidate.
            continue
        source_paths = fields.get("workspace_refs", [])
        if source_paths:
            if not any(_same_path(path, Path(item)) for item in source_paths):
                continue
        selected_sources.append(source)
        field_items.append(fields)
    payloads: list[Mapping[str, Any]] = [source.payload for source in selected_sources if isinstance(source.payload, Mapping)]

    # Test/adaptor callers may provide the canonical state in a single
    # runtime-metadata envelope instead of materializing files.  Treat those
    # fields exactly like the corresponding on-disk artifacts; this remains a
    # read-only projection and does not create an alternate checkpoint.
    embedded_keys = {
        "project_brief",
        "workflow_state",
        "stage_state",
        "design_package",
        "execution_handoff",
        "stage_closeout",
        "review",
        "technical_review",
        "integration",
        "verification",
    }
    for source in selected_sources:
        if not isinstance(source.payload, Mapping) or source.kind not in {"runtime", "explicit"}:
            continue
        payload = source.payload
        for container_name in ("authoritative_state", "state", "evidence"):
            container = payload.get(container_name)
            if isinstance(container, Mapping):
                for key in embedded_keys:
                    if isinstance(container.get(key), Mapping):
                        evidence[key] = container[key]
        for key in embedded_keys:
            if isinstance(payload.get(key), Mapping):
                evidence[key] = payload[key]
        if any(key in payload for key in ("phase", "brief_state", "design_review_state", "stage_status", "research_complete")):
            evidence.setdefault("workflow_state", payload)
    project_ids, workflow_refs, goals, workspace_refs, superseded_by = _collect_identity(field_items, base=anchor)
    # The candidate path itself is a locator, never an authority.  It is only
    # added as a comparison point after a caller has explicitly supplied it.
    for source in workspace_sources:
        if source in selected_sources:
            source_names.append(source.name)

    candidate = _Candidate(
        path=path,
        source_names=source_names,
        payloads=payloads,
        project_ids=project_ids,
        workflow_refs=workflow_refs,
        goals=goals,
        workspace_refs=workspace_refs,
        evidence=evidence,
        superseded_by=superseded_by,
    )

    if not path.is_dir():
        candidate.code = "WORKSPACE_NOT_FOUND"
        candidate.reason = "workspace locator does not name an existing directory"
        return candidate
    for payload in payloads:
        if isinstance(payload, Mapping) and "binding_digest" in payload:
            identity_body = {key: payload.get(key) for key in _BINDING_FIELDS}
            if sha256_json(identity_body) != payload["binding_digest"]:
                candidate.code = "BINDING_DIGEST_MISMATCH"
                candidate.status = "CONFLICTING"
                candidate.reason = "handoff identity binding was changed without matching provenance"
                return candidate
    if any(isinstance(payload, Mapping) and payload.get("_entry_error") for payload in payloads):
        candidate.code = "AUTHORITATIVE_ARTIFACT_INVALID"
        candidate.reason = "a named authoritative artifact could not be read"
        return candidate
    if len(set(project_ids)) > 1:
        candidate.status = "CONFLICTING"
        candidate.code = "PROJECT_IDENTITY_CONFLICT"
        candidate.reason = "authoritative artifacts disagree on project identity"
        return candidate
    if len(set(workflow_refs)) > 1:
        candidate.status = "CONFLICTING"
        candidate.code = "WORKFLOW_IDENTITY_CONFLICT"
        candidate.reason = "authoritative artifacts disagree on workflow identity"
        return candidate
    if len(workspace_refs) > 1 and not all(_same_path(path, item) for item in workspace_refs):
        candidate.status = "CONFLICTING"
        candidate.code = "WORKSPACE_IDENTITY_CONFLICT"
        candidate.reason = "handoff/runtime references disagree on workspace"
        return candidate
    if not project_ids or not workflow_refs:
        candidate.code = "BINDING_INCOMPLETE"
        candidate.reason = "project_id, workflow identity and workspace reference are all required"
        return candidate
    if workspace_refs and not all(_same_path(path, item) for item in workspace_refs):
        candidate.status = "CONFLICTING"
        candidate.code = "WORKSPACE_IDENTITY_MISMATCH"
        candidate.reason = "workspace locator does not match authoritative workspace reference"
        return candidate
    # At least one known canonical runtime artifact is required.  A path with
    # a hand-written identity file but no workflow checkpoint is not enough to
    # become authoritative.
    if not evidence and candidate_metadata is None:
        candidate.code = "AUTHORITATIVE_STATE_MISSING"
        candidate.reason = "candidate has no known canonical runtime artifact"
        return candidate

    expected_project = project_ids[0]
    for key, payload in evidence.items():
        fields = _source_fields(_Source(name=key, payload=payload, base=path, kind="runtime"))
        if fields.get("project_ids") and expected_project not in fields["project_ids"]:
            candidate.status = "CONFLICTING"
            candidate.code = "PROJECT_IDENTITY_MISMATCH"
            candidate.reason = f"{key} is bound to another project"
            return candidate
        if fields.get("workspace_refs"):
            references = []
            for raw in fields["workspace_refs"]:
                try:
                    references.append(_resolve_path(raw, base=path, field=f"{key}.workspace_ref"))
                except UnifiedEntryError:
                    continue
            if references and not all(_same_path(path, item) for item in references):
                candidate.status = "CONFLICTING"
                candidate.code = "WORKSPACE_IDENTITY_MISMATCH"
                candidate.reason = f"{key} points at another workspace"
                return candidate
        if fields.get("workflow_refs") and workflow_refs[0] not in fields["workflow_refs"]:
            candidate.status = "CONFLICTING"
            candidate.code = "WORKFLOW_IDENTITY_MISMATCH"
            candidate.reason = f"{key} is bound to another workflow"
            return candidate

    # Explicit supersession metadata lets a resolver select one candidate
    # mechanically.  It is not a new persisted lifecycle state.
    candidate.status = "STALE" if any(status in {"SUPERSEDED", "STALE"} for status in _all_statuses(source_list)) else "VALID"
    candidate.code = "OK" if candidate.status == "VALID" else "WORKSPACE_SUPERSEDED"
    candidate.reason = "authoritative identity and runtime evidence agree"
    return candidate


def _all_statuses(sources: Sequence[_Source]) -> list[str]:
    statuses: list[str] = []
    for source in sources:
        statuses.extend(_source_fields(source).get("statuses", []))
    return statuses


def _source_contains_candidate_list(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    for key in ("workspace_candidates", "workspace_candidates", "candidates"):
        value = payload.get(key)
        if isinstance(value, list) and any(isinstance(item, Mapping) for item in value):
            return True
    return False


def _source_anchor_fields(source: _Source) -> dict[str, Any]:
    """Extract envelope identity while leaving per-candidate identities local."""

    if not _source_contains_candidate_list(source.payload) or not isinstance(source.payload, Mapping):
        return _source_fields(source)
    fields: dict[str, Any] = {
        "project_ids": [],
        "workflow_refs": [],
        "workspace_refs": [],
        "goals": [],
        "statuses": [],
        "superseded_by": [],
    }
    for key in ("project_id", "project_ref"):
        value = _identity_scalar(source.payload.get(key), field="project")
        if value:
            fields["project_ids"].append(value)
    identity = source.payload.get("project_identity")
    value = _identity_scalar(identity, field="project")
    if value:
        fields["project_ids"].append(value)
    return fields


def _candidate_mappings(payload: Any) -> list[Mapping[str, Any]]:
    """Find explicit candidate records embedded in a handoff envelope."""

    found: list[Mapping[str, Any]] = []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                if _normal_key(key) in {"workspace_candidates", "candidates"} and isinstance(child, list):
                    for item in child:
                        if isinstance(item, Mapping):
                            found.append(item)
                walk(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:128]:
                walk(child, depth + 1)

    walk(payload)
    return found


def _candidate_paths(
    *,
    anchor: Path,
    sources: Sequence[_Source],
    explicit_workspace: Any = None,
    workspace_candidates: Sequence[Any] | None = None,
) -> list[tuple[Path, Mapping[str, Any] | None, str]]:
    result: list[tuple[Path, Mapping[str, Any] | None, str]] = []

    def add(value: Any, *, base: Path, metadata: Mapping[str, Any] | None, source: str) -> None:
        try:
            path = _resolve_path(value, base=base, field="workspace_ref")
        except UnifiedEntryError:
            return
        if not any(_same_path(path, current[0]) for current in result):
            result.append((path, metadata, source))
        elif metadata is not None:
            # Keep explicit metadata attached to an existing path.
            for index, current in enumerate(result):
                if _same_path(path, current[0]) and current[1] is None:
                    result[index] = (current[0], metadata, current[2])

    if explicit_workspace is not None:
        if isinstance(explicit_workspace, Mapping):
            add(explicit_workspace, base=anchor, metadata=explicit_workspace, source="explicit_workspace")
        else:
            add(explicit_workspace, base=anchor, metadata=None, source="explicit_workspace")
    for item in workspace_candidates or ():
        if isinstance(item, Mapping):
            add(item, base=anchor, metadata=item, source="workspace_candidates")
        else:
            add(item, base=anchor, metadata=None, source="workspace_candidates")
    for source in sources:
        for item in _candidate_mappings(source.payload):
            if _path_scalar(item.get("workspace_ref", item.get("workspace_root", item.get("runtime_workspace")))):
                add(item, base=source.base, metadata=item, source=source.name)
    for source in sources:
        fields = _source_fields(source)
        for raw in fields.get("workspace_refs", []):
            add(raw, base=source.base, metadata=None, source=source.name)
    return result


def _unwrap_capability_metadata(value: Any) -> dict[str, dict[str, Any]]:
    """Normalize existing metadata without defining a new registry."""

    catalog: dict[str, dict[str, Any]] = {}

    def add(capability: Any, record: Any = None) -> None:
        if not isinstance(capability, str) or not capability.strip():
            return
        key = capability.strip()
        if isinstance(record, Mapping):
            catalog.setdefault(key, {}).update(copy.deepcopy(dict(record)))
        else:
            catalog.setdefault(key, {})

    if isinstance(value, Mapping):
        for wrapper in ("capabilities", "canonical_capability_model", "capability_ownership", "owners"):
            if wrapper in value:
                nested = _unwrap_capability_metadata(value[wrapper])
                for key, record in nested.items():
                    catalog.setdefault(key, {}).update(record)
        for key, record in value.items():
            if key in {"capabilities", "canonical_capability_model", "capability_ownership", "owners"}:
                continue
            if isinstance(record, Mapping) and ("." in str(key) or "capability" in record or "actor" in record or "owner" in record):
                add(key, record)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                add(item)
            elif isinstance(item, Mapping):
                key = item.get("capability", item.get("id", item.get("name")))
                add(key, item)
    return catalog


def _capability_catalog(
    *,
    capability_metadata: Any,
    canonical_registry: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    catalog = _unwrap_capability_metadata(capability_metadata)
    if isinstance(canonical_registry, Mapping):
        registry = canonical_registry.get("capabilities", canonical_registry)
        if isinstance(registry, Mapping):
            for key, record in registry.items():
                if isinstance(record, Mapping):
                    catalog.setdefault(str(key), {}).update(copy.deepcopy(dict(record)))
    for capability in CAPABILITIES:
        catalog.setdefault(capability, {})
    return catalog


def _owner(record: Mapping[str, Any], capability: str) -> str | None:
    for key in ("actor", "owner", "execution_owner", "owner_actor", "responsible_actor"):
        value = record.get(key)
        if isinstance(value, Mapping):
            value = value.get("actor", value.get("id", value.get("name")))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return CAPABILITY_OWNERS.get(capability)


def _contract_identity(
    capability: str,
    *,
    record: Mapping[str, Any],
    canonical_registry: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if isinstance(canonical_registry, Mapping):
        try:
            registry = canonical_registry.get("capabilities", canonical_registry)
            if isinstance(registry, Mapping) and capability in registry:
                contract = resolve_canonical_contract(capability, registry)
                return {
                    "capability": contract.capability,
                    "contract_version": contract.contract_version,
                    "machine_schema_version": contract.machine_schema_version,
                    "contract_ref": contract.contract_ref,
                    "digest": contract.digest,
                }
        except (ContractValidationError, KeyError, TypeError, ValueError):
            return None
    ref = record.get("contract_ref")
    version = record.get("contract_version")
    machine_schema = record.get("machine_schema_version")
    if isinstance(ref, str) and ref.strip() and isinstance(version, str) and version.strip() and isinstance(machine_schema, str) and machine_schema.strip():
        body = {
            "capability": capability,
            "contract_version": version.strip(),
            "machine_schema_version": machine_schema.strip(),
            "policy": dict(record.get("policy", {})) if isinstance(record.get("policy"), Mapping) else {},
            "contract_ref": ref.strip(),
        }
        return {**body, "digest": sha256_json(body)}
    return None


def _state_value(mapping: Mapping[str, Any] | None, *keys: str) -> Any:
    if not isinstance(mapping, Mapping):
        return None
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _status(value: Any) -> str:
    if isinstance(value, str):
        return value.strip().upper().replace("-", "_")
    return ""


def _truthy(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().casefold() in {"true", "yes", "complete", "completed", "approved"})


def _evidence_goal(evidence: Mapping[str, Mapping[str, Any]]) -> str | None:
    for payload in evidence.values():
        for key in _GOAL_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        brief = payload.get("brief")
        if isinstance(brief, Mapping):
            for key in _GOAL_KEYS:
                value = brief.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return None


def _brief_approved(evidence: Mapping[str, Mapping[str, Any]]) -> bool:
    for payload in evidence.values():
        for key in ("brief_status", "status", "state", "brief_state"):
            if _status(payload.get(key)) == "APPROVED":
                return True
        if isinstance(payload.get("requirement_baseline"), Mapping) and payload.get("state") == "APPROVED":
            return True
    return False


def _design_status(evidence: Mapping[str, Mapping[str, Any]]) -> str:
    for payload in evidence.values():
        review = payload.get("design_review")
        if isinstance(review, Mapping):
            state = _status(_state_value(review, "state", "status", "decision"))
            if state in {"ACCEPTED", "APPROVED", "ACCEPT"}:
                return "ACCEPTED"
            if state:
                return "FORMED"
        for key in ("design_review_state", "design_status", "design_state"):
            state = _status(payload.get(key))
            if state in {"ACCEPTED", "APPROVED", "ACCEPT"}:
                return "ACCEPTED"
            if state:
                return "FORMED"
        human_decision = payload.get("human_decision")
        if payload.get("human_accept_receipt") or (
            isinstance(human_decision, Mapping)
            and _status(human_decision.get("decision")) == "ACCEPT"
        ):
            return "ACCEPTED"
    return "NONE"


def _research_complete(evidence: Mapping[str, Mapping[str, Any]]) -> bool:
    for payload in evidence.values():
        for key in ("research_complete", "research_completed", "research_completion"):
            if _truthy(payload.get(key)):
                return True
        state = _status(_state_value(payload, "research_status", "research_state"))
        if state in {"COMPLETE", "COMPLETED", "SUFFICIENT"}:
            return True
        phase = _status(payload.get("phase"))
        if phase in {"AWAITING_DESIGN_REVIEW", "DESIGN_ACCEPTED", "PLANNED", "RUNNING", "COMPLETE"}:
            return True
        if isinstance(payload.get("design_review"), Mapping) or "design_digest" in payload:
            return True
    return False


def _stage_record(evidence: Mapping[str, Mapping[str, Any]]) -> tuple[Mapping[str, Any] | None, str]:
    stage_state = evidence.get("stage_state")
    if not isinstance(stage_state, Mapping):
        return None, "NONE"
    current_id = stage_state.get("active_stage_id") or stage_state.get("current_stage_id")
    stages = stage_state.get("stages")
    if isinstance(stages, Mapping) and current_id in stages and isinstance(stages[current_id], Mapping):
        record = stages[current_id]
        return record, _status(record.get("status"))
    if isinstance(stage_state.get("current_stage"), Mapping):
        record = stage_state["current_stage"]
        return record, _status(record.get("status"))
    return stage_state, _status(stage_state.get("status", stage_state.get("stage_status")))


def _execution_complete(record: Mapping[str, Any] | None, evidence: Mapping[str, Mapping[str, Any]]) -> bool:
    if record is not None:
        for key in ("execution_evidence_complete", "evidence_complete", "execution_complete"):
            if _truthy(record.get(key)):
                return True
        latest = record.get("latest_stage_result", record.get("latest_result"))
        if isinstance(latest, Mapping) and _status(latest.get("status")) in {"SUCCEEDED", "SUCCESS", "COMPLETE", "COMPLETED"}:
            return True
        if _status(record.get("status")) in {"STAGE_READY", "APPROVED"}:
            return True
    for payload in evidence.values():
        if _truthy(payload.get("execution_evidence_complete")) or _truthy(payload.get("execution_complete")):
            return True
        if isinstance(payload.get("execution_evidence"), Mapping):
            return True
    return False


def _review_complete(evidence: Mapping[str, Mapping[str, Any]]) -> bool:
    for payload in evidence.values():
        review = payload.get("technical_review", payload.get("review"))
        if isinstance(review, Mapping):
            state = _status(_state_value(review, "decision", "state", "status", "workflow_decision"))
            if state in {"STAGE_READY", "APPROVED", "ACCEPTED", "COMPLETE", "COMPLETED"}:
                return True
        for key in ("technical_review_state", "review_state", "review_decision"):
            if _status(payload.get(key)) in {"STAGE_READY", "APPROVED", "ACCEPTED", "COMPLETE", "COMPLETED"}:
                return True
    return False


def _integration_complete(evidence: Mapping[str, Mapping[str, Any]]) -> bool:
    for payload in evidence.values():
        for key in ("integrated", "integration_complete", "delivery_complete", "verification_complete"):
            if _truthy(payload.get(key)):
                return True
        state = _status(_state_value(payload, "integration_status", "delivery_status", "integration_state"))
        if state in {"INTEGRATED", "DELIVERED", "COMPLETE", "COMPLETED", "VERIFIED"}:
            return True
    return False


def derive_lifecycle_position(evidence: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Derive one lifecycle position from existing artifacts only."""

    if not isinstance(evidence, Mapping):
        raise UnifiedEntryError("ENTRY_STATE_INVALID", "authoritative evidence must be a mapping")
    checkpoint = evidence.get("workflow_state")
    phase = _status(checkpoint.get("phase")) if isinstance(checkpoint, Mapping) else ""
    terminal = bool(
        phase in {"COMPLETE", "CLOSED", "TERMINAL"}
        or any(_truthy(payload.get("terminal")) or _status(payload.get("workflow_status")) in _TERMINAL_STATES for payload in evidence.values())
    )
    if terminal:
        return {"current_position": "workflow_closed", "next_capability": None, "next_actor": "Human", "terminal": True}

    brief_present = "project_brief" in evidence or "workflow_state" in evidence
    if not brief_present or not _brief_approved(evidence):
        pending = any(
            _status(payload.get(key)) in {"WAITING_USER_APPROVAL", "AWAITING_USER_APPROVAL", "READY_FOR_HUMAN_REVIEW"}
            for payload in evidence.values()
            for key in ("phase", "brief_state", "state", "status")
        )
        if pending:
            return {"current_position": "requirement_gate", "next_capability": "requirements.confirmation", "next_actor": "Human", "terminal": False}
        return {"current_position": "requirements", "next_capability": "requirements.intake", "next_actor": "GPT", "terminal": False}

    research_done = _research_complete(evidence)
    if not research_done:
        return {"current_position": "research", "next_capability": "research", "next_actor": "GPT", "terminal": False}
    design = _design_status(evidence)
    if design == "NONE":
        return {"current_position": "design", "next_capability": "design.synthesis", "next_actor": "GPT", "terminal": False}
    if design != "ACCEPTED":
        return {"current_position": "design_review", "next_capability": "design.human_review", "next_actor": "Human", "terminal": False}

    record, stage_status = _stage_record(evidence)
    if stage_status in {"PLANNED", "NONE", ""}:
        return {"current_position": "stage_planning", "next_capability": "stage.planning", "next_actor": "existing owner", "terminal": False}
    if stage_status in {"ACTIVE", "STAGE_READY"}:
        if not _execution_complete(record, evidence):
            return {"current_position": "stage_execution", "next_capability": "stage.execution", "next_actor": "Codex", "terminal": False}
        if not _review_complete(evidence):
            return {"current_position": "technical_review", "next_capability": "review.technical", "next_actor": "GPT", "terminal": False}
        if not _integration_complete(evidence):
            return {"current_position": "integration", "next_capability": "integration", "next_actor": "Codex", "terminal": False}
        return {"current_position": "verification", "next_capability": "verification", "next_actor": "Codex", "terminal": False}
    if stage_status in {"APPROVED", "CLOSED", "STOPPED"}:
        return {"current_position": "stage_closed", "next_capability": "stage.planning", "next_actor": "existing owner", "terminal": False}
    if _execution_complete(record, evidence) and not _review_complete(evidence):
        return {"current_position": "technical_review", "next_capability": "review.technical", "next_actor": "GPT", "terminal": False}
    return {"current_position": "stage_planning", "next_capability": "stage.planning", "next_actor": "existing owner", "terminal": False}


def _route_capability(
    position: Mapping[str, Any],
    *,
    capability_metadata: Any,
    canonical_registry: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    capability = position.get("next_capability")
    if not isinstance(capability, str) or not capability:
        return None, None
    catalog = _capability_catalog(capability_metadata=capability_metadata, canonical_registry=canonical_registry)
    record = catalog.get(capability)
    if record is None:
        return None, "CANONICAL_CAPABILITY_MISSING"
    actor = _owner(record, capability)
    if not actor:
        return None, "CAPABILITY_OWNER_MISSING"
    contract = _contract_identity(capability, record=record, canonical_registry=canonical_registry)
    # Existing metadata can deliberately advertise a route without embedding
    # a contract body.  The canonical contract is resolved when a registry is
    # supplied; the transient result still records the capability owner.
    return {
        "capability": capability,
        "actor": actor,
        "contract": contract,
    }, None


def _human_result(
    *,
    anchor: Path,
    candidates: Sequence[_Candidate],
    reason: str,
    question: str,
    question_id: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": ENTRY_SCHEMA_VERSION,
        "entry_outcome": "HUMAN_INPUT_REQUIRED",
        "outcome": "HUMAN_INPUT_REQUIRED",
        "mode": None,
        "project_id": project_id,
        "workflow_ref": None,
        "workspace_ref": None,
        "current_position": None,
        "next_capability": None,
        "next_actor": None,
        "canonical_contract": None,
        "ambiguity_reason": reason,
        "question_id": question_id,
        "question": question,
        "project_anchor": anchor.as_posix(),
        "workspace_candidates": [candidate.bounded_view() for candidate in candidates],
        "dispatch_performed": False,
    }


def _blocked_result(
    *,
    anchor: Path,
    candidates: Sequence[_Candidate],
    code: str,
    reason: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": ENTRY_SCHEMA_VERSION,
        "entry_outcome": "BLOCKED",
        "outcome": "BLOCKED",
        "mode": None,
        "project_id": project_id,
        "workflow_ref": None,
        "workspace_ref": None,
        "current_position": None,
        "next_capability": None,
        "next_actor": None,
        "canonical_contract": None,
        "blocked_code": code,
        "reason": reason,
        "project_anchor": anchor.as_posix(),
        "workspace_candidates": [candidate.bounded_view() for candidate in candidates],
        "dispatch_performed": False,
    }


def resolve_unified_entry(
    project_anchor: str | os.PathLike[str] | Mapping[str, Any] | None = None,
    *,
    intent: str = "continue",
    goal: str | None = None,
    project_id: str | None = None,
    workflow_ref: str | None = None,
    workspace_ref: str | os.PathLike[str] | Mapping[str, Any] | None = None,
    workspace_candidates: Sequence[Any] | None = None,
    handoff: Mapping[str, Any] | str | os.PathLike[str] | None = None,
    runtime_metadata: Mapping[str, Any] | str | os.PathLike[str] | None = None,
    capability_metadata: Any = None,
    canonical_registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one short entry request into a transient routing result.

    ``project_anchor`` is mandatory and explicit.  It may identify a project
    checkout or a runtime workspace, but neither is trusted solely because it
    exists.  ``handoff`` and ``runtime_metadata`` are optional injection seams
    for tests and adapters; when omitted, only the named handoff/runtime files
    below the anchor are read.
    """

    if isinstance(project_anchor, Mapping):
        request = dict(project_anchor)
        project_anchor = request.get("project_anchor", request.get("project_root"))
        intent = request.get("intent", request.get("mode", intent))
        goal = request.get("goal", request.get("task", goal))
        project_id = request.get("project_id", project_id)
        workflow_ref = request.get("workflow_ref", request.get("workflow_id", workflow_ref))
        workspace_ref = request.get("workspace_ref", request.get("workspace_root", workspace_ref))
        workspace_candidates = request.get("workspace_candidates", workspace_candidates)
        handoff = request.get("handoff", handoff)
        runtime_metadata = request.get("runtime_metadata", runtime_metadata)
        capability_metadata = request.get("capability_metadata", capability_metadata)
        canonical_registry = request.get("canonical_registry", canonical_registry)
    if project_anchor is None:
        raise UnifiedEntryError("PROJECT_ANCHOR_REQUIRED", "an explicit project anchor is required")
    anchor = _resolve_path(project_anchor, base=Path.cwd(), field="project_anchor", must_exist=True)
    if not anchor.is_dir():
        raise UnifiedEntryError("PROJECT_ANCHOR_INVALID", "project_anchor must be a directory")
    normalized_intent = _normalise_intent(intent)
    if canonical_registry is None:
        canonical_registry = canonical_capability_contracts()
    request_goal = _bounded_text(goal, "goal", required=False) if goal is not None else None
    expected_project = _bounded_text(project_id, "project_id") if project_id is not None else None
    expected_workflow = _bounded_text(workflow_ref, "workflow_ref") if workflow_ref is not None else None

    sources = _load_anchor_sources(anchor)
    if handoff is not None:
        sources.append(_source_from_value(handoff, name="explicit_handoff", base=anchor, kind="handoff"))
    if runtime_metadata is not None:
        sources.append(_source_from_value(runtime_metadata, name="explicit_runtime_metadata", base=anchor, kind="runtime"))
    if expected_project is not None or expected_workflow is not None:
        identity: dict[str, Any] = {}
        if expected_project is not None:
            identity["project_id"] = expected_project
        if expected_workflow is not None:
            identity["workflow_id"] = expected_workflow
        sources.append(_Source(name="explicit_identity", payload=identity, base=anchor, kind="explicit"))

    anchor_fields = [_source_anchor_fields(source) for source in sources]
    anchor_projects, anchor_workflows, anchor_goals, _, _ = _collect_identity(anchor_fields, base=anchor)
    if len(set(anchor_projects)) > 1:
        return _blocked_result(anchor=anchor, candidates=(), code="PROJECT_IDENTITY_CONFLICT", reason="handoff/runtime metadata disagree on project identity")
    if len(set(anchor_workflows)) > 1:
        return _blocked_result(anchor=anchor, candidates=(), code="WORKFLOW_IDENTITY_CONFLICT", reason="handoff/runtime metadata disagree on workflow identity")
    if expected_project is not None and anchor_projects and anchor_projects[0] != expected_project:
        return _blocked_result(anchor=anchor, candidates=(), code="PROJECT_IDENTITY_MISMATCH", reason="explicit project identity disagrees with handoff/runtime metadata", project_id=expected_project)
    if expected_workflow is not None and anchor_workflows and anchor_workflows[0] != expected_workflow:
        return _blocked_result(anchor=anchor, candidates=(), code="WORKFLOW_IDENTITY_MISMATCH", reason="explicit workflow identity disagrees with handoff/runtime metadata", project_id=expected_project or (anchor_projects[0] if anchor_projects else None))
    project_for_result = expected_project or (anchor_projects[0] if anchor_projects else None)

    paths = _candidate_paths(
        anchor=anchor,
        sources=sources,
        explicit_workspace=workspace_ref,
        workspace_candidates=workspace_candidates,
    )
    candidates: list[_Candidate] = []
    for path, metadata, source_name in paths:
        candidate = _candidate_for_path(path, sources=sources, anchor=anchor, candidate_metadata=metadata)
        if source_name not in candidate.source_names:
            candidate.source_names.insert(0, source_name)
        candidates.append(candidate)

    # An explicitly supplied workspace is a selector, not an authority.  A
    # selected candidate still needs its complete durable identity binding.
    explicit_path: Path | None = None
    if workspace_ref is not None:
        try:
            explicit_path = _resolve_path(workspace_ref, base=anchor, field="workspace_ref")
        except UnifiedEntryError:
            return _blocked_result(anchor=anchor, candidates=candidates, code="WORKSPACE_REF_INVALID", reason="explicit workspace_ref is not a valid locator", project_id=project_for_result)

    valid = [candidate for candidate in candidates if candidate.status == "VALID"]
    stale = [candidate for candidate in candidates if candidate.status == "STALE"]
    if stale and len(valid) == 1:
        # A valid candidate mechanically supersedes a stale one when the
        # source explicitly marked the stale candidate.  No new state is
        # written and the stale candidate remains visible in evidence.
        pass
    if explicit_path is not None:
        selected = next((candidate for candidate in valid if _same_path(candidate.path, explicit_path)), None)
        if selected is None:
            matching = next((candidate for candidate in candidates if _same_path(candidate.path, explicit_path)), None)
            if matching is not None:
                return _blocked_result(anchor=anchor, candidates=candidates, code=matching.code, reason=matching.reason, project_id=project_for_result)
            return _blocked_result(anchor=anchor, candidates=candidates, code="WORKSPACE_NOT_AUTHENTICATED", reason="explicit workspace_ref has no matching authoritative evidence", project_id=project_for_result)
        valid = [selected]
    elif len(valid) > 1:
        # Prefer an explicit supersession relation when one is present.
        workflow_ids = {candidate.workflow_refs[0] for candidate in valid if candidate.workflow_refs}
        superseding = [candidate for candidate in valid if candidate.superseded_by and candidate.superseded_by in workflow_ids]
        if len(superseding) == len(valid) - 1:
            valid = [candidate for candidate in valid if candidate not in superseding]
        else:
            labels = " / ".join(chr(ord("A") + index) for index, _ in enumerate(valid[:8]))
            return _human_result(
                anchor=anchor,
                candidates=valid,
                reason="multiple authoritative workspace candidates remain valid",
                question=f"发现多个有效运行目录。请选择 {labels}。",
                question_id="workspace_candidate",
                project_id=project_for_result,
            )

    selected = valid[0] if valid else None
    conflicting = [candidate for candidate in candidates if candidate.status == "CONFLICTING"]
    invalid_with_locator = [candidate for candidate in candidates if candidate.status == "INVALID"]

    if normalized_intent == "CONTINUE":
        if conflicting:
            return _blocked_result(anchor=anchor, candidates=candidates, code=conflicting[0].code, reason=conflicting[0].reason, project_id=project_for_result)
        if selected is None:
            return _human_result(
                anchor=anchor,
                candidates=candidates,
                reason="no trusted workflow identity was found for the continue request",
                question="没有找到可信 Workflow。请提供原 Workflow workspace，或确认这是一个新 Workflow。",
                question_id="missing_workflow",
                project_id=project_for_result,
            )
        if expected_workflow is not None and (not selected.workflow_refs or selected.workflow_refs[0] != expected_workflow):
            return _blocked_result(anchor=anchor, candidates=candidates, code="WORKFLOW_IDENTITY_MISMATCH", reason="selected workspace is bound to another workflow", project_id=project_for_result)
        evidence = selected.evidence
        position = derive_lifecycle_position(evidence)
        if position.get("terminal"):
            return _human_result(
                anchor=anchor,
                candidates=candidates,
                reason="the selected workflow is terminally closed",
                question="旧 Workflow 已终结。是否以当前项目为基础建立一个新的 Workflow？",
                question_id="terminal_workflow",
                project_id=selected.project_ids[0] if selected.project_ids else project_for_result,
            )
        mode = "RESUME"
    else:
        if conflicting:
            return _blocked_result(anchor=anchor, candidates=candidates, code=conflicting[0].code, reason=conflicting[0].reason, project_id=project_for_result)
        if selected is not None:
            existing_goal = _evidence_goal(selected.evidence) or (selected.goals[0] if selected.goals else None)
            if request_goal and existing_goal and request_goal.strip().casefold() == existing_goal.strip().casefold():
                # This is the idempotent NEW recovery path: after legal
                # creation, repeating the same goal resolves to RESUME.
                mode = "RESUME"
                evidence = selected.evidence
                position = derive_lifecycle_position(evidence)
                if position.get("terminal"):
                    return _human_result(
                        anchor=anchor,
                        candidates=candidates,
                        reason="the matching workflow is terminally closed",
                        question="旧 Workflow 已终结。是否以当前项目为基础建立一个新的 Workflow？",
                        question_id="terminal_workflow",
                        project_id=selected.project_ids[0] if selected.project_ids else project_for_result,
                    )
            else:
                return _human_result(
                    anchor=anchor,
                    candidates=candidates,
                    reason="a trusted workflow already exists for this project",
                    question="这个新任务要作为当前 Workflow 的后续任务继续，还是建立新的 Workflow？",
                    question_id="existing_workflow_choice",
                    project_id=selected.project_ids[0] if selected.project_ids else project_for_result,
                )
        else:
            if invalid_with_locator:
                return _blocked_result(anchor=anchor, candidates=candidates, code=invalid_with_locator[0].code, reason=invalid_with_locator[0].reason, project_id=project_for_result)
            mode = "NEW"
            evidence = {}
            position = {"current_position": "new_workflow", "next_capability": "requirements.intake", "next_actor": "GPT", "terminal": False}

    routing, route_error = _route_capability(position, capability_metadata=capability_metadata, canonical_registry=canonical_registry)
    if route_error is not None or routing is None:
        return _blocked_result(anchor=anchor, candidates=candidates, code=route_error or "CAPABILITY_ROUTE_INVALID", reason="next capability is not available from the existing canonical capability metadata", project_id=project_for_result)

    selected_project = selected.project_ids[0] if selected and selected.project_ids else project_for_result
    selected_workflow = selected.workflow_refs[0] if selected and selected.workflow_refs else None
    selected_workspace = selected.path.as_posix() if selected and mode == "RESUME" else None
    return {
        "schema_version": ENTRY_SCHEMA_VERSION,
        "entry_outcome": "ROUTE",
        "outcome": "ROUTE",
        "mode": mode,
        "project_id": selected_project,
        "workflow_ref": selected_workflow,
        "workspace_ref": selected_workspace,
        "current_position": position.get("current_position"),
        "next_capability": routing["capability"],
        "next_actor": routing["actor"],
        "canonical_contract": routing.get("contract"),
        "project_anchor": anchor.as_posix(),
        "workspace_candidates": [candidate.bounded_view() for candidate in candidates],
        "authority": {
            "identity_before_path": True,
            "resolve_before_create": True,
            "derive_dont_duplicate": True,
            "selected_workspace": selected_workspace,
            "selected_workflow": selected_workflow,
        },
        "dispatch_performed": False,
    }


def dispatch_once(
    result: Mapping[str, Any],
    dispatcher: Callable[[Mapping[str, Any]], Any] | None,
) -> dict[str, Any]:
    """Perform at most one downstream invocation and then return.

    This helper deliberately has no retry or continuation loop.  A dispatcher
    may be an existing canonical Contract adapter; the entry point passes the
    transient route once and never interprets a returned ``CONTINUE`` as a
    request to invoke another actor.
    """

    if not isinstance(result, Mapping):
        raise UnifiedEntryError("ENTRY_RESULT_INVALID", "entry result must be a mapping")
    output = copy.deepcopy(dict(result))
    if output.get("entry_outcome") != "ROUTE" or dispatcher is None:
        output["dispatch_performed"] = False
        return output
    try:
        invocation = dispatcher(copy.deepcopy(output))
    except Exception as exc:  # noqa: BLE001 - one bounded downstream seam
        output["dispatch_performed"] = True
        output["invocation"] = {"status": "FAILED", "error_code": type(exc).__name__[:128]}
        return output
    if isinstance(invocation, Mapping):
        safe_invocation = _safe_copy(invocation)
    else:
        safe_invocation = {"status": "COMPLETED" if invocation is not None else "EMPTY"}
    output["dispatch_performed"] = True
    output["invocation"] = safe_invocation
    return output


def resolve_and_dispatch(
    project_anchor: str | os.PathLike[str] | Mapping[str, Any] | None = None,
    *,
    dispatcher: Callable[[Mapping[str, Any]], Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Resolve an entry and, when routed, invoke one existing capability once."""

    return dispatch_once(resolve_unified_entry(project_anchor, **kwargs), dispatcher)


class UnifiedWorkflowEntry:
    """Reusable facade around the stateless resolver."""

    def __init__(
        self,
        *,
        capability_metadata: Any = None,
        canonical_registry: Mapping[str, Any] | None = None,
    ) -> None:
        self.capability_metadata = capability_metadata
        self.canonical_registry = canonical_registry

    def resolve(self, project_anchor: str | os.PathLike[str] | Mapping[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("capability_metadata", self.capability_metadata)
        kwargs.setdefault("canonical_registry", self.canonical_registry)
        return resolve_unified_entry(project_anchor, **kwargs)

    def enter(
        self,
        project_anchor: str | os.PathLike[str] | Mapping[str, Any] | None = None,
        *,
        dispatcher: Callable[[Mapping[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return dispatch_once(self.resolve(project_anchor, **kwargs), dispatcher)

    resolve_entry = resolve
    run_once = enter


# Short aliases used by a few adapter prototypes.
UnifiedEntryResolver = UnifiedWorkflowEntry
resolve_entry = resolve_unified_entry
route_entry = resolve_unified_entry


__all__ = [
    "ENTRY_MODES",
    "ENTRY_OUTCOMES",
    "ENTRY_SCHEMA_VERSION",
    "EntryError",
    "EntryRequest",
    "UnifiedEntryError",
    "UnifiedEntryResolver",
    "UnifiedWorkflowEntry",
    "UnifiedWorkflowEntryError",
    "derive_lifecycle_position",
    "dispatch_once",
    "resolve_and_dispatch",
    "resolve_entry",
    "resolve_unified_entry",
    "route_entry",
]
