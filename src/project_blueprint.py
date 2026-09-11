"""Bounded Step 14 project blueprint generation.

Step 14 is the small hand-off between Project Discovery and a later, user
gated Stage planner.  It consumes only the approved project brief, the
verified Step 12 context, and the canonical Step 13 discovery report.  A
compact ``CODEX_FEASIBILITY`` packet is built locally, then one explicitly
injected ``fresh`` consultant is called.  No browser transport, real GPT
client, Stage creation, or business/source write is hidden in this module.

The resulting Markdown and JSON manifest are canonical, bounded, and
idempotent.  Re-running with the same input digests returns the existing
artifact without another consultation; a changed brief/context/discovery
digest creates one new bounded blueprint revision in place.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .bridge_adapter import (
    BridgeEnvelopeError,
    is_bridge_envelope,
    normalize_bridge_envelope,
)
from .asset_layer import (
    ASSET_LAYER_MARKER,
    AVAILABLE_ASSETS_RELATIVE_PATH,
    CANDIDATE_USER_PROJECT_ASSET_ID,
    COMPOSITION_DECISIONS,
    AssetLayerError,
    build_asset_pack,
    ensure_available_assets,
)
from .contracts import ContractValidationError, canonical_json, load_schema, sha256_json, validate_instance
from .project_context import ProjectContextError, load_project_context
from .project_discovery import (
    CONSULTATION_MODE_FRESH,
    DISCOVERY_REPORT_RELATIVE_PATH,
    ProjectDiscoveryError,
    _project_url_from_brief,
    _parse_response_text as _parse_step13_response_text,
    _response_value_path_precheck as _step13_response_value_path_precheck,
    sanitize_project_url,
)
from .project_intake import ProjectIntakeError, validate_project_brief
from .project_state import ProjectStateError, load_project_brief, resolve_project_root
from .research_prompt_policy import method_evidence_policy_text


PROJECT_BLUEPRINT_SCHEMA_VERSION = "project_blueprint.v1"
BLUEPRINT_MANIFEST_SCHEMA_VERSION = "blueprint_manifest.v1"
PROJECT_BLUEPRINT_RELATIVE_PATH = Path(".research") / "blueprint" / "PROJECT_BLUEPRINT.md"
PROJECT_BLUEPRINT_PATH = PROJECT_BLUEPRINT_RELATIVE_PATH
PROJECT_BLUEPRINT_FILENAME = PROJECT_BLUEPRINT_RELATIVE_PATH.name
BLUEPRINT_MANIFEST_RELATIVE_PATH = Path(".research") / "blueprint" / "BLUEPRINT_MANIFEST.json"
BLUEPRINT_MANIFEST_PATH = BLUEPRINT_MANIFEST_RELATIVE_PATH
BLUEPRINT_MANIFEST_FILENAME = BLUEPRINT_MANIFEST_RELATIVE_PATH.name
PROJECT_BLUEPRINT_MANIFEST_RELATIVE_PATH = BLUEPRINT_MANIFEST_RELATIVE_PATH
PROJECT_BLUEPRINT_MANIFEST_PATH = BLUEPRINT_MANIFEST_PATH
PROJECT_BLUEPRINT_MARKER = "GPT_CODEX_BLUEPRINT_REVIEW_PASS"
BLUEPRINT_MARKER = PROJECT_BLUEPRINT_MARKER
PROJECT_BLUEPRINT_STATUS = "PROJECT_BLUEPRINT_READY"
BLUEPRINT_STATUS = PROJECT_BLUEPRINT_STATUS
READY_FOR_STAGE_PLANNING = "READY_FOR_STAGE_PLANNING"
CODEX_FEASIBILITY = "CODEX_FEASIBILITY"
CODEX_FEASIBILITY_MARKER = "CODEX_FEASIBILITY_PASS"
CODEX_FEASIBILITY_RELATIVE_PATH = Path(".research") / "blueprint" / "CODEX_FEASIBILITY.json"
CODEX_FEASIBILITY_PATH = CODEX_FEASIBILITY_RELATIVE_PATH
CODEX_FEASIBILITY_FILENAME = CODEX_FEASIBILITY_RELATIVE_PATH.name
CONSULTATION_MODE = CONSULTATION_MODE_FRESH
FRESH = CONSULTATION_MODE_FRESH

MAX_ALTERNATIVES = 4
MAX_TEXT_LENGTH = 12_000
MAX_LIST_ITEMS = 16
MAX_ROUTE_DEPTH = 8
MAX_BLUEPRINT_BYTES = 180_000
MAX_FEASIBILITY_BYTES = 120_000
MAX_CONTEXT_EXCERPT = 12_000
# Keep the transient response envelope finite independently of the persisted
# blueprint bound.  These values intentionally match Step 13's bounded
# response grammar; the response is never persisted as raw text.
MAX_RESPONSE_TEXT_LENGTH = 16_000
MAX_RESPONSE_BYTES = MAX_RESPONSE_TEXT_LENGTH * 4
MAX_RESPONSE_PROSE_LENGTH = 2_000

BLUEPRINT_GRAMMAR_BRANCHES = (
    "NUL",
    "INVALID_UTF8",
    "TOO_LARGE",
    "ABSOLUTE_PATH",
    "SECRET",
    "EMPTY",
    "ROOT_NOT_OBJECT",
    "OBJECT_MISSING",
    "OBJECT_UNBALANCED",
    "MULTIPLE_OBJECTS",
    "OBJECT_DUPLICATE_KEY",
    "FENCE_BODY_DUPLICATE_KEY",
    "OBJECT_INVALID_TEXT",
    "FENCE_BODY_INVALID_TEXT",
    "OBJECT_MALFORMED_JSON",
    "FENCE_BODY_MALFORMED_JSON",
    "OBJECT_TRAILING_DATA",
    "FENCE_BODY_TRAILING_DATA",
    "OBJECT_ROOT_NOT_OBJECT",
    "FENCE_BODY_ROOT_NOT_OBJECT",
    "OBJECT_CONTROL_CHAR_ESCAPED",
    "FENCE_BODY_CONTROL_CHAR_ESCAPED",
    "OBJECT_CONTROL_CHAR_UNSUPPORTED",
    "FENCE_BODY_CONTROL_CHAR_UNSUPPORTED",
    "OBJECT_CONTROL_CHAR_OUTSIDE_STRING",
    "FENCE_BODY_CONTROL_CHAR_OUTSIDE_STRING",
    "OBJECT_TOO_DEEP",
    "FENCE_BODY_TOO_DEEP",
    "PROSE_TOO_LARGE",
    "PROSE_CONTROL_CHAR",
    "PROSE_DELIMITER",
    "PROSE_CODE",
    "PROSE_JSON_VALUE",
    "FENCE_PROSE_TOO_LARGE",
    "FENCE_PROSE_CONTROL_CHAR",
    "FENCE_PROSE_BACKTICK",
    "FENCE_PROSE_CODE",
    "UNSUPPORTED_FENCE",
    "UNEXPECTED_FENCE",
    "FENCE_UNCLOSED",
    "MULTIPLE_FENCES",
    "FENCE_ORDER",
    "FENCE_BODY_EMPTY",
    "WRAPPER_INVALID",
    "MULTIPLE_WRAPPERS",
    "AMBIGUOUS_WRAPPER",
    "PRIMARY_REQUIRED",
    "LEGACY_PRIMARY_AMBIGUOUS",
    "SEMANTIC_INVALID",
    "LIMIT",
    "ASSET_ID",
)
_BLUEPRINT_GRAMMAR_BRANCH_SET = frozenset(BLUEPRINT_GRAMMAR_BRANCHES)
_BLUEPRINT_WRAPPER_KEYS = frozenset({"blueprint", "project_blueprint", "result"})
_RESPONSE_PATH_KEYS = frozenset(
    {
        "path",
        "file",
        "filename",
        "filepath",
        "file_path",
        "repository_root",
        "workspace_root",
        "working_directory",
        "cwd",
        "clone_path",
        "source_path",
    }
)

_SENSITIVE_KEYS = frozenset(
    {
        "api_key", "apikey", "access_token", "auth_token", "client_secret",
        "cookie", "cookies", "dom", "local_storage", "password", "passwd",
        "prompt", "raw_dom", "raw_response", "secret", "session", "session_id",
        "session_storage", "storage_state", "token", "tokens", "transcript",
        "conversation", "messages", "turns", "chat_history", "raw_history",
        "confidence", "confidence_score",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class ProjectBlueprintError(RuntimeError):
    """Stable fail-closed error for Step 14."""

    def __init__(self, code: str, message: str, *, grammar_branch: str | None = None) -> None:
        self.code = str(code)
        # A failed response may be reported to an offline caller, but the
        # diagnostic must remain a closed-set token.  Never attach a parser
        # message, key name, response excerpt, path, or raw response here.
        if grammar_branch is not None:
            self.grammar_branch = _sanitize_grammar_branch(grammar_branch)
            self.response_state = "REJECTED"
            self.grammar_state = "REJECTED"
        super().__init__(message)


BlueprintError = ProjectBlueprintError
ProjectBlueprintFailure = ProjectBlueprintError


class BlueprintConsultant(Protocol):
    def consult(self, **kwargs: Any) -> Mapping[str, Any] | str: ...


def _sanitize_grammar_branch(value: Any) -> str:
    if isinstance(value, str) and value in _BLUEPRINT_GRAMMAR_BRANCH_SET:
        return value
    return "SEMANTIC_INVALID"


def _response_invalid(branch: str, *, code: str = "BLUEPRINT_RESPONSE_INVALID") -> None:
    branch_text = _sanitize_grammar_branch(branch)
    raise ProjectBlueprintError(
        code,
        f"blueprint response rejected [{branch_text}]",
        grammar_branch=branch_text,
    ) from None


def _safe_text(value: Any, field: str, *, required: bool = False, max_length: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise ProjectBlueprintError("BLUEPRINT_INPUT_INVALID", f"{field} must be text")
    result = value.strip()
    if required and not result:
        raise ProjectBlueprintError("BLUEPRINT_INPUT_INVALID", f"{field} must be non-empty")
    if "\x00" in result or len(result) > max_length:
        raise ProjectBlueprintError("BLUEPRINT_INPUT_INVALID", f"{field} exceeds its bound")
    return result


def _assert_safe(value: Any, *, path: str = "$", depth: int = 0) -> None:
    if depth > MAX_ROUTE_DEPTH:
        raise ProjectBlueprintError("BLUEPRINT_INPUT_TOO_DEEP", "blueprint data is too deeply nested")
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ProjectBlueprintError("BLUEPRINT_INPUT_INVALID", f"object key at {path} must be text")
            key_name = str(key).strip().lower().replace("-", "_")
            if key_name in _SENSITIVE_KEYS:
                raise ProjectBlueprintError("BLUEPRINT_SECRET_REJECTED", f"forbidden field at {path}")
            _assert_safe(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise ProjectBlueprintError("BLUEPRINT_INPUT_TOO_LARGE", f"too many values at {path}")
        for index, child in enumerate(value):
            _assert_safe(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str):
        if "\x00" in value or len(value) > MAX_TEXT_LENGTH:
            raise ProjectBlueprintError("BLUEPRINT_INPUT_INVALID", f"unsafe text at {path}")
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise ProjectBlueprintError("BLUEPRINT_SECRET_REJECTED", f"secret-like value at {path}")
        return
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProjectBlueprintError("BLUEPRINT_INPUT_INVALID", f"non-finite number at {path}")
        return
    raise ProjectBlueprintError("BLUEPRINT_INPUT_INVALID", f"unsupported value at {path}")


def _unwrap_brief(value: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = value.get("project_brief")
    return nested if isinstance(nested, Mapping) else value


def _approved_brief(root: Path, supplied: Mapping[str, Any] | None) -> dict[str, Any]:
    if supplied is None:
        try:
            supplied = load_project_brief(root)
        except ProjectStateError as exc:
            raise ProjectBlueprintError("BRIEF_UNREADABLE", "canonical project brief could not be loaded") from exc
    if not isinstance(supplied, Mapping):
        raise ProjectBlueprintError("BRIEF_NOT_FOUND", "an approved project brief is required")
    supplied = _unwrap_brief(supplied)
    try:
        checked = validate_project_brief(supplied, root)
    except ProjectIntakeError as exc:
        raise ProjectBlueprintError("BRIEF_INVALID", "canonical project brief failed validation") from exc
    if checked.get("state") != "APPROVED" or checked.get("status") != "APPROVED":
        raise ProjectBlueprintError("BRIEF_NOT_APPROVED", "Step 14 requires an approved project brief")
    return checked


def _context(root: Path) -> dict[str, Any]:
    try:
        loaded = load_project_context(root)
    except (ProjectContextError, ProjectStateError, OSError, UnicodeError) as exc:
        raise ProjectBlueprintError("CONTEXT_REQUIRED", "a verified Step 12 project context is required") from exc
    if not isinstance(loaded, Mapping):
        raise ProjectBlueprintError("CONTEXT_INVALID", "project context must be an object")
    markdown = loaded.get("markdown")
    if not isinstance(markdown, str) or len(markdown.encode("utf-8")) > 120_000:
        raise ProjectBlueprintError("CONTEXT_INVALID", "project context is outside its bound")
    return copy.deepcopy(dict(loaded))


def _discovery(root: Path) -> dict[str, Any]:
    try:
        from .project_discovery import load_discovery_report

        loaded = load_discovery_report(root)
    except (ProjectDiscoveryError, ProjectStateError, OSError, UnicodeError) as exc:
        raise ProjectBlueprintError("DISCOVERY_REQUIRED", "a verified Step 13 discovery report is required") from exc
    if not isinstance(loaded, Mapping) or loaded.get("status") != "DISCOVERY_COMPLETE":
        raise ProjectBlueprintError("DISCOVERY_INVALID", "discovery report is not complete")
    return copy.deepcopy(dict(loaded))


def _semantic_context(context: Mapping[str, Any]) -> dict[str, Any]:
    semantic = context.get("context") if isinstance(context.get("context"), Mapping) else {}
    # Route-bearing hand-off fields are deliberately omitted.  They can carry
    # a prior recommendation or sunk-cost narrative from Step 12; Step 14's
    # FRESH review must derive its route independently.
    allowed = (
        "target", "inputs", "outputs", "user_visible_success", "constraints", "non_goals",
        "preferences", "unresolved_questions", "next_action", "git_identity",
    )
    result = {key: copy.deepcopy(semantic[key]) for key in allowed if key in semantic}
    # Keep the compact hand-off useful for the common top-level context form.
    for key in allowed:
        if key not in result and key in context:
            result[key] = copy.deepcopy(context[key])
    # ``git_identity`` is useful for local provenance, but its resolved
    # repository root is not GPT-visible context.  The local retention/
    # blueprint manifests remain the provenance authority; keep only a
    # controlled placeholder in this bounded feasibility packet.
    identity = result.get("git_identity")
    if isinstance(identity, Mapping):
        identity_view = copy.deepcopy(dict(identity))
        if "repository_root" in identity_view:
            identity_view["repository_root"] = "<LOCAL_REPOSITORY_ROOT>"
        result["git_identity"] = identity_view
    return result


def _resource_id(logical_name: str) -> str:
    """Return a stable id for a workflow resource without using local paths."""

    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(logical_name)).strip("-").casefold()
    return f"resource-{slug or 'canonical'}"


def _canonical_resource(root: Path, relative_path: str, logical_name: str, *, required: bool = True) -> dict[str, Any] | None:
    """Describe one bounded canonical file using a relative path and digest."""

    path = root / Path(relative_path)
    if not path.exists():
        if required:
            raise ProjectBlueprintError("CANONICAL_RESOURCE_MISSING", f"required canonical resource is missing: {logical_name}")
        return None
    if path.is_symlink() or not path.is_file():
        raise ProjectBlueprintError("CANONICAL_RESOURCE_INVALID", f"canonical resource is not a regular file: {logical_name}")
    try:
        data = path.read_bytes()
    except (OSError, UnicodeError) as exc:
        raise ProjectBlueprintError("CANONICAL_RESOURCE_UNREADABLE", f"canonical resource cannot be read: {logical_name}") from exc
    if len(data) > MAX_FEASIBILITY_BYTES:
        raise ProjectBlueprintError("CANONICAL_RESOURCE_TOO_LARGE", f"canonical resource exceeds its bound: {logical_name}")
    return {
        "resource_id": _resource_id(logical_name),
        "logical_name": logical_name,
        "path": relative_path,
        "digest": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def _resource_provenance(root: Path, *, include_feasibility: bool = False) -> dict[str, Any]:
    """Build the compact provenance map carried by feasibility/manifest records."""

    specs = (
        ("discovery_report", DISCOVERY_REPORT_RELATIVE_PATH.as_posix(), "DISCOVERY_REPORT.json", True),
        ("available_assets", ".research/AVAILABLE_ASSETS.json", "AVAILABLE_ASSETS.json", True),
        ("project_context", ".research/PROJECT_CONTEXT.md", "PROJECT_CONTEXT.md", True),
    )
    # CODEX_FEASIBILITY.json is the object being constructed by
    # compact_codex_feasibility(). Including its current on-disk digest in
    # the packet would make the first call (file absent) differ from every
    # idempotent reuse (file present), and would create a self-reference if
    # its digest were ever included in the file itself. Callers that need
    # post-build provenance may opt in explicitly.
    if include_feasibility:
        specs += (("codex_feasibility", CODEX_FEASIBILITY_RELATIVE_PATH.as_posix(), "CODEX_FEASIBILITY.json", False),)
    result: dict[str, Any] = {}
    for key, path, logical_name, required in specs:
        item = _canonical_resource(root, path, logical_name, required=required)
        if item is not None:
            result[key] = item
    return result


def _feasibility_digest(packet: Mapping[str, Any]) -> str:
    """Digest a feasibility object without including its self-referential field."""

    body = {key: copy.deepcopy(value) for key, value in packet.items() if key != "feasibility_digest"}
    return sha256_json(body)


def _validate_feasibility(packet: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(packet, Mapping):
        raise ProjectBlueprintError("FEASIBILITY_INVALID", "codex feasibility must be an object")
    detached = copy.deepcopy(dict(packet))
    if detached.get("schema_version") != "codex_feasibility.v1":
        raise ProjectBlueprintError("FEASIBILITY_INVALID", "unsupported codex feasibility schema")
    if detached.get("stage") != CODEX_FEASIBILITY:
        raise ProjectBlueprintError("FEASIBILITY_INVALID", "codex feasibility stage marker is invalid")
    if detached.get("path") != CODEX_FEASIBILITY_RELATIVE_PATH.as_posix():
        raise ProjectBlueprintError("FEASIBILITY_INVALID", "codex feasibility path is invalid")
    if detached.get("marker") != CODEX_FEASIBILITY_MARKER:
        raise ProjectBlueprintError("FEASIBILITY_INVALID", "codex feasibility marker is invalid")
    digest = detached.get("feasibility_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or digest != _feasibility_digest(detached):
        raise ProjectBlueprintError("FEASIBILITY_DIGEST_MISMATCH", "codex feasibility digest does not match")
    try:
        validate_instance(detached, load_schema("codex_feasibility"))
    except ContractValidationError as exc:
        raise ProjectBlueprintError("FEASIBILITY_INVALID", "codex feasibility failed its schema") from exc
    _assert_safe(detached)
    return detached


def compact_codex_feasibility(
    project_root: str | os.PathLike[str] = ".",
    *,
    brief: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the local ``CODEX_FEASIBILITY`` packet without consulting GPT."""

    root = resolve_project_root(project_root)
    checked_brief = _approved_brief(root, brief)
    context = _context(root)
    discovery = _discovery(root)
    try:
        project_url = _project_url_from_brief(checked_brief)
    except ProjectDiscoveryError as exc:
        raise ProjectBlueprintError(exc.code, str(exc)) from exc
    try:
        available_assets = ensure_available_assets(root, brief=checked_brief)
        asset_pack = build_asset_pack(root, brief=checked_brief, purpose="blueprint")
    except AssetLayerError as exc:
        raise ProjectBlueprintError("ASSET_LAYER_INVALID", str(exc)) from exc
    # Step 14's local packet is itself a workflow-owned canonical resource.
    # Capture the other canonical inputs by relative path and content digest;
    # never put a resolved checkout path or raw transport material in this
    # record.  The feasibility file is intentionally not self-referenced here
    # (doing so would create a digest cycle).
    resource_provenance = _resource_provenance(root, include_feasibility=False)
    pack_id = str(
        asset_pack.get(
            "pack_id",
            "asset-pack-" + sha256_json({
                "project_id": str(checked_brief["project_id"]),
                "purpose": "blueprint",
                "assets_digest": str(asset_pack.get("available_assets_digest", "")),
                "asset_ids": list(asset_pack.get("asset_ids", [])),
            })[:16],
        )
    )
    resource_provenance["asset_pack"] = {
        "resource_id": pack_id,
        "logical_name": "ASSET_PACK",
        "path": AVAILABLE_ASSETS_RELATIVE_PATH.as_posix(),
        "digest": str(asset_pack.get("pack_digest", "")),
        "bytes": len(canonical_json(asset_pack).encode("utf-8")),
        "asset_ids": [str(item) for item in asset_pack.get("asset_ids", [])],
    }
    # Deliberately exclude discovery primary/alternatives.  Blueprint routes
    # must be independently derived by the fresh consultant rather than
    # inheriting a previous recommendation or sunk-cost narrative.
    discovery_summary = {
        "real_goal": discovery.get("real_goal", ""),
        "relevant_non_repo_methods": copy.deepcopy(discovery.get("relevant_non_repo_methods", [])),
        "important_risks": copy.deepcopy(discovery.get("important_risks", [])),
        "facts_needing_local_verification": copy.deepcopy(discovery.get("facts_needing_local_verification", [])),
        "no_direct_match_found": bool(discovery.get("no_direct_match_found", False)),
    }
    packet: dict[str, Any] = {
        "schema_version": "codex_feasibility.v1",
        "stage": CODEX_FEASIBILITY,
        "project_id": str(checked_brief["project_id"]),
        "brief_digest": sha256_json(checked_brief),
        "brief_revision": int(checked_brief.get("revision", 1)),
        "context_digest": str(context.get("context_digest", "")),
        "discovery_digest": str(discovery.get("evidence_digest", sha256_json(discovery))),
        "project_url": project_url,
        "real_goal": _safe_text(discovery_summary["real_goal"], "real_goal", required=True),
        "context": _semantic_context(context),
        "discovery": discovery_summary,
        # Blueprint receives the same canonical asset standard as Discovery.
        # The bounded pack contains metadata and verified facts only; source
        # files remain local and are never attached automatically.
        "available_assets_digest": str(available_assets.get("assets_digest", "")),
        "asset_pack": copy.deepcopy(asset_pack),
        "asset_pack_id": pack_id,
        "asset_pack_digest": str(asset_pack.get("pack_digest", "")),
        "candidate_zero_asset_id": (
            CANDIDATE_USER_PROJECT_ASSET_ID
            if any(item.get("asset_id") == CANDIDATE_USER_PROJECT_ASSET_ID for item in available_assets.get("assets", []))
            else None
        ),
        "asset_layer_marker": ASSET_LAYER_MARKER,
        "path": CODEX_FEASIBILITY_RELATIVE_PATH.as_posix(),
        "marker": CODEX_FEASIBILITY_MARKER,
        "resource_ids": [str(item["resource_id"]) for item in resource_provenance.values()],
        "resource_digests": {
            str(key): str(item["digest"])
            for key, item in resource_provenance.items()
        },
        "resource_provenance": resource_provenance,
        "route_instruction": "Derive one primary route and zero to four alternatives independently from this compact feasibility packet.",
    }
    _assert_safe(packet)
    packet["feasibility_digest"] = _feasibility_digest(packet)
    checked_packet = _validate_feasibility(packet)
    # Keep the feasibility packet in memory for the single Blueprint
    # consultation.  PROJECT_BLUEPRINT.md and BLUEPRINT_MANIFEST.json are the
    # Step 14 canonical outputs; persisting this intermediate packet would
    # make a later preflight change its own input set and would add an
    # unrequested artifact to the canonical blueprint directory.
    return checked_packet


build_codex_feasibility = compact_codex_feasibility
build_feasibility_packet = compact_codex_feasibility
compact_feasibility = compact_codex_feasibility


def _prompt_for(packet: Mapping[str, Any]) -> str:
    # The prompt intentionally contains only the compact packet.  It does not
    # mention or serialize discovery recommendation fields.
    compact = canonical_json(dict(packet))
    skeleton = {
        "primary_route": {
            "route_id": "route-id",
            "title": "short route title",
            "summary": "bounded route summary",
            "steps": ["bounded validation step"],
        },
        "alternatives": [],
        "composition_decision": "KEEP_EXISTING",
        "base_decision": "KEEP_EXISTING",
        "existing_user_project_role": "bounded role of the current project",
        "external_repository_role": "bounded role of any verified external asset",
        "available_assets_used": [],
        "modules_to_keep": [],
        "modules_to_borrow": [],
        "modules_to_replace": [],
        "modules_to_delete_retire": [],
        "new_components_required": [],
        "why_primary": "why this route is preferred",
        "why_composition_preferred": "why this composition is preferred",
        "baseline_must_not_regress": [],
        "anti_tunnel_architecture_risks": [],
        "architecture_reset_triggers": [],
        "feasibility_summary": "bounded feasibility summary",
        "validation_plan": [],
        "important_risks": [],
        "open_questions": [],
    }
    allowed_decisions = ", ".join(COMPOSITION_DECISIONS)
    prompt = (
        "Perform one bounded FRESH CODEX feasibility review.\n"
        f"{method_evidence_policy_text(context='FRESH Project Blueprint')}\n"
        "Return exactly one top-level JSON object and nothing else. Use bare JSON: no Markdown, no json fence, no prose, and no wrapper object under blueprint, project_blueprint, result, or any other key.\n"
        "The object must contain exactly one non-empty primary_route object, an alternatives array with zero to four route values, and no primary/route/routes aliases.\n"
        f"composition_decision and base_decision are required and must both use the same exact allowed value: {allowed_decisions}.\n"
        "The remaining fields in the skeleton are required with the shown JSON types. Lists must remain arrays (never a string), contain no more than 16 non-empty strings, and must not be silently truncated.\n"
        "available_assets_used must contain only asset_id values present in the supplied asset pack; do not invent IDs.\n"
        "Do not emit paths, filenames, repository roots, credentials, secrets, browser state, transcript content, or production architecture. Escape LF, CR, and TAB inside JSON strings as \\n, \\r, and \\t; do not emit literal control characters.\n"
        "Do not select or start a Stage. Derive routes independently from the compact feasibility packet and keep the final result to one primary route.\n"
        "Complete top-level JSON skeleton (replace placeholder values, preserve keys and types):\n"
        f"{canonical_json(skeleton)}\n"
        f"Compact feasibility packet:\n{compact}"
    )
    if len(prompt.encode("utf-8")) > MAX_BLUEPRINT_BYTES:
        raise ProjectBlueprintError("FEASIBILITY_TOO_LARGE", "feasibility packet exceeds its bound")
    return prompt


def build_blueprint_prompt(
    project_root: str | os.PathLike[str] = ".",
    *,
    brief: Mapping[str, Any] | None = None,
) -> str:
    return _prompt_for(compact_codex_feasibility(project_root, brief=brief))


def _signature_call(target: Callable[..., Any], values: Mapping[str, Any]) -> Any:
    payload = dict(values)
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return target(project_url=payload["project_url"], mode=payload["mode"], prompt=payload["prompt"])
    parameters = list(signature.parameters.values())
    has_var_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters)
    aliases: dict[str, Any] = {
        "project_url": payload["project_url"],
        "url": payload["project_url"],
        "mode": payload["mode"],
        "consultation_mode": payload["mode"],
        "prompt": payload["prompt"],
        "packet": payload["packet"],
        "feasibility": payload["packet"],
        "feasibility_packet": payload["packet"],
        "context": payload["packet"],
        "evidence": payload["packet"],
        "request": payload["request"],
        "payload": payload["request"],
    }
    if has_var_kwargs:
        return target(**aliases)
    kwargs: dict[str, Any] = {}
    positional: list[Any] = []
    for parameter in parameters:
        if parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            continue
        if parameter.name in aliases:
            value = aliases[parameter.name]
        elif parameter.default is inspect.Parameter.empty and len(parameters) == 1:
            value = payload["request"]
        elif parameter.default is inspect.Parameter.empty:
            raise ProjectBlueprintError("CONSULTANT_SIGNATURE_INVALID", "consultant has an unsupported required parameter")
        else:
            continue
        if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        else:
            kwargs[parameter.name] = value
    return target(*positional, **kwargs)


def _call_consultant(consultant: Any, *, project_url: str, packet: Mapping[str, Any]) -> Any:
    if consultant is None:
        raise ProjectBlueprintError("CONSULTANT_REQUIRED", "an explicit one-shot blueprint consultant is required")
    target = getattr(consultant, "consult", consultant)
    if not callable(target):
        raise ProjectBlueprintError("CONSULTANT_INVALID", "consultant must be callable or expose consult()")
    prompt = _prompt_for(packet)
    request = {
        "project_url": project_url,
        "mode": CONSULTATION_MODE_FRESH,
        "prompt": prompt,
        "feasibility_digest": packet["feasibility_digest"],
    }
    values = {"project_url": project_url, "mode": CONSULTATION_MODE_FRESH, "prompt": prompt, "packet": copy.deepcopy(dict(packet)), "request": request}
    try:
        result = _signature_call(target, values)
        if is_bridge_envelope(result):
            try:
                # A real bridge envelope must prove that its completed receipt
                # belongs to this exact approved Project URL.  Raw mappings
                # remain valid for deterministic offline blueprint fixtures.
                return normalize_bridge_envelope(
                    result,
                    expected_project_url=project_url,
                    expected_mode=CONSULTATION_MODE_FRESH,
                    require_receipt=True,
                )
            except BridgeEnvelopeError as exc:
                raise ProjectBlueprintError(exc.code, str(exc)) from exc
        return result
    except ProjectBlueprintError:
        raise
    except Exception as exc:
        code = "CONSULTATION_RATE_LIMITED" if "limit" in type(exc).__name__.casefold() else "CONSULTATION_FAILED"
        raise ProjectBlueprintError(code, "one-shot blueprint consultation failed") from exc


def _map_step13_response_error(exc: ProjectDiscoveryError) -> None:
    """Translate the reused Step 13 grammar failure into a Step 14 error.

    Step 13 deliberately keeps its grammar diagnostics de-identified.  Carry
    only its closed-set branch token across the boundary; never copy the
    original exception, parser message, or response text into the blueprint
    error/state.
    """

    branch = getattr(exc, "grammar_branch", None)
    if exc.code == "DISCOVERY_SECRET_REJECTED":
        code = "BLUEPRINT_SECRET_REJECTED"
        branch = "SECRET"
    elif exc.code in {"DISCOVERY_INPUT_TOO_LARGE", "DISCOVERY_RESPONSE_TOO_LARGE"}:
        code = "BLUEPRINT_RESPONSE_TOO_LARGE"
        branch = branch or "LIMIT"
    else:
        code = "BLUEPRINT_RESPONSE_INVALID"
        branch = branch or "SEMANTIC_INVALID"
    branch_text = _sanitize_grammar_branch(branch)
    raise ProjectBlueprintError(
        code,
        f"blueprint response rejected [{branch_text}]",
        grammar_branch=branch_text,
    ) from None


def _parse_bounded_response_text(response: Any) -> dict[str, Any]:
    """Parse the finite Step 13 response grammar without retaining raw text."""

    if not isinstance(response, str):
        _response_invalid("SEMANTIC_INVALID")
    if "\x00" in response:
        _response_invalid("NUL")
    try:
        encoded = response.encode("utf-8")
    except UnicodeEncodeError:
        _response_invalid("INVALID_UTF8")
    if len(response) > MAX_RESPONSE_TEXT_LENGTH or len(encoded) > MAX_RESPONSE_BYTES:
        _response_invalid("TOO_LARGE")
    try:
        # This is the same bounded bare/fenced/balanced-object grammar as Step
        # 13.  It allows one safe JSON fence or delimiter-free prose around a
        # unique balanced object and reversibly escapes literal LF/CR/TAB only
        # inside JSON strings.  The helper never persists its input.
        return _parse_step13_response_text(response)
    except ProjectDiscoveryError as exc:
        _map_step13_response_error(exc)
    # ``_map_step13_response_error`` always raises; this keeps static type
    # checkers aware that this function has no fall-through value.
    raise AssertionError("unreachable")


def _precheck_response_value(value: Any) -> None:
    """Apply Step 13's path detector to already-decoded offline mappings."""

    try:
        _step13_response_value_path_precheck(value)
    except ProjectDiscoveryError as exc:
        _map_step13_response_error(exc)


def _reject_response_path_fields(value: Any, *, depth: int = 0) -> None:
    """Reject local path-bearing response fields before semantic projection."""

    if depth > MAX_ROUTE_DEPTH:
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str) and key.casefold().replace("-", "_") in _RESPONSE_PATH_KEYS:
                _response_invalid("ABSOLUTE_PATH")
            _reject_response_path_fields(child, depth=depth + 1)
        return
    if isinstance(value, list):
        for child in value:
            _reject_response_path_fields(child, depth=depth + 1)


def _string_list(
    value: Any,
    field: str,
    *,
    maximum: int = MAX_LIST_ITEMS,
    item_maximum: int = 4_000,
    missing: bool = True,
) -> list[str]:
    """Validate one canonical bounded list without coercion or truncation."""

    if value is _MISSING:
        if missing:
            return []
        _response_invalid("SEMANTIC_INVALID")
    if not isinstance(value, list):
        _response_invalid("SEMANTIC_INVALID")
    if len(value) > maximum:
        _response_invalid("LIMIT")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            _response_invalid("SEMANTIC_INVALID")
        try:
            text = _safe_text(item, field, required=True, max_length=item_maximum)
        except ProjectBlueprintError:
            _response_invalid("LIMIT" if len(item) > item_maximum else "SEMANTIC_INVALID")
        if text in result:
            # Do not silently deduplicate a consultant's decision list.  A
            # duplicate is an ambiguous/invalid response, not a reason to
            # mutate the selected plan.
            _response_invalid("SEMANTIC_INVALID")
        result.append(text)
    return result


def _route_identity(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("route_id", "id", "repo_url", "url", "name", "title"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                # Treat common route identity aliases as the same route.  A
                # primary must not reappear in alternatives under ``id`` vs
                # ``route_id`` (or URL/name) spelling.
                return "route:" + item.strip().casefold()
    if isinstance(value, str):
        return "text:" + value.strip().casefold()
    return "json:" + canonical_json(value)


def _route(value: Any, field: str, *, allow_string: bool = False) -> Any:
    if allow_string and isinstance(value, str):
        try:
            return _safe_text(value, field, required=True, max_length=8_000)
        except ProjectBlueprintError:
            _response_invalid("SEMANTIC_INVALID")
    if not isinstance(value, Mapping):
        _response_invalid("SEMANTIC_INVALID")
    if not value:
        _response_invalid("SEMANTIC_INVALID")
    try:
        _assert_safe(value, path=field)
    except ProjectBlueprintError as exc:
        if exc.code == "BLUEPRINT_SECRET_REJECTED":
            raise ProjectBlueprintError(
                "BLUEPRINT_SECRET_REJECTED",
                "blueprint response rejected [SECRET]",
                grammar_branch="SECRET",
            ) from None
        _response_invalid("LIMIT" if exc.code in {"BLUEPRINT_INPUT_TOO_LARGE", "BLUEPRINT_INPUT_TOO_DEEP"} else "SEMANTIC_INVALID")
    detached = copy.deepcopy(dict(value))
    return detached


_MISSING = object()
_RESPONSE_ROUTE_ALIASES = frozenset(
    {
        "primary",
        "route",
        "primary_recommendation",
        "selected_route",
        "routes",
        "alternative_routes",
        "decision",
        "base_route_decision",
    }
)


def _wrapper_entries(source: Mapping[str, Any]) -> list[tuple[str, Any]]:
    entries: list[tuple[str, Any]] = []
    for key, value in source.items():
        if isinstance(key, str) and key.casefold() in _BLUEPRINT_WRAPPER_KEYS:
            entries.append((key, value))
    return entries


def _unwrap_response_wrapper(response: Mapping[str, Any]) -> dict[str, Any]:
    """Unwrap only one explicit, unique blueprint/result wrapper.

    This intentionally does not recursively search arbitrary nested objects.
    A wrapper is eligible only when its root key is one of the three explicit
    names and its value is an object containing ``primary_route``.
    """

    source = dict(response)
    wrappers = _wrapper_entries(source)
    has_primary = "primary_route" in source
    if has_primary:
        if wrappers:
            # Even a non-object known wrapper beside a direct route is
            # ambiguous: silently ignoring it could mix two consultant shapes.
            _response_invalid("AMBIGUOUS_WRAPPER")
        if any(
            key != "primary_route"
            and isinstance(value, Mapping)
            and "primary_route" in value
            for key, value in source.items()
        ):
            _response_invalid("AMBIGUOUS_WRAPPER")
        return source
    if len(wrappers) > 1:
        _response_invalid("MULTIPLE_WRAPPERS")
    if not wrappers:
        return source
    _, wrapped = wrappers[0]
    if not isinstance(wrapped, Mapping):
        _response_invalid("WRAPPER_INVALID")
    if "primary_route" not in wrapped:
        _response_invalid("PRIMARY_REQUIRED", code="PRIMARY_ROUTE_REQUIRED")
    if _wrapper_entries(wrapped):
        _response_invalid("AMBIGUOUS_WRAPPER")
    # Do not merge root fields into the selected wrapper.  A caller that sends
    # semantic route fields at both levels is ambiguous and must fail closed.
    root_semantic_keys = set(source).intersection(
        {
            "alternatives",
            "composition_decision",
            "base_decision",
            "available_assets_used",
            "modules_to_keep",
            "modules_to_borrow",
            "modules_to_replace",
            "modules_to_delete_retire",
            "new_components_required",
            "assets_used",
            "keep_modules",
            "borrow_modules",
            "replace_modules",
            "modules_to_retire",
            "modules_to_delete",
            "new_components",
            "rationale",
            "summary",
            "user_project_role",
            "external_asset_role",
            "composition_rationale",
            "baseline_guardrails",
            "architecture_risks",
            "reset_triggers",
            "checks",
            "risks",
            "unresolved_questions",
        }
    )
    if root_semantic_keys:
        _response_invalid("AMBIGUOUS_WRAPPER")
    if any(
        key != wrappers[0][0]
        and isinstance(value, Mapping)
        and "primary_route" in value
        for key, value in source.items()
    ):
        _response_invalid("AMBIGUOUS_WRAPPER")
    return dict(wrapped)


def _response_decision(source: Mapping[str, Any]) -> str:
    present = [key for key in ("composition_decision", "base_decision") if key in source]
    # ``composition_decision`` is the explicit input spelling while
    # ``base_decision`` is the established manifest spelling.  Require at
    # least one spelling, and if both are supplied require agreement; do not
    # silently default a missing decision.
    if not present:
        _response_invalid("SEMANTIC_INVALID")
    values: list[str] = []
    for key in present:
        value = source[key]
        if not isinstance(value, str):
            _response_invalid("SEMANTIC_INVALID")
        candidate = value.strip()
        if candidate not in COMPOSITION_DECISIONS:
            _response_invalid("SEMANTIC_INVALID")
        values.append(candidate)
    if len(values) == 2 and values[0] != values[1]:
        _response_invalid("SEMANTIC_INVALID")
    return values[0]


def _response_field(source: Mapping[str, Any], canonical: str, *aliases: str) -> Any:
    """Read one semantic field without silently choosing between aliases."""

    names = (canonical, *aliases)
    present = [name for name in names if name in source]
    if len(present) > 1:
        _response_invalid("SEMANTIC_INVALID")
    return source[present[0]] if present else _MISSING


def _asset_ids_from_pack(asset_pack: Mapping[str, Any] | None) -> set[str] | None:
    if asset_pack is None:
        return None
    if not isinstance(asset_pack, Mapping):
        _response_invalid("ASSET_ID")
    raw_ids = asset_pack.get("asset_ids", _MISSING)
    raw_assets = asset_pack.get("assets", _MISSING)
    asset_ids: list[str] = []
    if raw_ids is not _MISSING:
        if not isinstance(raw_ids, list) or any(not isinstance(item, str) or not item.strip() for item in raw_ids):
            _response_invalid("ASSET_ID")
        asset_ids = list(raw_ids)
    if raw_assets is not _MISSING:
        if not isinstance(raw_assets, list):
            _response_invalid("ASSET_ID")
        from_assets: list[str] = []
        for item in raw_assets:
            if not isinstance(item, Mapping) or not isinstance(item.get("asset_id"), str) or not item["asset_id"].strip():
                _response_invalid("ASSET_ID")
            from_assets.append(item["asset_id"])
        if asset_ids and asset_ids != from_assets:
            _response_invalid("ASSET_ID")
        asset_ids = from_assets if not asset_ids else asset_ids
    if len(asset_ids) != len(set(asset_ids)):
        _response_invalid("ASSET_ID")
    return set(asset_ids)


def _normalise_response(
    response: Any,
    *,
    asset_pack: Mapping[str, Any] | None = None,
    available_assets: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if is_bridge_envelope(response):
        try:
            response = normalize_bridge_envelope(response)["response_text"]
        except BridgeEnvelopeError as exc:
            raise ProjectBlueprintError(exc.code, str(exc)) from exc
    if isinstance(response, str):
        response = _parse_bounded_response_text(response)
    elif isinstance(response, Mapping):
        _precheck_response_value(response)
    else:
        _response_invalid("ROOT_NOT_OBJECT")
    if not isinstance(response, Mapping):
        _response_invalid("ROOT_NOT_OBJECT")
    try:
        _assert_safe(response, path="response")
    except ProjectBlueprintError as exc:
        if exc.code == "BLUEPRINT_SECRET_REJECTED":
            raise ProjectBlueprintError(
                "BLUEPRINT_SECRET_REJECTED",
                "blueprint response rejected [SECRET]",
                grammar_branch="SECRET",
            ) from None
        _response_invalid("LIMIT" if exc.code in {"BLUEPRINT_INPUT_TOO_LARGE", "BLUEPRINT_INPUT_TOO_DEEP"} else "SEMANTIC_INVALID")
    _reject_response_path_fields(response)
    source = _unwrap_response_wrapper(response)
    for key in source:
        if isinstance(key, str) and key.casefold() in _RESPONSE_ROUTE_ALIASES:
            _response_invalid("LEGACY_PRIMARY_AMBIGUOUS")
    if "primary_route" not in source:
        _response_invalid("PRIMARY_REQUIRED", code="PRIMARY_ROUTE_REQUIRED")
    primary = _route(source["primary_route"], "primary_route", allow_string=False)
    primary_identity = _route_identity(primary)
    if "alternatives" not in source or not isinstance(source["alternatives"], list):
        _response_invalid("SEMANTIC_INVALID")
    alternatives_raw = source["alternatives"]
    if len(alternatives_raw) > MAX_ALTERNATIVES:
        _response_invalid("LIMIT")
    alternatives: list[Any] = []
    seen = {primary_identity}
    for raw in alternatives_raw:
        value = _route(raw, "alternatives", allow_string=True)
        if isinstance(value, Mapping) and str(value.get("role", "")).casefold() in {"primary", "recommended", "selected"}:
            _response_invalid("SEMANTIC_INVALID")
        identity = _route_identity(value)
        if identity in seen:
            _response_invalid("SEMANTIC_INVALID")
        seen.add(identity)
        alternatives.append(value)
    base_decision = _response_decision(source)
    response_asset_pack = asset_pack if asset_pack is not None else available_assets
    allowed_asset_ids = _asset_ids_from_pack(response_asset_pack)
    used_asset_ids = _string_list(
        _response_field(source, "available_assets_used", "assets_used"),
        "available_assets_used",
        item_maximum=200,
    )
    if allowed_asset_ids is not None and any(item not in allowed_asset_ids for item in used_asset_ids):
        _response_invalid("ASSET_ID")

    def text_field(name: str, *aliases: str, maximum: int = MAX_TEXT_LENGTH) -> str:
        value = _response_field(source, name, *aliases)
        if value is _MISSING:
            return ""
        if not isinstance(value, str):
            _response_invalid("SEMANTIC_INVALID")
        try:
            return _safe_text(value, name, max_length=maximum)
        except ProjectBlueprintError:
            _response_invalid("LIMIT" if len(value) > maximum else "SEMANTIC_INVALID")

    why_primary = text_field("why_primary", "rationale", maximum=MAX_TEXT_LENGTH)
    feasibility_summary = text_field("feasibility_summary", "summary", maximum=MAX_TEXT_LENGTH)
    existing_role = text_field("existing_user_project_role", "user_project_role", maximum=4_000)
    external_role = text_field("external_repository_role", "external_asset_role", maximum=4_000)
    result = {
        "primary_route": primary,
        "alternatives": alternatives,
        "composition_decision": base_decision,
        "base_decision": base_decision,
        "existing_user_project_role": existing_role,
        "external_repository_role": external_role,
        "available_assets_used": used_asset_ids,
        "modules_to_keep": _string_list(_response_field(source, "modules_to_keep", "keep_modules"), "modules_to_keep"),
        "modules_to_borrow": _string_list(_response_field(source, "modules_to_borrow", "borrow_modules"), "modules_to_borrow"),
        "modules_to_replace": _string_list(_response_field(source, "modules_to_replace", "replace_modules"), "modules_to_replace"),
        "modules_to_delete_retire": _string_list(
            _response_field(source, "modules_to_delete_retire", "modules_to_retire", "modules_to_delete"),
            "modules_to_delete_retire",
        ),
        "new_components_required": _string_list(
            _response_field(source, "new_components_required", "new_components"),
            "new_components_required",
        ),
        "why_primary": why_primary,
        "why_composition_preferred": text_field("why_composition_preferred", "composition_rationale"),
        "baseline_must_not_regress": _string_list(
            _response_field(source, "baseline_must_not_regress", "baseline_guardrails"),
            "baseline_must_not_regress",
        ),
        "anti_tunnel_architecture_risks": _string_list(
            _response_field(source, "anti_tunnel_architecture_risks", "architecture_risks"),
            "anti_tunnel_architecture_risks",
        ),
        "architecture_reset_triggers": _string_list(
            _response_field(source, "architecture_reset_triggers", "reset_triggers"),
            "architecture_reset_triggers",
        ),
        "feasibility_summary": feasibility_summary,
        "validation_plan": _string_list(_response_field(source, "validation_plan", "checks"), "validation_plan"),
        "important_risks": _string_list(_response_field(source, "important_risks", "risks"), "important_risks"),
        "open_questions": _string_list(_response_field(source, "open_questions", "unresolved_questions"), "open_questions"),
        "no_production_architecture_selected": True,
        "no_stage_started": True,
    }
    _assert_safe(result)
    return result


normalize_blueprint_response = _normalise_response
normalise_blueprint_response = _normalise_response


def _read_json(path: Path, code: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ProjectBlueprintError(code, "blueprint manifest path is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectBlueprintError(code, "blueprint manifest could not be read") from exc
    if not isinstance(value, dict):
        raise ProjectBlueprintError(code, "blueprint manifest must contain an object")
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> bool:
    """Write one canonical JSON object, preserving bytes on idempotent reuse."""

    if path.exists() and (path.is_symlink() or path.is_dir()):
        raise ProjectBlueprintError("FEASIBILITY_WRITE_FAILED", "canonical feasibility path is not a regular file")
    text = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(text.encode("utf-8")) > MAX_FEASIBILITY_BYTES:
        raise ProjectBlueprintError("FEASIBILITY_TOO_LARGE", "canonical feasibility exceeds its bound")
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == text:
                return True
        except (OSError, UnicodeError) as exc:
            raise ProjectBlueprintError("FEASIBILITY_WRITE_FAILED", "canonical feasibility could not be read") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    fd: int | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            handle.write(text)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise ProjectBlueprintError("FEASIBILITY_WRITE_FAILED", "canonical feasibility could not be written") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return False


def _atomic_text(path: Path, text: str) -> None:
    if path.exists() and (path.is_symlink() or path.is_dir()):
        raise ProjectBlueprintError("BLUEPRINT_WRITE_FAILED", "canonical blueprint path is not a regular file")
    if len(text.encode("utf-8")) > MAX_BLUEPRINT_BYTES:
        raise ProjectBlueprintError("BLUEPRINT_TOO_LARGE", "canonical blueprint exceeds its bound")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    fd: int | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = None
            handle.write(text)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise ProjectBlueprintError("BLUEPRINT_WRITE_FAILED", "canonical blueprint could not be written") from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _markdown(*, project_id: str, brief_digest: str, context_digest: str, discovery_digest: str, feasibility_digest: str, result: Mapping[str, Any]) -> str:
    semantic = {
        "schema_version": PROJECT_BLUEPRINT_SCHEMA_VERSION,
        "project_id": project_id,
        "brief_digest": brief_digest,
        "context_digest": context_digest,
        "discovery_digest": discovery_digest,
        "feasibility_digest": feasibility_digest,
        "status": PROJECT_BLUEPRINT_STATUS,
        "next_action": READY_FOR_STAGE_PLANNING,
        "primary_route": copy.deepcopy(result["primary_route"]),
        "alternatives": copy.deepcopy(result["alternatives"]),
        "composition_decision": result["composition_decision"],
        "base_decision": result["base_decision"],
        "existing_user_project_role": result["existing_user_project_role"],
        "external_repository_role": result["external_repository_role"],
        "available_assets_used": copy.deepcopy(result["available_assets_used"]),
        "modules_to_keep": copy.deepcopy(result["modules_to_keep"]),
        "modules_to_borrow": copy.deepcopy(result["modules_to_borrow"]),
        "modules_to_replace": copy.deepcopy(result["modules_to_replace"]),
        "modules_to_delete_retire": copy.deepcopy(result["modules_to_delete_retire"]),
        "new_components_required": copy.deepcopy(result["new_components_required"]),
        "why_primary": result["why_primary"],
        "why_composition_preferred": result["why_composition_preferred"],
        "baseline_must_not_regress": copy.deepcopy(result["baseline_must_not_regress"]),
        "anti_tunnel_architecture_risks": copy.deepcopy(result["anti_tunnel_architecture_risks"]),
        "architecture_reset_triggers": copy.deepcopy(result["architecture_reset_triggers"]),
        "feasibility_summary": result["feasibility_summary"],
        "validation_plan": copy.deepcopy(result["validation_plan"]),
        "important_risks": copy.deepcopy(result["important_risks"]),
        "open_questions": copy.deepcopy(result["open_questions"]),
        "stage_created": False,
        "stage_started": False,
    }
    return (
        "# Project Blueprint\n\n"
        f"<!-- marker: {PROJECT_BLUEPRINT_MARKER} -->\n"
        "This is a bounded Step 14 route hand-off. It is not Stage approval.\n\n"
        "```json\n"
        + json.dumps(semantic, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n```\n"
    )


def _manifest_valid(manifest: Mapping[str, Any], markdown: str, packet: Mapping[str, Any]) -> bool:
    try:
        validate_instance(dict(manifest), load_schema("blueprint_manifest"))
    except ContractValidationError:
        return False
    expected = {
        "project_id": packet["project_id"],
        "brief_digest": packet["brief_digest"],
        "context_digest": packet["context_digest"],
        "discovery_digest": packet["discovery_digest"],
        "feasibility_digest": packet["feasibility_digest"],
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        return False
    if hashlib.sha256(markdown.encode("utf-8")).hexdigest() != manifest.get("blueprint_digest"):
        return False
    return True


def _inventory(root: Path) -> list[str]:
    research = root / ".research"
    if not research.exists():
        return []
    return sorted(
        item.relative_to(root).as_posix()
        for item in research.rglob("*")
        if item.is_file() and ".git" not in item.parts
    )


def _result_from_manifest(manifest: Mapping[str, Any], root: Path, *, reused: bool) -> dict[str, Any]:
    """Expose a project-blueprint envelope while keeping manifest versioning separate."""

    result = copy.deepcopy(dict(manifest))
    result["schema_version"] = PROJECT_BLUEPRINT_SCHEMA_VERSION
    result["manifest_schema_version"] = BLUEPRINT_MANIFEST_SCHEMA_VERSION
    result["persistent_inventory"] = _inventory(root)
    result["consultation_count"] = 1
    result["gpt_calls"] = 1
    result["external_calls"] = 1
    result["idempotent_reuse"] = reused
    return result


def build_project_blueprint(
    project_root: str | os.PathLike[str] = ".",
    *,
    consultant: Any = None,
    consult: Any = None,
    brief: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one bounded blueprint after all three canonical preconditions."""

    if consultant is None:
        consultant = consult
    root = resolve_project_root(project_root)
    packet = compact_codex_feasibility(root, brief=brief)
    blueprint_path = root / PROJECT_BLUEPRINT_RELATIVE_PATH
    manifest_path = root / BLUEPRINT_MANIFEST_RELATIVE_PATH
    existing = _read_json(manifest_path, "BLUEPRINT_MANIFEST_INVALID")
    if existing is not None:
        if not blueprint_path.is_file() or blueprint_path.is_symlink():
            raise ProjectBlueprintError("BLUEPRINT_INVALID", "blueprint manifest exists without a regular Markdown artifact")
        try:
            existing_markdown = blueprint_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ProjectBlueprintError("BLUEPRINT_INVALID", "canonical blueprint cannot be read") from exc
        if _manifest_valid(existing, existing_markdown, packet):
            return _result_from_manifest(existing, root, reused=True)
        # A malformed artifact is not silently replaced.  This avoids hiding
        # tampering; callers can remove a stale blueprint only through an
        # explicit user action before requesting a new input revision.
        raise ProjectBlueprintError("BLUEPRINT_INPUT_MISMATCH", "existing blueprint is not bound to current feasibility evidence")
    response = _call_consultant(consultant, project_url=packet["project_url"], packet=packet)
    transport = response if is_bridge_envelope(response) else None
    # The canonical asset pack is already bounded and verified locally.  Pass
    # only its IDs into response normalization so consultant-selected assets
    # cannot introduce an unverified identifier; no raw response is retained.
    normalized = _normalise_response(response, asset_pack=packet.get("asset_pack"))
    markdown = _markdown(
        project_id=packet["project_id"],
        brief_digest=packet["brief_digest"],
        context_digest=packet["context_digest"],
        discovery_digest=packet["discovery_digest"],
        feasibility_digest=packet["feasibility_digest"],
        result=normalized,
    )
    blueprint_digest = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    claims_digest = sha256_json(normalized)
    manifest: dict[str, Any] = {
        "schema_version": BLUEPRINT_MANIFEST_SCHEMA_VERSION,
        "project_id": packet["project_id"],
        "brief_digest": packet["brief_digest"],
        "context_digest": packet["context_digest"],
        "discovery_digest": packet["discovery_digest"],
        "feasibility_digest": packet["feasibility_digest"],
        "status": PROJECT_BLUEPRINT_STATUS,
        "next_action": READY_FOR_STAGE_PLANNING,
        "blueprint_path": PROJECT_BLUEPRINT_RELATIVE_PATH.as_posix(),
        "blueprint_digest": blueprint_digest,
        "primary_route": copy.deepcopy(normalized["primary_route"]),
        "alternatives": copy.deepcopy(normalized["alternatives"]),
        "composition_decision": normalized["composition_decision"],
        "base_decision": normalized["base_decision"],
        "existing_user_project_role": normalized["existing_user_project_role"],
        "external_repository_role": normalized["external_repository_role"],
        "available_assets_used": copy.deepcopy(normalized["available_assets_used"]),
        "modules_to_keep": copy.deepcopy(normalized["modules_to_keep"]),
        "modules_to_borrow": copy.deepcopy(normalized["modules_to_borrow"]),
        "modules_to_replace": copy.deepcopy(normalized["modules_to_replace"]),
        "modules_to_delete_retire": copy.deepcopy(normalized["modules_to_delete_retire"]),
        "new_components_required": copy.deepcopy(normalized["new_components_required"]),
        "why_primary": normalized["why_primary"],
        "why_composition_preferred": normalized["why_composition_preferred"],
        "baseline_must_not_regress": copy.deepcopy(normalized["baseline_must_not_regress"]),
        "anti_tunnel_architecture_risks": copy.deepcopy(normalized["anti_tunnel_architecture_risks"]),
        "architecture_reset_triggers": copy.deepcopy(normalized["architecture_reset_triggers"]),
        "feasibility_summary": normalized["feasibility_summary"],
        "validation_plan": copy.deepcopy(normalized["validation_plan"]),
        "important_risks": copy.deepcopy(normalized["important_risks"]),
        "open_questions": copy.deepcopy(normalized["open_questions"]),
        "consultation": {
            "mode": CONSULTATION_MODE_FRESH,
            "project_url": sanitize_project_url(packet["project_url"]),
            "request_count": 1,
            "claims_digest": claims_digest,
            "feasibility_digest": packet["feasibility_digest"],
        },
        "stage_created": False,
        "stage_started": False,
        "production_architecture_selected": False,
        "marker": PROJECT_BLUEPRINT_MARKER,
        "manifest_path": BLUEPRINT_MANIFEST_RELATIVE_PATH.as_posix(),
        "available_assets_digest": packet.get("available_assets_digest", ""),
        "asset_pack_digest": packet.get("asset_pack_digest", ""),
        "asset_layer_marker": ASSET_LAYER_MARKER,
    }
    if isinstance(transport, Mapping):
        # Receipt-safe bridge metadata is useful for Project binding audit but
        # never includes the raw prompt or assistant response.
        manifest["bridge_receipt"] = {
            "consultation_id": transport.get("consultation_id"),
            "conversation_id": transport.get("conversation_id"),
            "receipt_path": transport.get("receipt_path"),
            "project_url": transport.get("project_url", packet["project_url"]),
            "mode": transport.get("mode", CONSULTATION_MODE_FRESH),
            "request_count": 1,
            "receipt": copy.deepcopy(transport.get("receipt", {})),
        }
    manifest["manifest_digest"] = sha256_json(manifest)
    _assert_safe(manifest)
    try:
        validate_instance(manifest, load_schema("blueprint_manifest"))
    except ContractValidationError as exc:
        raise ProjectBlueprintError("BLUEPRINT_SCHEMA_INVALID", "generated blueprint manifest failed schema") from exc
    # Each output is independently atomic; no sibling temporary artifact is
    # retained.  Write Markdown first, then its binding manifest.
    _atomic_text(blueprint_path, markdown)
    _atomic_text(manifest_path, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return _result_from_manifest(manifest, root, reused=False)


generate_project_blueprint = build_project_blueprint
run_project_blueprint = build_project_blueprint
perform_project_blueprint = build_project_blueprint
create_project_blueprint = build_project_blueprint
build_blueprint = build_project_blueprint
generate_blueprint = build_project_blueprint
run_blueprint = build_project_blueprint


def load_project_blueprint(project_root: str | os.PathLike[str] = ".") -> dict[str, Any]:
    root = resolve_project_root(project_root)
    manifest = _read_json(root / BLUEPRINT_MANIFEST_RELATIVE_PATH, "BLUEPRINT_NOT_FOUND")
    if manifest is None:
        raise ProjectBlueprintError("BLUEPRINT_NOT_FOUND", "no project blueprint exists")
    blueprint_path = root / PROJECT_BLUEPRINT_RELATIVE_PATH
    if blueprint_path.is_symlink() or not blueprint_path.is_file():
        raise ProjectBlueprintError("BLUEPRINT_INVALID", "canonical blueprint Markdown is missing")
    try:
        markdown = blueprint_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ProjectBlueprintError("BLUEPRINT_INVALID", "canonical blueprint Markdown cannot be read") from exc
    try:
        validate_instance(manifest, load_schema("blueprint_manifest"))
    except ContractValidationError as exc:
        raise ProjectBlueprintError("BLUEPRINT_INVALID", "blueprint manifest failed schema validation") from exc
    if hashlib.sha256(markdown.encode("utf-8")).hexdigest() != manifest.get("blueprint_digest"):
        raise ProjectBlueprintError("BLUEPRINT_DIGEST_MISMATCH", "blueprint Markdown does not match its manifest")
    result = _result_from_manifest(manifest, root, reused=True)
    result.update({"blueprint_markdown": markdown, "path": PROJECT_BLUEPRINT_RELATIVE_PATH.as_posix()})
    return result


load_blueprint = load_project_blueprint


def verify_project_blueprint(project_root: str | os.PathLike[str] = ".") -> dict[str, Any]:
    loaded = load_project_blueprint(project_root)
    return {
        "passed": True,
        "marker": PROJECT_BLUEPRINT_MARKER,
        "status": loaded["status"],
        "next_action": loaded["next_action"],
        "path": PROJECT_BLUEPRINT_RELATIVE_PATH.as_posix(),
        "manifest_path": BLUEPRINT_MANIFEST_RELATIVE_PATH.as_posix(),
        "project_id": loaded["project_id"],
        "persistent_inventory": loaded["persistent_inventory"],
    }


verify_blueprint = verify_project_blueprint


def validate_project_blueprint(report: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(report, Mapping):
        raise ProjectBlueprintError("BLUEPRINT_INVALID", "blueprint report must be an object")
    try:
        validate_instance(dict(report), load_schema("project_blueprint"))
    except ContractValidationError as exc:
        raise ProjectBlueprintError("BLUEPRINT_INVALID", "blueprint report failed schema validation") from exc
    return copy.deepcopy(dict(report))


class ProjectBlueprint:
    """Object facade for the bounded Step 14 service."""

    def __init__(self, project_root: str | os.PathLike[str] = ".", *, consultant: Any = None, consult: Any = None) -> None:
        self.root = resolve_project_root(project_root)
        self.consultant = consultant if consultant is not None else consult

    def run(self, **kwargs: Any) -> dict[str, Any]:
        return build_project_blueprint(
            self.root,
            consultant=kwargs.pop("consultant", self.consultant),
            **kwargs,
        )

    def build(self, **kwargs: Any) -> dict[str, Any]:
        return self.run(**kwargs)

    def load(self) -> dict[str, Any]:
        return load_project_blueprint(self.root)


__all__ = [
    "BLUEPRINT_MANIFEST_FILENAME",
    "BLUEPRINT_MANIFEST_PATH",
    "BLUEPRINT_MANIFEST_RELATIVE_PATH",
    "BLUEPRINT_MANIFEST_SCHEMA_VERSION",
    "BLUEPRINT_MARKER",
    "BLUEPRINT_STATUS",
    "BLUEPRINT_GRAMMAR_BRANCHES",
    "CODEX_FEASIBILITY",
    "CONSULTATION_MODE",
    "FRESH",
    "MAX_ALTERNATIVES",
    "MAX_RESPONSE_BYTES",
    "MAX_RESPONSE_PROSE_LENGTH",
    "MAX_RESPONSE_TEXT_LENGTH",
    "PROJECT_BLUEPRINT_FILENAME",
    "PROJECT_BLUEPRINT_MARKER",
    "PROJECT_BLUEPRINT_MANIFEST_PATH",
    "PROJECT_BLUEPRINT_MANIFEST_RELATIVE_PATH",
    "PROJECT_BLUEPRINT_PATH",
    "PROJECT_BLUEPRINT_RELATIVE_PATH",
    "PROJECT_BLUEPRINT_SCHEMA_VERSION",
    "PROJECT_BLUEPRINT_STATUS",
    "READY_FOR_STAGE_PLANNING",
    "BlueprintConsultant",
    "BlueprintError",
    "ProjectBlueprint",
    "ProjectBlueprintError",
    "ProjectBlueprintFailure",
    "build_blueprint_prompt",
    "build_blueprint",
    "build_codex_feasibility",
    "build_feasibility_packet",
    "build_project_blueprint",
    "compact_codex_feasibility",
    "compact_feasibility",
    "create_project_blueprint",
    "generate_project_blueprint",
    "generate_blueprint",
    "load_blueprint",
    "load_project_blueprint",
    "normalise_blueprint_response",
    "normalize_blueprint_response",
    "perform_project_blueprint",
    "run_project_blueprint",
    "run_blueprint",
    "validate_project_blueprint",
    "verify_blueprint",
    "verify_project_blueprint",
]
