"""Step 15: turn a verified Bootstrap hand-off into one planned Stage.

Step 14 deliberately stops at ``READY_FOR_STAGE_PLANNING``.  This module is
the small, explicit boundary after that hand-off.  It reads the canonical
Step 11--14 artifacts, derives one deterministic ``stage_contract.v1`` and
registers it through :class:`src.stage_controller.StageController`.

The planner is intentionally local and finite.  It does not call the browser
bridge, infer a production architecture, start a Stage, or execute a route.
The only persistent outputs it owns are the Stage contract, the controller
state written by the controller, and a bounded Stage planning envelope.
Malformed or incomplete Bootstrap evidence fails closed before any Stage is
registered.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractValidationError, canonical_json, load_schema, sha256_json, validate_instance
from .project_blueprint import (
    BLUEPRINT_MANIFEST_RELATIVE_PATH,
    PROJECT_BLUEPRINT_MARKER,
    PROJECT_BLUEPRINT_RELATIVE_PATH,
    ProjectBlueprintError,
    load_project_blueprint,
)
from .project_context import ProjectContextError, load_project_context
from .project_discovery import (
    DISCOVERY_REPORT_RELATIVE_PATH,
    DISCOVERY_REPORT_MARKER,
    DISCOVERY_STATUS,
    ProjectDiscoveryError,
    load_discovery_report,
)
from .project_intake import ProjectIntakeError, validate_project_brief
from .project_state import ProjectStateError, load_project_brief, resolve_project_root
from .design_package import DesignPackageError, validate_design_package
from .design_review import DesignReviewError, design_summary_digest, normalize_design_summary
from .stage_controller import (
    StageController,
    StageControllerError,
    StageState,
    validate_stage_contract_v1,
)
from .research_prompt_policy import method_evidence_policy_reference


STAGE_PLANNING_SCHEMA_VERSION = "stage_planning.v1"
STAGE_PLANNING_MARKER = "STAGE_PLANNING_PASS"
STAGE_PLANNING_STATUS = "PLANNED"
STAGE_PLANNING_NEXT_ACTION = "USER_START_STAGE"
STAGE_PLAN_RELATIVE_PATH = Path(".research") / "stage-planning" / "STAGE_PLAN.json"
STAGE_PLAN_PATH = STAGE_PLAN_RELATIVE_PATH
STAGE_PLAN_FILENAME = STAGE_PLAN_RELATIVE_PATH.name
BOOTSTRAP_STATE_RELATIVE_PATH = Path(".research") / "bootstrap_state.json"
BOOTSTRAP_STATE_PATH = BOOTSTRAP_STATE_RELATIVE_PATH
STAGE_STATE_RELATIVE_PATH = Path(".research") / "stage-state.json"
STAGE_STATE_PATH = STAGE_STATE_RELATIVE_PATH
STAGE_CONTRACT_FILENAME = "contract.json"
DESIGN_PACKAGE_RELATIVE_PATH = Path(".research") / "DESIGN_PACKAGE.json"
DESIGN_PACKAGE_PATH = DESIGN_PACKAGE_RELATIVE_PATH
HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH = Path(".research") / "design-review" / "HUMAN_ACCEPT_RECEIPT.json"
HUMAN_ACCEPT_RECEIPT_PATH = HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH
EXECUTION_HANDOFF_RELATIVE_PATH = Path(".research") / "execution-handoff" / "EXECUTION_HANDOFF.json"
EXECUTION_HANDOFF_PATH = EXECUTION_HANDOFF_RELATIVE_PATH
EXECUTION_HANDOFF_SCHEMA_VERSION = "execution_handoff.v1"
EXECUTION_HANDOFF_MARKER = "EXECUTION_HANDOFF_PASS"
HUMAN_ACCEPT_RECEIPT_SCHEMA_VERSION = "human_design_accept_receipt.v1"
BOOTSTRAP_STATE_SCHEMA_VERSION = "bootstrap_state.v1"
BOOTSTRAP_STATE_STATUS = "BOOTSTRAP_COMPLETE"
BOOTSTRAP_STATE_MARKER = "PROJECT_BOOTSTRAP_RESEARCH_PASS"
BOOTSTRAP_STATE_NEXT_ACTION = "READY_FOR_STAGE_PLANNING"
DISCOVERY_STATE_PATH = DISCOVERY_REPORT_RELATIVE_PATH.as_posix()
BLUEPRINT_STATE_PATH = PROJECT_BLUEPRINT_RELATIVE_PATH.as_posix()

MAX_STAGE_PLAN_BYTES = 120_000
MAX_BOOTSTRAP_STATE_BYTES = 120_000
MAX_ARTIFACT_BYTES = 180_000
MAX_TEXT_LENGTH = 12_000
MAX_LIST_ITEMS = 32
_STAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _accepted_stage_plan_relative(stage_id: str) -> str:
    """Return the immutable per-Stage planning envelope path."""

    if _STAGE_ID_RE.fullmatch(stage_id) is None:
        raise _fail("STAGE_ID_INVALID", "stage_id must contain only letters, numbers, dot, underscore, or hyphen")
    return f".research/stages/{stage_id}/{STAGE_PLAN_FILENAME}"

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "client_secret",
        "cookie",
        "cookies",
        "dom",
        "local_storage",
        "password",
        "passwd",
        "prompt",
        "raw_dom",
        "raw_response",
        "secret",
        "session",
        "session_id",
        "session_storage",
        "storage_state",
        "token",
        "tokens",
        "transcript",
        "messages",
        "turns",
        "chat_history",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|sk-proj-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)


class StagePlanningError(RuntimeError):
    """A stable, fail-closed Step 15 error."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


# Compatibility spellings make this layer convenient for small integrations.
StagePlanError = StagePlanningError
PlanningError = StagePlanningError


def _fail(code: str, message: str, exc: BaseException | None = None) -> StagePlanningError:
    error = StagePlanningError(code, message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _assert_safe(value: Any, *, path: str = "$", depth: int = 0) -> None:
    """Reject credentials and raw transport material before persistence."""

    if depth > 14:
        raise _fail("STAGE_PLAN_TOO_DEEP", f"input nesting is too deep at {path}")
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).strip().lower().replace("-", "_")
            if key_text in _SENSITIVE_KEYS:
                raise _fail("STAGE_PLAN_SECRET_REJECTED", f"sensitive field is not allowed at {path}")
            _assert_safe(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_LIST_ITEMS * 8:
            raise _fail("STAGE_PLAN_LIMIT", f"list is too large at {path}")
        for index, child in enumerate(value):
            _assert_safe(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str):
        if "\x00" in value:
            raise _fail("STAGE_PLAN_INVALID_TEXT", f"NUL character is not allowed at {path}")
        for pattern in _SECRET_PATTERNS:
            if pattern.search(value):
                raise _fail("STAGE_PLAN_SECRET_REJECTED", f"secret-like value is not allowed at {path}")


def _text(value: Any, field: str, *, required: bool = True, max_length: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise _fail("STAGE_PLAN_INPUT_INVALID", f"{field} must be a string")
    result = value.strip()
    if required and not result:
        raise _fail("STAGE_PLAN_INPUT_INVALID", f"{field} must be non-empty")
    if len(result) > max_length or "\x00" in result:
        raise _fail("STAGE_PLAN_INPUT_INVALID", f"{field} is invalid or too long")
    return result


def _relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise _fail("STAGE_PLAN_PATH_INVALID", f"{field} must be a relative path")
    candidate = value.replace("\\", "/").strip()
    if not candidate or candidate.startswith("/") or re.match(r"^[A-Za-z]:", candidate):
        raise _fail("STAGE_PLAN_PATH_INVALID", f"{field} must be relative")
    parts = candidate.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _fail("STAGE_PLAN_PATH_INVALID", f"{field} contains an unsafe component")
    return "/".join(parts)


def _read_json(path: Path, field: str, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise _fail(f"{field.upper()}_NOT_FOUND", f"{field} is not a regular file")
    try:
        if path.stat().st_size > max_bytes:
            raise _fail(f"{field.upper()}_TOO_LARGE", f"{field} exceeds its bounded size")
        value = json.loads(path.read_text(encoding="utf-8"))
    except StagePlanningError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _fail(f"{field.upper()}_INVALID", f"{field} could not be read") from exc
    if not isinstance(value, dict):
        raise _fail(f"{field.upper()}_INVALID", f"{field} must contain an object")
    _assert_safe(value, path=field)
    return value


def _file_digest(path: Path, field: str) -> tuple[str, int]:
    if path.is_symlink() or not path.is_file():
        raise _fail(f"{field.upper()}_NOT_FOUND", f"{field} is not a regular file")
    try:
        size = path.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise _fail(f"{field.upper()}_TOO_LARGE", f"{field} exceeds its bounded size")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except StagePlanningError:
        raise
    except OSError as exc:
        raise _fail(f"{field.upper()}_UNREADABLE", f"{field} could not be read", exc) from exc
    return digest.hexdigest(), size


def _atomic_json(path: Path, value: Mapping[str, Any], *, max_bytes: int = MAX_STAGE_PLAN_BYTES) -> None:
    _assert_safe(value, path=str(path))
    encoded = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > max_bytes:
        raise _fail("STAGE_PLAN_TOO_LARGE", f"{path.name} exceeds its bounded size")
    if path.exists() and (path.is_symlink() or path.is_dir()):
        raise _fail("STAGE_PLAN_WRITE_FAILED", f"{path.name} is not a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise _fail("STAGE_PLAN_WRITE_FAILED", f"could not write {path.name}", exc) from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _digest_from_file(path: Path, field: str) -> str:
    return _file_digest(path, field)[0]


def _validate_digest(value: Any, field: str) -> str:
    result = _text(value, field, max_length=64)
    if _HEX64_RE.fullmatch(result) is None:
        raise _fail("STAGE_PLAN_DIGEST_INVALID", f"{field} must be a lowercase SHA-256 digest")
    return result


def _human_accept_receipt_digest(receipt: Mapping[str, Any]) -> str:
    """Return the self-binding digest for one Human ACCEPT receipt."""

    detached = copy.deepcopy(dict(receipt))
    detached.pop("receipt_digest", None)
    return sha256_json(detached)


def build_human_accept_receipt(
    *,
    project_id: str,
    brief_digest: str,
    design_digest: str,
    actor: str,
    accepted_at: str | None = None,
    source: str = "runtime_design_review_acceptance",
) -> dict[str, Any]:
    """Build a bounded, digest-bound record of an explicit Human ACCEPT.

    The receipt is a consistency record, rather than a signature.  Its inputs
    are the already validated project identity, requirement digest, and design
    digest.  Callers persist it atomically and later verification rechecks the
    self-binding digest and all three identities.
    """

    payload: dict[str, Any] = {
        "schema_version": HUMAN_ACCEPT_RECEIPT_SCHEMA_VERSION,
        "decision": "ACCEPT",
        "actor": _text(actor, "actor", max_length=256),
        "project_id": _text(project_id, "project_id", max_length=200),
        "brief_digest": _validate_digest(brief_digest, "brief_digest"),
        "design_digest": _validate_digest(design_digest, "design_digest"),
        "source": _text(source, "source", max_length=128),
    }
    if accepted_at is not None:
        payload["accepted_at"] = _text(accepted_at, "accepted_at", max_length=128)
    payload["receipt_digest"] = _human_accept_receipt_digest(payload)
    return payload


def validate_human_accept_receipt(
    value: Mapping[str, Any],
    *,
    project_id: str,
    brief_digest: str,
    design_digest: str,
) -> dict[str, Any]:
    """Validate one Human ACCEPT receipt against the current workflow facts."""

    if not isinstance(value, Mapping):
        raise _fail("HUMAN_ACCEPT_RECEIPT_INVALID", "Human ACCEPT receipt must be an object")
    detached = copy.deepcopy(dict(value))
    _assert_safe(detached, path="human_accept_receipt")
    if detached.get("schema_version") != HUMAN_ACCEPT_RECEIPT_SCHEMA_VERSION:
        raise _fail("HUMAN_ACCEPT_RECEIPT_INVALID", "Human ACCEPT receipt schema is invalid")
    if detached.get("decision") != "ACCEPT":
        raise _fail("HUMAN_ACCEPT_RECEIPT_INVALID", "Human ACCEPT receipt decision is not ACCEPT")
    if detached.get("project_id") != project_id:
        raise _fail("HUMAN_ACCEPT_RECEIPT_IDENTITY_MISMATCH", "Human ACCEPT receipt project identity differs")
    if detached.get("brief_digest") != _validate_digest(brief_digest, "brief_digest"):
        raise _fail("HUMAN_ACCEPT_RECEIPT_BINDING_MISMATCH", "Human ACCEPT receipt brief digest is stale")
    if detached.get("design_digest") != _validate_digest(design_digest, "design_digest"):
        raise _fail("HUMAN_ACCEPT_RECEIPT_BINDING_MISMATCH", "Human ACCEPT receipt design digest is stale")
    actor = detached.get("actor")
    if not isinstance(actor, str) or not actor.strip():
        raise _fail("HUMAN_ACCEPT_RECEIPT_INVALID", "Human ACCEPT receipt actor is missing")
    if not isinstance(detached.get("source"), str) or not detached["source"].strip():
        raise _fail("HUMAN_ACCEPT_RECEIPT_INVALID", "Human ACCEPT receipt source is missing")
    receipt_digest = detached.get("receipt_digest")
    if not isinstance(receipt_digest, str) or _HEX64_RE.fullmatch(receipt_digest) is None:
        raise _fail("HUMAN_ACCEPT_RECEIPT_DIGEST_INVALID", "Human ACCEPT receipt digest is invalid")
    if receipt_digest != _human_accept_receipt_digest(detached):
        raise _fail("HUMAN_ACCEPT_RECEIPT_DIGEST_MISMATCH", "Human ACCEPT receipt digest is inconsistent")
    return detached


def _read_design_package(root: Path, path: Path) -> tuple[dict[str, Any], str, str, int]:
    """Read the canonical Design Package and return logical/raw digests."""

    package = _read_json(path, "design package")
    try:
        checked = validate_design_package(package)
    except DesignPackageError as exc:
        raise _fail("DESIGN_PACKAGE_INVALID", "Design Package failed validation", exc) from exc
    _assert_safe(checked, path="design_package")
    raw_digest, size = _file_digest(path, "design package")
    return checked, sha256_json(checked), raw_digest, size


def _resolve_receipt_path(root: Path, reference: Any) -> tuple[str, Path]:
    """Resolve a consultation id or repo-relative receipt path safely."""

    if not isinstance(reference, str) or not reference.strip():
        raise _fail("RESEARCH_RECEIPT_INVALID", "research receipt reference must be a non-empty string")
    text = reference.strip().replace("\\", "/")
    candidate_text = text if text.endswith("receipt.json") else f".consultations/{text}/receipt.json"
    relative = _relative_path(candidate_text, "research receipt path")
    candidate = root.joinpath(*relative.split("/"))
    if candidate.is_symlink() or not candidate.is_file():
        raise _fail("RESEARCH_RECEIPT_NOT_FOUND", "research receipt is not a regular file")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _fail("RESEARCH_RECEIPT_PATH_INVALID", "research receipt escaped the workspace", exc) from exc
    return relative, candidate


def _provenance_references(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, Mapping):
        for key in ("paths", "receipts", "receipt_paths", "references"):
            candidate = value.get(key)
            if isinstance(candidate, (list, tuple)):
                return list(candidate)
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _load_research_provenance(
    root: Path,
    *,
    brief: Mapping[str, Any],
    accepted_review: Mapping[str, Any],
    supplied: Any = None,
) -> list[dict[str, Any]]:
    """Resolve and verify bounded research receipts for an accepted design."""

    refs = _provenance_references(supplied)
    if not refs:
        reference = accepted_review.get("gpt_reconsultation_ref")
        if isinstance(reference, str) and reference.strip():
            refs = [reference]
    if not refs:
        # A direct planner caller may omit the consultation reference.  Select
        # only complete receipts under this workspace and bind the selected
        # files by digest; no receipt content is copied into the handoff.
        receipt_root = root / ".consultations"
        if receipt_root.is_dir() and not receipt_root.is_symlink():
            refs = [path.relative_to(root).as_posix() for path in sorted(receipt_root.rglob("receipt.json"))[:8]]

    expected_url: str | None = None
    nested_brief = brief.get("brief") if isinstance(brief.get("brief"), Mapping) else {}
    for key in ("chatgpt_project_url", "project_url"):
        candidate = nested_brief.get(key) if isinstance(nested_brief, Mapping) else None
        if not isinstance(candidate, str) or not candidate.strip():
            candidate = brief.get(key)
        if isinstance(candidate, str) and candidate.strip():
            expected_url = candidate.strip()
            break

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_reference in refs[:8]:
        reference = raw_reference
        if isinstance(raw_reference, Mapping):
            reference = raw_reference.get("path", raw_reference.get("receipt_path", raw_reference.get("consultation_id")))
        relative, path = _resolve_receipt_path(root, reference)
        if relative in seen:
            continue
        receipt = _read_json(path, "research receipt", max_bytes=MAX_ARTIFACT_BYTES)
        if receipt.get("status") != "complete" or receipt.get("request_count") != 1:
            raise _fail("RESEARCH_RECEIPT_INVALID", "research receipt must prove one complete request")
        if receipt.get("conversation_validated") is not True or receipt.get("project_scope_verified") is not True:
            raise _fail("RESEARCH_RECEIPT_INVALID", "research receipt must prove conversation and Project scope")
        consultation_id = receipt.get("consultation_id")
        conversation_id = receipt.get("conversation_id")
        if not isinstance(consultation_id, str) or not consultation_id.strip() or not isinstance(conversation_id, str) or not conversation_id.strip():
            raise _fail("RESEARCH_RECEIPT_INVALID", "research receipt identity is incomplete")
        if isinstance(raw_reference, str) and not raw_reference.endswith("receipt.json") and consultation_id != raw_reference:
            raise _fail("RESEARCH_RECEIPT_BINDING_MISMATCH", "research receipt consultation identity differs")
        if expected_url is not None and receipt.get("project_url") != expected_url:
            raise _fail("RESEARCH_RECEIPT_BINDING_MISMATCH", "research receipt Project URL differs")
        digest, size = _file_digest(path, "research receipt")
        result.append(
            {
                "path": relative,
                "digest": digest,
                "bytes": size,
                "consultation_id": consultation_id,
                "conversation_id": conversation_id,
                "mode": str(receipt.get("mode", "")).upper(),
                "status": "COMPLETE",
                "request_count": 1,
            }
        )
        seen.add(relative)
    if not result:
        raise _fail("RESEARCH_PROVENANCE_MISSING", "accepted design requires at least one verified research receipt")
    return result


def _accepted_input_descriptors(
    root: Path,
    *,
    research_provenance: Sequence[Mapping[str, Any]],
) -> tuple[list[str], dict[str, str], dict[str, int]]:
    """Return the immutable files bound by an accepted-design handoff."""

    required = [Path(".research") / "PROJECT_BRIEF.json", DESIGN_PACKAGE_RELATIVE_PATH]
    # ``workflow-state.json`` is deliberately excluded.  The runtime updates
    # that mutable checkpoint after Stage planning, so binding it as a Stage
    # input would make every successful handoff immediately stale.
    required.extend(Path(str(item["path"])) for item in research_provenance)
    experiments = root / ".research" / "experiments"
    if experiments.is_dir() and not experiments.is_symlink():
        required.extend(path.relative_to(root) for path in sorted(experiments.glob("*.json")) if path.is_file() and not path.is_symlink())

    paths: list[str] = []
    digests: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for relative in required:
        normalized = _relative_path(relative.as_posix(), "accepted-design input path")
        if normalized in digests:
            continue
        path = root.joinpath(*normalized.split("/"))
        digest, size = _file_digest(path, f"accepted-design input {normalized}")
        paths.append(normalized)
        digests[normalized] = digest
        sizes[normalized] = size
    if len(paths) < 3:
        raise _fail("EXECUTION_HANDOFF_INPUT_INVALID", "accepted-design handoff needs brief, package, and receipt inputs")
    return paths, digests, sizes


def _bootstrap_state_digest(state: Mapping[str, Any]) -> str:
    """Return the digest of the state envelope without its self-binding field."""

    detached = copy.deepcopy(dict(state))
    detached.pop("state_digest", None)
    return sha256_json(detached)


def bootstrap_state_digest(state: Mapping[str, Any]) -> str:
    """Public helper for producers that persist ``bootstrap_state.v1``."""

    if not isinstance(state, Mapping):
        raise _fail("BOOTSTRAP_STATE_INVALID", "bootstrap state must be an object")
    return _bootstrap_state_digest(state)


def _brief_digest(brief: Mapping[str, Any]) -> str:
    return sha256_json(brief)


def _canonical_input_descriptors(root: Path, *, bootstrap_state: Mapping[str, Any]) -> tuple[list[str], dict[str, str], dict[str, int]]:
    """Return stable relative paths and byte digests for Bootstrap inputs."""

    candidates = [
        (Path(".research") / "PROJECT_BRIEF.json", "project brief"),
        (Path(".research") / "PROJECT_CONTEXT.md", "project context"),
        (DISCOVERY_REPORT_RELATIVE_PATH, "discovery report"),
        (PROJECT_BLUEPRINT_RELATIVE_PATH, "project blueprint"),
        (BLUEPRINT_MANIFEST_RELATIVE_PATH, "blueprint manifest"),
        (Path(".research") / "AVAILABLE_ASSETS.json", "available assets"),
        (Path(".research") / "baseline-result.json", "baseline result"),
        (BOOTSTRAP_STATE_RELATIVE_PATH, "bootstrap state"),
    ]
    paths: list[str] = []
    digests: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for relative, label in candidates:
        path = root / relative
        # AVAILABLE_ASSETS and baseline-result are useful evidence but are not
        # required by every valid Bootstrap fixture.  All other entries are
        # mandatory and are checked by their dedicated loaders first.
        if not path.is_file() and label in {"available assets", "baseline result"}:
            continue
        digest, size = _file_digest(path, label)
        key = relative.as_posix()
        paths.append(key)
        digests[key] = digest
        sizes[key] = size
    # The state was loaded before this helper.  Referencing it here avoids an
    # accidental implementation that writes an unbound state file while
    # planning.
    if BOOTSTRAP_STATE_RELATIVE_PATH.as_posix() not in digests:
        raise _fail("BOOTSTRAP_STATE_NOT_FOUND", "bootstrap_state.json is required for Step 15")
    _assert_safe(bootstrap_state, path="bootstrap_state")
    return paths, digests, sizes


def _load_bootstrap_state(root: Path, path: str | os.PathLike[str] | None = None) -> tuple[dict[str, Any], Path]:
    supplied = Path(path).expanduser() if path is not None else root / BOOTSTRAP_STATE_RELATIVE_PATH
    if not supplied.is_absolute():
        supplied = root / supplied
    try:
        resolved = supplied.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError) as exc:
        raise _fail("BOOTSTRAP_STATE_NOT_FOUND", "bootstrap state could not be resolved", exc) from exc
    except ValueError as exc:
        raise _fail("BOOTSTRAP_STATE_OUTSIDE_REPOSITORY", "bootstrap state must remain inside the repository", exc) from exc
    if resolved != root / BOOTSTRAP_STATE_RELATIVE_PATH:
        # A caller may inspect a copied state, but canonical Step 15 output is
        # always bound to the project-local snake_case path.
        raise _fail("BOOTSTRAP_STATE_PATH_INVALID", "bootstrap state must use .research/bootstrap_state.json")
    return _read_json(resolved, "bootstrap state", max_bytes=MAX_BOOTSTRAP_STATE_BYTES), resolved


def _state_value(state: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in state:
            return state[name]
    for wrapper_name in ("bootstrap", "bootstrap_state", "outputs", "artifacts", "evidence"):
        wrapper = state.get(wrapper_name)
        if isinstance(wrapper, Mapping):
            for name in names:
                if name in wrapper:
                    return wrapper[name]
    return None


def _bootstrap_marker(state: Mapping[str, Any]) -> str | None:
    marker = state.get("marker")
    if isinstance(marker, str) and marker.strip():
        return marker.strip()
    markers = state.get("markers")
    if isinstance(markers, list) and BOOTSTRAP_STATE_MARKER in markers:
        return BOOTSTRAP_STATE_MARKER
    return None


def _validate_bootstrap_state(
    state: Mapping[str, Any],
    *,
    project_id: str,
    brief_digest: str,
    context_digest: str,
    discovery_digest: str,
    blueprint_digest: str,
    input_digests: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Require the complete, digest-bound Bootstrap-to-Stage hand-off.

    Step 15 deliberately does not interpret partial or historical envelopes.
    The state file is the producer-owned assertion that all Bootstrap outputs
    are present and that no downstream Stage side effect has happened.  Every
    assertion is required here, and ``state_digest`` binds the complete
    envelope (minus its own self-reference) against accidental or malicious
    edits.
    """

    if not isinstance(state, Mapping) or not state:
        raise _fail("BOOTSTRAP_STATE_INVALID", "bootstrap state must be a non-empty object")
    detached = copy.deepcopy(dict(state))
    _assert_safe(detached, path="bootstrap_state")
    try:
        validate_instance(detached, load_schema("bootstrap_state.v1"))
    except ContractValidationError as exc:
        raise _fail("BOOTSTRAP_STATE_INVALID", "bootstrap state failed bootstrap_state.v1 validation", exc) from exc

    # Keep a stable readiness error for callers that want to distinguish a
    # well-formed but not-yet-ready hand-off from malformed evidence.
    if detached.get("status") not in {"BOOTSTRAP_COMPLETE", "READY_FOR_STAGE_PLANNING"}:
        raise _fail("BOOTSTRAP_NOT_READY", "bootstrap state is not ready for Stage planning")
    marker = detached.get("marker")
    markers = detached.get("markers")
    if marker is not None and marker != BOOTSTRAP_STATE_MARKER:
        raise _fail("BOOTSTRAP_STATE_INVALID", "bootstrap state marker is not the required PASS marker")
    if not isinstance(markers, list) or BOOTSTRAP_STATE_MARKER not in markers:
        raise _fail("BOOTSTRAP_STATE_INVALID", "bootstrap state markers do not contain the required PASS marker")
    if detached.get("next_action") != BOOTSTRAP_STATE_NEXT_ACTION:
        raise _fail("BOOTSTRAP_STATE_INVALID", "bootstrap state next_action is not READY_FOR_STAGE_PLANNING")
    if detached.get("project_scope_verified") is not True:
        raise _fail("BOOTSTRAP_STATE_INVALID", "bootstrap state did not verify project scope")

    bindings = {
        "project_id": project_id,
        "brief_digest": brief_digest,
        "context_digest": context_digest,
        "discovery_digest": discovery_digest,
        "blueprint_digest": blueprint_digest,
    }
    for field, expected in bindings.items():
        if field in detached and detached.get(field) != expected:
            raise _fail("BOOTSTRAP_STATE_BINDING_MISMATCH", f"bootstrap state {field} is stale")

    nested_outputs = (
        ("discovery", DISCOVERY_STATUS, DISCOVERY_STATE_PATH, discovery_digest, DISCOVERY_REPORT_MARKER, "report_path", "report_digest"),
        ("blueprint", "PROJECT_BLUEPRINT_READY", BLUEPRINT_STATE_PATH, blueprint_digest, PROJECT_BLUEPRINT_MARKER, "blueprint_path", "blueprint_digest"),
    )
    for field, expected_status, expected_path, expected_digest, expected_marker, legacy_path, legacy_digest in nested_outputs:
        nested = detached.get(field)
        if not isinstance(nested, Mapping):
            raise _fail("BOOTSTRAP_STATE_INVALID", f"bootstrap state {field} output is required")
        if nested.get("marker") != expected_marker:
            raise _fail("BOOTSTRAP_STATE_INVALID", f"bootstrap state {field}.marker is not the required PASS marker")
        if nested.get("status") != expected_status:
            raise _fail("BOOTSTRAP_STATE_BINDING_MISMATCH", f"bootstrap state {field}.status is stale")
        nested_path = nested.get("path", nested.get(legacy_path))
        nested_digest = nested.get("digest", nested.get(legacy_digest))
        if nested_path != expected_path:
            raise _fail("BOOTSTRAP_STATE_PATH_INVALID", f"bootstrap state {field}.path is not canonical")
        if nested.get("path") is not None and nested.get(legacy_path) is not None and nested.get("path") != nested.get(legacy_path):
            raise _fail("BOOTSTRAP_STATE_PATH_INVALID", f"bootstrap state {field}.path aliases disagree")
        if not isinstance(nested_digest, str) or _HEX64_RE.fullmatch(nested_digest) is None:
            raise _fail("BOOTSTRAP_STATE_DIGEST_INVALID", f"bootstrap state {field}.digest is invalid")
        if input_digests is not None and nested_digest != input_digests.get(expected_path):
            raise _fail("BOOTSTRAP_STATE_BINDING_MISMATCH", f"bootstrap state {field}.digest is stale")
        # Some producer envelopes also carry logical evidence digests at the
        # top level.  When present, those are checked against the dedicated
        # artifact loaders below; they are distinct from the byte digest used
        # by the nested path binding.
        if nested.get("digest") is not None and nested.get("digest") != nested_digest:
            raise _fail("BOOTSTRAP_STATE_BINDING_MISMATCH", f"bootstrap state {field}.digest aliases disagree")
        if nested.get(legacy_digest) is not None and nested.get(legacy_digest) != nested_digest:
            raise _fail("BOOTSTRAP_STATE_BINDING_MISMATCH", f"bootstrap state {field}.digest is stale")

    if detached.get("state_digest") != _bootstrap_state_digest(detached):
        raise _fail("BOOTSTRAP_STATE_DIGEST_MISMATCH", "bootstrap state state_digest does not match its envelope")

    # These are explicit false assertions in the hand-off schema.  Retain the
    # active-stage check for forward-compatible envelopes that add the field.
    for field in ("stage_created", "stage_started", "production_architecture_selected", "business_source_modified"):
        if field in detached and detached.get(field) is not False:
            raise _fail("BOOTSTRAP_STATE_BOUNDARY_VIOLATION", f"bootstrap state records {field}=true")
    active = detached.get("active_stage_id")
    if active not in (None, "", False):
        raise _fail("BOOTSTRAP_STATE_BOUNDARY_VIOLATION", "bootstrap state contains an active Stage")
    return detached


def _route_title(route: Mapping[str, Any]) -> str:
    for key in ("title", "name", "summary", "route_id", "id"):
        value = route.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "bounded Blueprint route"


def _route_id(route: Mapping[str, Any], blueprint_digest: str) -> str:
    value = route.get("route_id", route.get("id"))
    if isinstance(value, str) and value.strip():
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
        if slug:
            return slug[:48]
    return "blueprint-" + blueprint_digest[:12]


def _brief_text(brief: Mapping[str, Any], *keys: str, default: str) -> str:
    nested = brief.get("brief") if isinstance(brief.get("brief"), Mapping) else brief
    for key in keys:
        value = nested.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    rough = brief.get("rough_requirement")
    if isinstance(rough, str) and rough.strip():
        return rough.strip()
    return default


def _brief_list(brief: Mapping[str, Any], *keys: str) -> list[str]:
    nested = brief.get("brief") if isinstance(brief.get("brief"), Mapping) else brief
    values: list[str] = []
    for key in keys:
        candidate = nested.get(key)
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, str) and item.strip() and item.strip() not in values:
                    values.append(item.strip())
    return values[:MAX_LIST_ITEMS]


def _route_steps(route: Mapping[str, Any]) -> list[str]:
    values = route.get("steps", route.get("validation_steps", []))
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    return [item.strip() for item in values if isinstance(item, str) and item.strip()][:MAX_LIST_ITEMS]


def _derive_contract(
    root: Path,
    *,
    brief: Mapping[str, Any],
    context: Mapping[str, Any],
    discovery: Mapping[str, Any],
    blueprint: Mapping[str, Any],
    bootstrap_state: Mapping[str, Any],
    input_paths: Sequence[str],
    input_digests: Mapping[str, str],
    input_sizes: Mapping[str, int],
    stage_id: str | None,
    stage_name: str | None,
    allowed_paths: Sequence[str] | None,
    required_checks: Sequence[str] | None,
    review_artifact_requirements: Sequence[str] | None,
    max_iterations: int,
    retry_budget: int | None,
) -> dict[str, Any]:
    project_id = _text(brief.get("project_id"), "project_id", max_length=200)
    blueprint_digest = _validate_digest(blueprint.get("blueprint_digest"), "blueprint_digest")
    route = blueprint.get("primary_route")
    if not isinstance(route, Mapping) or not route:
        raise _fail("BLUEPRINT_ROUTE_INVALID", "Blueprint must contain one non-empty primary_route object")
    route_id = _route_id(route, blueprint_digest)
    derived_stage_id = f"stage-{route_id}-{blueprint_digest[:12]}"
    selected_stage_id = _text(stage_id or derived_stage_id, "stage_id", max_length=80)
    if _STAGE_ID_RE.fullmatch(selected_stage_id) is None:
        raise _fail("STAGE_ID_INVALID", "stage_id must contain only letters, numbers, dot, underscore, or hyphen")
    title = _route_title(route)
    selected_name = _text(stage_name or f"Bootstrap route: {title}", "stage_name", max_length=512)

    project_goal = _brief_text(
        brief,
        "goal",
        "project_goal",
        "problem_statement",
        default="Execute the approved project route as one bounded Stage.",
    )
    desired = _brief_text(
        brief,
        "desired_outcome",
        "user_visible_goal",
        "goal",
        default="Produce a user-visible, reviewable result for the approved project goal.",
    )
    route_summary = _brief_text(
        route,
        "summary",
        "description",
        "title",
        "name",
        default=title,
    )
    stage_goal = f"Validate the bounded Blueprint route '{title}' against the frozen Bootstrap evidence."
    criteria = _brief_list(brief, "success_criteria", "acceptance_criteria")
    validation_plan = blueprint.get("validation_plan")
    if isinstance(validation_plan, list):
        criteria.extend(
            item.strip()
            for item in validation_plan
            if isinstance(item, str) and item.strip() and item.strip() not in criteria
        )
    if not criteria:
        criteria = ["All declared required checks pass.", "The review artifacts are present and inspectable."]
    criteria = criteria[:MAX_LIST_ITEMS]
    steps = _route_steps(route)
    acceptance_description = (
        f"{route_summary} Validate the route without changing the frozen Bootstrap inputs. "
        + " Acceptance criteria: "
        + "; ".join(criteria)
        + (" Planned route steps: " + "; ".join(steps) if steps else "")
    )

    # Preserve the descriptor order, including optional assets/baseline
    # records.  The input list is the canonical order used by both the plan
    # envelope and its contract baseline; reconstructing it independently
    # would make equivalent evidence appear to be a different plan.
    canonical_paths = list(input_paths)
    stage_scope = f".research/stages/{selected_stage_id}"
    chosen_allowed = list(allowed_paths) if allowed_paths is not None else [stage_scope]
    if not chosen_allowed:
        raise _fail("STAGE_SCOPE_INVALID", "allowed_paths must contain at least one path")
    checked_allowed = [_relative_path(item, "allowed_paths") for item in chosen_allowed]
    if len(set(checked_allowed)) != len(checked_allowed):
        raise _fail("STAGE_SCOPE_INVALID", "allowed_paths must not contain duplicates")
    protected = [
        ".git",
        ".auth",
        ".consultations",
        ".research/PROJECT_BRIEF.json",
        ".research/PROJECT_CONTEXT.md",
        ".research/discovery",
        ".research/blueprint",
        ".research/AVAILABLE_ASSETS.json",
        ".research/baseline-result.json",
        BOOTSTRAP_STATE_RELATIVE_PATH.as_posix(),
    ]
    # Contract paths are intentionally explicit and relative.  The controller
    # performs the second validation pass before registration.
    protected = list(dict.fromkeys(protected))

    checks = list(required_checks) if required_checks is not None else [
        "bootstrap_outputs_schema_valid",
        "baseline_unchanged",
        "local_fixture_checks_pass",
    ]
    checks = [_text(item, "required_checks item", max_length=256) for item in checks]
    if not checks or len(set(checks)) != len(checks):
        raise _fail("STAGE_CHECKS_INVALID", "required_checks must be a non-empty unique list")
    artifacts = list(review_artifact_requirements) if review_artifact_requirements is not None else [
        "metrics_summary",
        "before_after",
    ]
    artifacts = [_text(item, "review_artifact_requirements item", max_length=256) for item in artifacts]
    if not artifacts or len(set(artifacts)) != len(artifacts):
        raise _fail("STAGE_ARTIFACTS_INVALID", "review_artifact_requirements must be a non-empty unique list")
    if not isinstance(max_iterations, int) or isinstance(max_iterations, bool) or not 1 <= max_iterations <= 100:
        raise _fail("STAGE_ITERATIONS_INVALID", "max_iterations must be between 1 and 100")
    if retry_budget is not None and (
        isinstance(retry_budget, bool) or not isinstance(retry_budget, int) or not 1 <= retry_budget <= 100
    ):
        raise _fail("STAGE_RETRY_BUDGET_INVALID", "retry_budget must be between 1 and 100")

    brief_digest = _brief_digest(brief)
    context_digest = _validate_digest(context.get("context_digest"), "context_digest")
    discovery_digest = _validate_digest(discovery.get("evidence_digest"), "discovery_digest")
    feasibility_digest = blueprint.get("feasibility_digest")
    if feasibility_digest is not None:
        feasibility_digest = _validate_digest(feasibility_digest, "feasibility_digest")
    baseline_result_path = ".research/baseline-result.json" if ".research/baseline-result.json" in input_digests else None
    baseline: dict[str, Any] = {
        "schema_version": "stage_baseline.v1",
        "project_id": project_id,
        "brief_digest": brief_digest,
        "context_digest": context_digest,
        "discovery_digest": discovery_digest,
        "blueprint_digest": blueprint_digest,
        "feasibility_digest": feasibility_digest,
        "bootstrap_state_digest": input_digests[BOOTSTRAP_STATE_RELATIVE_PATH.as_posix()],
        "source_paths": list(canonical_paths),
        "source_digests": {key: input_digests[key] for key in canonical_paths if key in input_digests},
        "source_sizes": {key: int(input_sizes[key]) for key in canonical_paths if key in input_sizes},
        "baseline_result_path": baseline_result_path,
        "baseline_result_digest": input_digests.get(baseline_result_path) if baseline_result_path else None,
        "route_id": route_id,
    }
    _assert_safe(baseline, path="baseline")

    plan_id = "plan-stage-" + sha256_json({
        "project_id": project_id,
        "stage_id": selected_stage_id,
        "baseline": baseline,
    })[:20]
    metadata = {
        "step": "15",
        "planning_mode": "bootstrap_to_stage_contract",
        "route_id": route_id,
        "source_artifacts": list(canonical_paths),
        "bootstrap_marker": _bootstrap_marker(bootstrap_state),
        "discovery_marker": discovery.get("marker"),
        "blueprint_marker": blueprint.get("marker"),
        "validation_plan": [str(item) for item in validation_plan if isinstance(item, str)][:MAX_LIST_ITEMS]
        if isinstance(validation_plan, list)
        else [],
        "no_automatic_start": True,
    }
    contract: dict[str, Any] = {
        "schema_version": "stage_contract.v1",
        "plan_id": plan_id,
        "project_id": project_id,
        "repository_root": root.as_posix(),
        "stage_id": selected_stage_id,
        "stage_name": selected_name,
        "project_goal": project_goal,
        "stage_goal": stage_goal,
        "user_visible_goal": desired,
        "inputs": canonical_paths,
        "protected_paths": protected,
        "allowed_paths": checked_allowed,
        "acceptance_description": acceptance_description,
        "required_checks": checks,
        "review_artifact_requirements": artifacts,
        "baseline": baseline,
        "status": StageState.PLANNED.value,
        "lane_mode": "PRIMARY",
        "comparison_mode": "PRIMARY",
        "critic_mode": "NONE",
        "max_iterations": max_iterations,
        "metadata": metadata,
    }
    if retry_budget is not None:
        contract["retry_budget"] = retry_budget
    try:
        checked = validate_stage_contract_v1(contract)
    except ContractValidationError as exc:
        raise _fail("STAGE_CONTRACT_INVALID", "derived Stage contract failed stage_contract.v1 validation", exc) from exc
    return checked


def _plan_schema_payload(
    root: Path,
    *,
    contract: Mapping[str, Any],
    input_paths: Sequence[str],
    input_digests: Mapping[str, str],
    bootstrap_state: Mapping[str, Any],
    stage_state_path: Path,
    contract_path: Path,
    reused: bool,
    stage_view: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    relative_contract = contract_path.relative_to(root).as_posix()
    relative_state = stage_state_path.relative_to(root).as_posix()
    plan_path = (root / STAGE_PLAN_RELATIVE_PATH).relative_to(root).as_posix()
    payload: dict[str, Any] = {
        "schema_version": STAGE_PLANNING_SCHEMA_VERSION,
        "project_id": contract["project_id"],
        "repository_root": root.as_posix(),
        "plan_id": contract.get("plan_id"),
        "stage_id": contract["stage_id"],
        "status": STAGE_PLANNING_STATUS,
        "planning_status": "STAGE_PLANNING_COMPLETE",
        "stage_status": StageState.PLANNED.value,
        "next_action": STAGE_PLANNING_NEXT_ACTION,
        "stage_created": True,
        "stage_started": False,
        "stage_contract_path": relative_contract,
        "contract_path": relative_contract,
        "stage_state_path": relative_state,
        "state_path": relative_state,
        "stage_plan_path": plan_path,
        "input_paths": list(input_paths),
        "input_digests": dict(input_digests),
        "baseline_digest": sha256_json(contract["baseline"]),
        "contract_digest": sha256_json(contract),
        "bootstrap_state_digest": input_digests.get(BOOTSTRAP_STATE_RELATIVE_PATH.as_posix()),
        "bootstrap_marker": _bootstrap_marker(bootstrap_state),
        "marker": STAGE_PLANNING_MARKER,
        "idempotent_reuse": reused,
        "method_evidence_policy": method_evidence_policy_reference(),
        "stage": copy.deepcopy(dict(stage_view)) if isinstance(stage_view, Mapping) else None,
    }
    try:
        validate_instance(payload, load_schema("stage_planning"))
    except ContractValidationError as exc:
        raise _fail("STAGE_PLAN_SCHEMA_INVALID", "Stage planning envelope failed its schema", exc) from exc
    _assert_safe(payload, path="stage_plan")
    return payload


def _load_existing_plan(root: Path, *, relative_path: str | None = None) -> dict[str, Any] | None:
    path = root / (relative_path or STAGE_PLAN_RELATIVE_PATH.as_posix())
    if not path.exists():
        return None
    value = _read_json(path, "stage plan")
    try:
        validate_instance(value, load_schema("stage_planning"))
    except ContractValidationError as exc:
        raise _fail("STAGE_PLAN_INVALID", "existing Stage planning envelope failed schema validation", exc) from exc
    return value


def _checked_repo_path(
    root: Path,
    value: Any,
    field: str,
    *,
    require_file: bool = False,
) -> tuple[str, Path]:
    """Normalize a plan path and prove that it remains inside ``root``."""

    relative = _relative_path(value, field)
    candidate = root.joinpath(*relative.split("/"))
    if candidate.is_symlink():
        raise _fail("STAGE_PLAN_PATH_INVALID", f"{field} must not be a symlink")
    try:
        resolved = candidate.resolve(strict=require_file)
        resolved.relative_to(root)
    except ValueError as exc:
        raise _fail("STAGE_PLAN_PATH_INVALID", f"{field} must remain inside the repository", exc) from exc
    except (OSError, RuntimeError) as exc:
        code = "STAGE_PLAN_PATH_INVALID" if require_file else "STAGE_PLAN_PATH_INVALID"
        raise _fail(code, f"{field} could not be resolved", exc) from exc
    if require_file and not candidate.is_file():
        raise _fail("STAGE_PLAN_PATH_INVALID", f"{field} must name a regular file")
    return relative, candidate


def _recompute_input_digests(
    root: Path,
    input_paths: Sequence[Any],
) -> tuple[list[str], dict[str, str], dict[str, int]]:
    """Re-read every plan input and return its current byte evidence."""

    if not isinstance(input_paths, Sequence) or isinstance(input_paths, (str, bytes, bytearray)):
        raise _fail("STAGE_PLAN_INPUT_INVALID", "input_paths must be an array")
    checked_paths: list[str] = []
    digests: dict[str, str] = {}
    sizes: dict[str, int] = {}
    for raw_path in input_paths:
        relative, candidate = _checked_repo_path(root, raw_path, "input path", require_file=True)
        if relative in digests:
            raise _fail("STAGE_PLAN_INPUT_INVALID", "input_paths must not contain duplicates")
        digest, size = _file_digest(candidate, f"input {relative}")
        checked_paths.append(relative)
        digests[relative] = digest
        sizes[relative] = size
    if not checked_paths:
        raise _fail("STAGE_PLAN_INPUT_INVALID", "input_paths must not be empty")
    return checked_paths, digests, sizes


def _validate_contract_against_plan(
    root: Path,
    *,
    plan: Mapping[str, Any],
    contract_path: Path,
    contract_payload: Mapping[str, Any],
    input_paths: Sequence[str] | None = None,
    input_digests: Mapping[str, str] | None = None,
    input_sizes: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Validate the persisted contract and every digest/path binding."""

    if not isinstance(contract_payload, Mapping):
        raise _fail("STAGE_CONTRACT_INVALID", "Stage contract must be an object")
    stage_id = _text(plan.get("stage_id"), "stage_id", max_length=80)
    expected_contract_relative = f".research/stages/{stage_id}/{STAGE_CONTRACT_FILENAME}"
    actual_contract_relative, actual_contract_path = _checked_repo_path(
        root,
        plan.get("stage_contract_path"),
        "stage_contract_path",
        require_file=True,
    )
    if actual_contract_relative != expected_contract_relative or actual_contract_path != contract_path:
        raise _fail("STAGE_CONTRACT_PATH_INVALID", "Stage contract path is not the canonical Stage path")
    if plan.get("contract_path") != actual_contract_relative:
        raise _fail("STAGE_PLAN_PATH_INVALID", "contract_path and stage_contract_path disagree")

    try:
        checked = validate_stage_contract_v1(contract_payload)
    except ContractValidationError as exc:
        raise _fail("STAGE_CONTRACT_INVALID", "persisted Stage contract failed stage_contract.v1 validation", exc) from exc
    # The planner writes the normalized controller contract.  Refuse an
    # envelope that only becomes valid after validation because its persisted
    # digest would no longer describe the actual contract semantics.
    if canonical_json(checked) != canonical_json(dict(contract_payload)):
        raise _fail("STAGE_CONTRACT_INVALID", "persisted Stage contract is not canonical")

    if checked.get("project_id") != plan.get("project_id"):
        raise _fail("STAGE_PLAN_BINDING_MISMATCH", "Stage contract project_id disagrees with the Stage plan")
    if checked.get("stage_id") != stage_id:
        raise _fail("STAGE_PLAN_BINDING_MISMATCH", "Stage contract stage_id disagrees with the Stage plan")
    if checked.get("plan_id") != plan.get("plan_id"):
        raise _fail("STAGE_PLAN_BINDING_MISMATCH", "Stage contract plan_id disagrees with the Stage plan")
    repository_root = checked.get("repository_root")
    if not isinstance(repository_root, str) or not Path(repository_root).expanduser().is_absolute():
        raise _fail("STAGE_CONTRACT_ROOT_INVALID", "Stage contract repository_root must be absolute")
    try:
        if Path(repository_root).expanduser().resolve(strict=True) != root:
            raise _fail("STAGE_CONTRACT_ROOT_INVALID", "Stage contract repository_root disagrees with the repository")
    except (OSError, RuntimeError) as exc:
        raise _fail("STAGE_CONTRACT_ROOT_INVALID", "Stage contract repository_root could not be resolved", exc) from exc

    expected_paths = list(input_paths if input_paths is not None else plan.get("input_paths", []))
    expected_digests = dict(input_digests if input_digests is not None else plan.get("input_digests", {}))
    if checked.get("inputs") != expected_paths:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "Stage contract inputs disagree with the Stage plan")
    baseline = checked.get("baseline")
    if not isinstance(baseline, Mapping):
        raise _fail("STAGE_CONTRACT_INVALID", "Stage contract baseline is required")
    if baseline.get("source_paths") != expected_paths:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "Stage baseline source_paths disagree with the Stage plan")
    if baseline.get("source_digests") != expected_digests:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "Stage baseline source_digests disagree with the Stage plan")
    if baseline.get("bootstrap_state_digest") != expected_digests.get(BOOTSTRAP_STATE_RELATIVE_PATH.as_posix()):
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "Stage baseline bootstrap digest is stale")
    if input_sizes is not None and baseline.get("source_sizes") != {key: int(input_sizes[key]) for key in expected_paths}:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "Stage baseline source_sizes disagree with current inputs")
    if plan.get("baseline_digest") != sha256_json(baseline):
        raise _fail("STAGE_BASELINE_DIGEST_MISMATCH", "Stage baseline no longer matches the Stage plan")
    if plan.get("contract_digest") != sha256_json(dict(contract_payload)):
        raise _fail("STAGE_CONTRACT_DIGEST_MISMATCH", "Stage contract no longer matches the Stage plan")
    return checked


def _validate_baseline_sources(
    root: Path,
    *,
    contract: Mapping[str, Any],
    plan: Mapping[str, Any],
    input_digests: Mapping[str, str],
) -> dict[str, Any]:
    """Recompute logical Bootstrap digests bound into the Stage baseline."""

    baseline = contract.get("baseline")
    if not isinstance(baseline, Mapping):
        raise _fail("STAGE_CONTRACT_INVALID", "Stage contract baseline is required")
    try:
        brief_raw = load_project_brief(root)
        if not isinstance(brief_raw, Mapping):
            raise ValueError("project brief is missing")
        brief = validate_project_brief(brief_raw, root)
        context = load_project_context(root)
        discovery = load_discovery_report(root)
        blueprint = load_project_blueprint(root)
    except (ProjectStateError, ProjectIntakeError, ProjectContextError, ProjectDiscoveryError, ProjectBlueprintError, RuntimeError, ValueError) as exc:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "canonical Bootstrap artifacts failed baseline verification", exc) from exc
    if discovery.get("marker") != DISCOVERY_REPORT_MARKER:
        raise _fail("DISCOVERY_NOT_PASS", "canonical discovery report is missing the required PASS marker")
    expected = {
        "project_id": plan.get("project_id"),
        "brief_digest": sha256_json(dict(brief)),
        "context_digest": context.get("context_digest"),
        "discovery_digest": discovery.get("evidence_digest"),
        "blueprint_digest": blueprint.get("blueprint_digest"),
    }
    for field, observed in expected.items():
        if baseline.get(field) != observed:
            raise _fail("STAGE_PLAN_INPUT_MISMATCH", f"Stage baseline {field} is stale")
    feasibility_digest = blueprint.get("feasibility_digest")
    if baseline.get("feasibility_digest") != feasibility_digest:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "Stage baseline feasibility_digest is stale")
    baseline_result_path = baseline.get("baseline_result_path")
    if baseline_result_path is not None:
        if not isinstance(baseline_result_path, str) or input_digests.get(baseline_result_path) is None:
            raise _fail("STAGE_PLAN_INPUT_MISMATCH", "Stage baseline result path is stale")
        if baseline.get("baseline_result_digest") != input_digests[baseline_result_path]:
            raise _fail("STAGE_PLAN_INPUT_MISMATCH", "Stage baseline result digest is stale")
    expected_plan_id = "plan-stage-" + sha256_json({
        "project_id": contract.get("project_id"),
        "stage_id": contract.get("stage_id"),
        "baseline": dict(baseline),
    })[:20]
    if contract.get("plan_id") != expected_plan_id or plan.get("plan_id") != expected_plan_id:
        raise _fail("STAGE_PLAN_BINDING_MISMATCH", "plan_id is not derived from the current project baseline")
    return {
        "brief": brief,
        "context": context,
        "discovery": discovery,
        "blueprint": blueprint,
    }


def plan_stage(
    project_root: str | os.PathLike[str] = ".",
    *,
    stage_id: str | None = None,
    stage_name: str | None = None,
    allowed_paths: Sequence[str] | None = None,
    required_checks: Sequence[str] | None = None,
    review_artifact_requirements: Sequence[str] | None = None,
    max_iterations: int = 1,
    retry_budget: int | None = None,
    bootstrap_state_path: str | os.PathLike[str] | None = None,
    require_bootstrap_state: bool = True,
) -> dict[str, Any]:
    """Build and register one ``PLANNED`` Stage from canonical Bootstrap evidence.

    Re-running with unchanged evidence returns the existing plan without
    another registration event.  A changed input set never overwrites a
    prior plan implicitly; callers receive ``STAGE_PLAN_INPUT_MISMATCH`` and
    must make an explicit user decision before changing the planned Stage.
    """

    try:
        root = resolve_project_root(project_root)
    except ProjectStateError as exc:
        raise _fail("PROJECT_ROOT_INVALID", "project root could not be resolved", exc) from exc

    existing = _load_existing_plan(root)
    if require_bootstrap_state:
        bootstrap_state, bootstrap_path = _load_bootstrap_state(root, bootstrap_state_path)
    else:
        bootstrap_path = root / BOOTSTRAP_STATE_RELATIVE_PATH
        bootstrap_state = _read_json(bootstrap_path, "bootstrap state", max_bytes=MAX_BOOTSTRAP_STATE_BYTES)

    try:
        brief_raw = load_project_brief(root)
    except ProjectStateError as exc:
        raise _fail("BRIEF_INVALID", "canonical project brief could not be loaded", exc) from exc
    if not isinstance(brief_raw, Mapping):
        raise _fail("BRIEF_NOT_FOUND", "an approved project brief is required")
    try:
        brief = validate_project_brief(brief_raw, root)
    except ProjectIntakeError as exc:
        raise _fail("BRIEF_INVALID", "canonical project brief failed validation", exc) from exc
    if brief.get("state") != "APPROVED" or brief.get("status") != "APPROVED":
        raise _fail("BRIEF_NOT_APPROVED", "Step 15 requires state=APPROVED")

    try:
        context = load_project_context(root)
    except (ProjectContextError, RuntimeError) as exc:
        raise _fail("CONTEXT_INVALID", "canonical project context failed validation", exc) from exc
    try:
        discovery = load_discovery_report(root)
    except (ProjectDiscoveryError, RuntimeError) as exc:
        raise _fail("DISCOVERY_INVALID", "canonical discovery report failed validation", exc) from exc
    if discovery.get("marker") != DISCOVERY_REPORT_MARKER:
        raise _fail("DISCOVERY_NOT_PASS", "canonical discovery report is missing the required PASS marker")
    try:
        blueprint = load_project_blueprint(root)
    except (ProjectBlueprintError, RuntimeError) as exc:
        raise _fail("BLUEPRINT_INVALID", "canonical project Blueprint failed validation", exc) from exc

    project_id = str(brief.get("project_id", ""))
    brief_digest = _brief_digest(brief)
    context_digest = _validate_digest(context.get("context_digest"), "context_digest")
    discovery_digest = _validate_digest(discovery.get("evidence_digest"), "discovery_digest")
    blueprint_digest = _validate_digest(blueprint.get("blueprint_digest"), "blueprint_digest")
    if blueprint.get("project_id") != project_id or discovery.get("project_id") != project_id:
        raise _fail("BOOTSTRAP_PROJECT_MISMATCH", "Bootstrap artifacts are bound to another project")
    if blueprint.get("brief_digest") != brief_digest or discovery.get("brief_digest") != brief_digest:
        raise _fail("BOOTSTRAP_BRIEF_MISMATCH", "Bootstrap artifacts are stale for the approved brief")
    if blueprint.get("context_digest") != context_digest:
        raise _fail("BOOTSTRAP_CONTEXT_MISMATCH", "Blueprint is stale for the canonical context")
    if blueprint.get("discovery_digest") != discovery_digest:
        raise _fail("BOOTSTRAP_DISCOVERY_MISMATCH", "Blueprint is stale for the discovery report")
    if blueprint.get("status") != "PROJECT_BLUEPRINT_READY" or blueprint.get("next_action") != "READY_FOR_STAGE_PLANNING":
        raise _fail("BLUEPRINT_NOT_READY", "Blueprint is not at READY_FOR_STAGE_PLANNING")
    if blueprint.get("stage_created") is True or blueprint.get("stage_started") is True:
        raise _fail("BLUEPRINT_BOUNDARY_VIOLATION", "Blueprint records a Stage side effect")
    if discovery.get("stage_started") is True or discovery.get("production_architecture_selected") is True:
        raise _fail("DISCOVERY_BOUNDARY_VIOLATION", "Discovery records a forbidden downstream side effect")

    # Ensure the canonical path is included in the digest set even when a
    # custom loader was used in a test.
    input_paths, input_digests, input_sizes = _canonical_input_descriptors(root, bootstrap_state=bootstrap_state)
    bootstrap_state = _validate_bootstrap_state(
        bootstrap_state,
        project_id=project_id,
        brief_digest=brief_digest,
        context_digest=context_digest,
        discovery_digest=discovery_digest,
        blueprint_digest=blueprint_digest,
        input_digests=input_digests,
    )
    if _digest_from_file(bootstrap_path, "bootstrap state") != input_digests[BOOTSTRAP_STATE_RELATIVE_PATH.as_posix()]:
        raise _fail("BOOTSTRAP_STATE_CHANGED", "bootstrap state changed while it was being inspected")

    contract = _derive_contract(
        root,
        brief=brief,
        context=context,
        discovery=discovery,
        blueprint=blueprint,
        bootstrap_state=bootstrap_state,
        input_paths=input_paths,
        input_digests=input_digests,
        input_sizes=input_sizes,
        stage_id=stage_id,
        stage_name=stage_name,
        allowed_paths=allowed_paths,
        required_checks=required_checks,
        review_artifact_requirements=review_artifact_requirements,
        max_iterations=max_iterations,
        retry_budget=retry_budget,
    )
    contract_path = root / ".research" / "stages" / contract["stage_id"] / STAGE_CONTRACT_FILENAME
    state_path = root / STAGE_STATE_RELATIVE_PATH

    if existing is not None:
        if existing.get("project_id") != contract["project_id"] or existing.get("stage_id") != contract["stage_id"]:
            raise _fail("STAGE_PLAN_INPUT_MISMATCH", "an existing Stage plan is bound to another route")
        if existing.get("plan_id") != contract.get("plan_id"):
            raise _fail("STAGE_PLAN_INPUT_MISMATCH", "existing Stage plan has a different plan_id")
        if existing.get("stage_contract_path") != contract_path.relative_to(root).as_posix():
            raise _fail("STAGE_CONTRACT_PATH_INVALID", "existing Stage plan points at a non-canonical contract path")
        if existing.get("stage_state_path") != STAGE_STATE_RELATIVE_PATH.as_posix():
            raise _fail("STAGE_PLAN_PATH_INVALID", "existing Stage plan points at a non-canonical state path")
        if existing.get("input_digests") != dict(input_digests):
            raise _fail("STAGE_PLAN_INPUT_MISMATCH", "existing Stage plan has different input evidence")
        existing_contract_relative, existing_contract_path = _checked_repo_path(
            root,
            existing.get("stage_contract_path"),
            "stage_contract_path",
            require_file=True,
        )
        existing_contract = _read_json(existing_contract_path, "stage contract", max_bytes=MAX_ARTIFACT_BYTES)
        checked_existing_contract = _validate_contract_against_plan(
            root,
            plan=existing,
            contract_path=existing_contract_path,
            contract_payload=existing_contract,
            input_paths=input_paths,
            input_digests=input_digests,
            input_sizes=input_sizes,
        )
        if canonical_json(checked_existing_contract) != canonical_json(contract):
            raise _fail("STAGE_PLAN_INPUT_MISMATCH", "existing Stage contract does not match current Bootstrap evidence")
        try:
            controller = StageController.from_state(state_path)
            stage = controller.show_stage(contract["stage_id"])
        except (StageControllerError, ContractValidationError, OSError) as exc:
            raise _fail("STAGE_STATE_INVALID", "existing Stage state failed validation", exc) from exc
        if stage.get("status") != StageState.PLANNED.value:
            raise _fail("STAGE_PLAN_BOUNDARY_VIOLATION", "existing planned Stage is no longer PLANNED")
        try:
            state_contract = validate_stage_contract_v1(stage.get("contract"))
        except ContractValidationError as exc:
            raise _fail("STAGE_STATE_INVALID", "existing Stage state contract failed validation", exc) from exc
        if canonical_json(state_contract) != canonical_json(checked_existing_contract):
            raise _fail("STAGE_STATE_INVALID", "existing Stage state contract disagrees with its canonical contract")
        return _plan_schema_payload(
            root,
            contract=contract,
            input_paths=input_paths,
            input_digests=input_digests,
            bootstrap_state=bootstrap_state,
            stage_state_path=state_path,
            contract_path=contract_path,
            reused=True,
            stage_view=stage,
        )

    # Validate and persist the contract before the controller sees it.  The
    # controller performs its own independent validation and writes the only
    # authoritative state snapshot.
    _atomic_json(contract_path, contract, max_bytes=MAX_ARTIFACT_BYTES)
    try:
        controller = StageController(state_path=state_path)
        registered = controller.prepare_stage(contract)
    except (StageControllerError, ContractValidationError) as exc:
        raise _fail("STAGE_REGISTRATION_REJECTED", "StageController rejected the derived contract", exc) from exc
    stage = registered.get("stage") if isinstance(registered, Mapping) else None
    if not isinstance(stage, Mapping) or stage.get("status") != StageState.PLANNED.value:
        raise _fail("STAGE_REGISTRATION_INVALID", "Stage registration did not result in PLANNED")
    plan = _plan_schema_payload(
        root,
        contract=contract,
        input_paths=input_paths,
        input_digests=input_digests,
        bootstrap_state=bootstrap_state,
        stage_state_path=state_path,
        contract_path=contract_path,
        reused=False,
        stage_view=stage,
    )
    _atomic_json(root / STAGE_PLAN_RELATIVE_PATH, plan)
    return plan


def _accepted_design_route(summary: Mapping[str, Any]) -> dict[str, Any]:
    try:
        normalized = normalize_design_summary(summary, require_core=True)
    except DesignReviewError as exc:
        raise _fail("DESIGN_REVIEW_INVALID", "accepted Design Review summary failed validation", exc) from exc
    stages = normalized.get("stages")
    if not isinstance(stages, list) or not stages:
        raise _fail("DESIGN_REVIEW_INVALID", "accepted Design Review has no Stage route")
    for route in stages:
        if isinstance(route, Mapping) and route.get("route_class") == "CANDIDATE_0":
            return dict(route)
    route = stages[0]
    if not isinstance(route, Mapping):
        raise _fail("DESIGN_REVIEW_INVALID", "accepted Design Review route is invalid")
    return dict(route)


def _accepted_option_list(options: Mapping[str, Any], key: str) -> list[str] | None:
    value = options.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        raise _fail("EXECUTION_HANDOFF_INPUT_INVALID", f"{key} must be an array")
    result: list[str] = []
    for item in value:
        checked = _text(item, f"{key} item", max_length=512)
        if checked not in result:
            result.append(checked)
    return result


def _accepted_stage_contract(
    root: Path,
    *,
    brief: Mapping[str, Any],
    design_review: Mapping[str, Any],
    design_package: Mapping[str, Any],
    design_package_digest: str,
    human_accept_receipt: Mapping[str, Any],
    human_accept_receipt_digest: str,
    research_provenance: Sequence[Mapping[str, Any]],
    input_paths: Sequence[str],
    input_digests: Mapping[str, str],
    input_sizes: Mapping[str, int],
    stage_options: Mapping[str, Any],
    stage_plan_relative: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Derive a Stage contract and an auditable handoff from approved facts."""

    project_id = _text(brief.get("project_id"), "project_id", max_length=200)
    brief_digest = _brief_digest(brief)
    design_digest = _validate_digest(design_review.get("design_digest"), "design_digest")
    if design_digest != design_summary_digest(design_review.get("summary", {})):
        raise _fail("DESIGN_REVIEW_INVALID", "accepted Design Review digest is inconsistent")
    receipt = validate_human_accept_receipt(
        human_accept_receipt,
        project_id=project_id,
        brief_digest=brief_digest,
        design_digest=design_digest,
    )
    route = _accepted_design_route(design_review["summary"])
    raw_route_id = route.get("stage_id", route.get("route_id", route.get("id")))
    route_id = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw_route_id or "accepted-design").strip()).strip("-") or "accepted-design"
    route_id = route_id[:48]
    derived_stage_id = f"stage-{route_id}-{design_digest[:12]}"
    selected_stage_id = _text(stage_options.get("stage_id") or derived_stage_id, "stage_id", max_length=80)
    if _STAGE_ID_RE.fullmatch(selected_stage_id) is None:
        raise _fail("STAGE_ID_INVALID", "stage_id must contain only letters, numbers, dot, underscore, or hyphen")
    selected_stage_plan_relative = stage_plan_relative or _accepted_stage_plan_relative(selected_stage_id)
    stage_name = _text(stage_options.get("stage_name") or route.get("name") or route.get("stage_id") or "Accepted design route", "stage_name", max_length=512)

    nested_brief = brief.get("brief") if isinstance(brief.get("brief"), Mapping) else brief
    project_goal = _text(
        design_review["summary"].get("project_final_goal") or nested_brief.get("goal") or nested_brief.get("desired_outcome") or "Execute the approved design route.",
        "project_goal",
        max_length=4000,
    )
    stage_goal = _text(route.get("goal") or route.get("name") or "Execute the accepted design route.", "stage_goal", max_length=4000)
    user_visible_goal = _text(nested_brief.get("desired_outcome") or project_goal, "user_visible_goal", max_length=4000)

    allowed_paths = _accepted_option_list(stage_options, "allowed_paths")
    if allowed_paths is None:
        allowed_paths = _accepted_option_list(design_review["summary"], "allowed_paths")
    if not allowed_paths:
        allowed_paths = [f".research/stages/{selected_stage_id}"]
    checked_allowed = [_relative_path(item, "allowed_paths") for item in allowed_paths]
    if len(set(checked_allowed)) != len(checked_allowed):
        raise _fail("STAGE_SCOPE_INVALID", "allowed_paths must not contain duplicates")

    protected = [
        ".git",
        ".auth",
        ".consultations",
        ".research/PROJECT_BRIEF.json",
        DESIGN_PACKAGE_RELATIVE_PATH.as_posix(),
        ".research/workflow-state.json",
        HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix(),
        EXECUTION_HANDOFF_RELATIVE_PATH.as_posix(),
        selected_stage_plan_relative,
        STAGE_STATE_RELATIVE_PATH.as_posix(),
    ]
    selected_protected = _accepted_option_list(stage_options, "protected_paths")
    if selected_protected is None:
        selected_protected = _accepted_option_list(design_review["summary"], "protected_paths")
    for item in selected_protected or []:
        normalized = _relative_path(item, "protected_paths")
        if normalized not in protected:
            protected.append(normalized)
    protected.extend(str(item["path"]) for item in research_provenance if str(item["path"]) not in protected)
    protected = list(dict.fromkeys(protected))

    required_checks = _accepted_option_list(stage_options, "required_checks")
    if required_checks is None:
        required_checks = _accepted_option_list(design_review["summary"], "required_checks")
    if required_checks is None:
        package_checks = design_package.get("success_criteria")
        required_checks = _accepted_option_list({"required_checks": package_checks}, "required_checks") if package_checks else None
    if not required_checks:
        required_checks = ["accepted_design_handoff_verified"]

    review_artifacts = _accepted_option_list(stage_options, "review_artifact_requirements")
    if review_artifacts is None:
        review_artifacts = _accepted_option_list(design_review["summary"], "review_artifacts")
    if not review_artifacts:
        review_artifacts = ["execution_result", "test_report"]

    max_iterations = stage_options.get("max_iterations", 1)
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or not 1 <= max_iterations <= 100:
        raise _fail("STAGE_ITERATIONS_INVALID", "max_iterations must be between 1 and 100")
    retry_budget = stage_options.get("retry_budget")
    if retry_budget is not None and (
        isinstance(retry_budget, bool) or not isinstance(retry_budget, int) or not 1 <= retry_budget <= 100
    ):
        raise _fail("STAGE_RETRY_BUDGET_INVALID", "retry_budget must be between 1 and 100")

    baseline: dict[str, Any] = {
        "schema_version": "stage_baseline.v1",
        "handoff_schema_version": EXECUTION_HANDOFF_SCHEMA_VERSION,
        "project_id": project_id,
        "brief_digest": brief_digest,
        "design_digest": design_digest,
        "design_package_digest": design_package_digest,
        "design_package_file_digest": input_digests.get(DESIGN_PACKAGE_RELATIVE_PATH.as_posix()),
        "human_accept_receipt_digest": human_accept_receipt_digest,
        "human_accept_receipt_file_digest": input_digests.get(HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix()),
        "research_provenance": [copy.deepcopy(dict(item)) for item in research_provenance],
        "source_paths": list(input_paths),
        "source_digests": dict(input_digests),
        "source_sizes": {key: int(value) for key, value in input_sizes.items()},
        "route_id": route_id,
    }
    _assert_safe(baseline, path="baseline")
    plan_id = "plan-stage-" + sha256_json({"project_id": project_id, "stage_id": selected_stage_id, "baseline": baseline})[:20]
    contract: dict[str, Any] = {
        "schema_version": "stage_contract.v1",
        "plan_id": plan_id,
        "project_id": project_id,
        "repository_root": root.as_posix(),
        "stage_id": selected_stage_id,
        "stage_name": stage_name,
        "project_goal": project_goal,
        "stage_goal": stage_goal,
        "user_visible_goal": user_visible_goal,
        "inputs": list(input_paths),
        "protected_paths": protected,
        "allowed_paths": checked_allowed,
        "acceptance_description": _text(
            "; ".join(_route_steps(route)) or str(route.get("goal") or "Execute the accepted design route."),
            "acceptance_description",
            max_length=12_000,
        ),
        "required_checks": required_checks,
        "review_artifact_requirements": review_artifacts,
        "baseline": baseline,
        "status": StageState.PLANNED.value,
        "lane_mode": "PRIMARY",
        "comparison_mode": "PRIMARY",
        "critic_mode": "NONE",
        "max_iterations": max_iterations,
        "metadata": {
            "step": "design-approval-handoff-v1",
            "planning_mode": "accepted_design_to_stage_contract",
            "handoff_marker": EXECUTION_HANDOFF_MARKER,
            "route_id": route_id,
            "design_package_digest": design_package_digest,
            "human_accept_receipt_digest": human_accept_receipt_digest,
            "no_automatic_start": True,
        },
    }
    if retry_budget is not None:
        contract["retry_budget"] = retry_budget
    try:
        contract = validate_stage_contract_v1(contract)
    except ContractValidationError as exc:
        raise _fail("STAGE_CONTRACT_INVALID", "accepted-design Stage contract failed validation", exc) from exc

    handoff: dict[str, Any] = {
        "schema_version": EXECUTION_HANDOFF_SCHEMA_VERSION,
        "marker": EXECUTION_HANDOFF_MARKER,
        "status": "READY_FOR_STAGE_PLANNING",
        "project_id": project_id,
        "repository_root": root.as_posix(),
        "requirement": {
            "path": ".research/PROJECT_BRIEF.json",
            "revision": brief.get("revision"),
            "digest": brief_digest,
        },
        "design": {
            "path": DESIGN_PACKAGE_RELATIVE_PATH.as_posix(),
            "digest": design_package_digest,
            "file_digest": input_digests[DESIGN_PACKAGE_RELATIVE_PATH.as_posix()],
            "review_digest": design_digest,
            "review_revision": design_review.get("revision"),
        },
        "human_decision": {
            "decision": "ACCEPT",
            "actor": receipt["actor"],
            "receipt_path": HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix(),
            "receipt_digest": human_accept_receipt_digest,
            "receipt_file_digest": input_digests.get(HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix()),
        },
        "research_provenance": [copy.deepcopy(dict(item)) for item in research_provenance],
        "inputs": list(input_paths),
        "input_digests": dict(input_digests),
        "stage": {
            "stage_id": contract["stage_id"],
            "stage_created": False,
            "stage_started": False,
        },
        "stage_contract_path": f".research/stages/{contract['stage_id']}/{STAGE_CONTRACT_FILENAME}",
        "stage_plan_path": selected_stage_plan_relative,
    }
    handoff["handoff_digest"] = sha256_json(handoff)
    _assert_safe(handoff, path="execution_handoff")
    return contract, handoff


def _accepted_plan_payload(
    root: Path,
    *,
    contract: Mapping[str, Any],
    handoff: Mapping[str, Any],
    input_paths: Sequence[str],
    input_digests: Mapping[str, str],
    input_sizes: Mapping[str, int],
    receipt_digest: str,
    reused: bool,
    stage_view: Mapping[str, Any] | None,
    stage_plan_relative: str,
) -> dict[str, Any]:
    contract_digest = sha256_json(contract)
    baseline_digest = sha256_json(contract["baseline"])
    payload: dict[str, Any] = {
        "schema_version": STAGE_PLANNING_SCHEMA_VERSION,
        "project_id": contract["project_id"],
        "repository_root": root.as_posix(),
        "plan_id": contract["plan_id"],
        "stage_id": contract["stage_id"],
        "status": STAGE_PLANNING_STATUS,
        "planning_status": "STAGE_PLANNING_COMPLETE",
        "planning_mode": "accepted_design_to_stage_contract",
        "stage_status": StageState.PLANNED.value,
        "next_action": STAGE_PLANNING_NEXT_ACTION,
        "stage_created": True,
        "stage_started": False,
        "stage_contract_path": f".research/stages/{contract['stage_id']}/{STAGE_CONTRACT_FILENAME}",
        "contract_path": f".research/stages/{contract['stage_id']}/{STAGE_CONTRACT_FILENAME}",
        "stage_state_path": STAGE_STATE_RELATIVE_PATH.as_posix(),
        "state_path": STAGE_STATE_RELATIVE_PATH.as_posix(),
        "stage_plan_path": stage_plan_relative,
        "execution_handoff_path": EXECUTION_HANDOFF_RELATIVE_PATH.as_posix(),
        # ``handoff_digest`` is the handoff's self-binding digest.  Keep the
        # same value in the Stage plan so resume verification can compare the
        # two envelopes without depending on JSON serialization bytes.
        "execution_handoff_digest": handoff.get("handoff_digest"),
        "design_package_path": DESIGN_PACKAGE_RELATIVE_PATH.as_posix(),
        "design_package_digest": contract["baseline"]["design_package_digest"],
        "human_accept_receipt_path": HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix(),
        "human_accept_receipt_digest": receipt_digest,
        "requirement_digest": contract["baseline"]["brief_digest"],
        "design_digest": contract["baseline"]["design_digest"],
        "input_paths": list(input_paths),
        "input_digests": dict(input_digests),
        "input_sizes": {key: int(value) for key, value in input_sizes.items()},
        "baseline_digest": baseline_digest,
        "contract_digest": contract_digest,
        "marker": STAGE_PLANNING_MARKER,
        "idempotent_reuse": bool(reused),
        "stage": copy.deepcopy(dict(stage_view)) if isinstance(stage_view, Mapping) else None,
        "handoff_marker": EXECUTION_HANDOFF_MARKER,
    }
    try:
        validate_instance(payload, load_schema("stage_planning"))
    except ContractValidationError as exc:
        raise _fail("STAGE_PLAN_SCHEMA_INVALID", "accepted-design Stage plan failed schema validation", exc) from exc
    _assert_safe(payload, path="stage_plan")
    return payload


def validate_execution_handoff(
    value: Mapping[str, Any],
    *,
    project_id: str | None = None,
    brief_digest: str | None = None,
    design_package_digest: str | None = None,
    design_digest: str | None = None,
) -> dict[str, Any]:
    """Validate a persisted execution handoff without starting a Stage."""

    if not isinstance(value, Mapping):
        raise _fail("EXECUTION_HANDOFF_INVALID", "execution handoff must be an object")
    detached = copy.deepcopy(dict(value))
    _assert_safe(detached, path="execution_handoff")
    try:
        validate_instance(detached, load_schema("execution_handoff.v1"))
    except ContractValidationError as exc:
        raise _fail("EXECUTION_HANDOFF_INVALID", "execution handoff failed schema validation", exc) from exc
    if detached.get("handoff_digest") != sha256_json({key: item for key, item in detached.items() if key != "handoff_digest"}):
        raise _fail("EXECUTION_HANDOFF_DIGEST_MISMATCH", "execution handoff digest is inconsistent")
    if project_id is not None and detached.get("project_id") != project_id:
        raise _fail("EXECUTION_HANDOFF_IDENTITY_MISMATCH", "execution handoff project identity differs")
    requirement = detached["requirement"]
    design = detached["design"]
    if brief_digest is not None and requirement.get("digest") != brief_digest:
        raise _fail("EXECUTION_HANDOFF_BINDING_MISMATCH", "execution handoff requirement digest is stale")
    if design_package_digest is not None and design.get("digest") != design_package_digest:
        raise _fail("EXECUTION_HANDOFF_BINDING_MISMATCH", "execution handoff Design Package digest is stale")
    if design_digest is not None and design.get("review_digest") != design_digest:
        raise _fail("EXECUTION_HANDOFF_BINDING_MISMATCH", "execution handoff Design Review digest is stale")
    stage = detached.get("stage")
    status = detached.get("status")
    if not isinstance(stage, Mapping):
        raise _fail("EXECUTION_HANDOFF_INVALID", "execution handoff Stage metadata is missing")
    if status == "READY_FOR_STAGE_PLANNING":
        if stage.get("stage_created") is not False or stage.get("stage_started") is not False:
            raise _fail("EXECUTION_HANDOFF_BOUNDARY_VIOLATION", "pre-planning handoff cannot claim Stage side effects")
    elif status == "STAGE_PLANNED":
        if stage.get("stage_created") is not True or stage.get("stage_started") is not False:
            raise _fail("EXECUTION_HANDOFF_BOUNDARY_VIOLATION", "planned handoff must prove created-but-not-started Stage")
    else:  # schema validation normally catches this; keep a stable code here.
        raise _fail("EXECUTION_HANDOFF_INVALID", "execution handoff status is not supported")
    return detached


def plan_stage_from_accepted_design(
    project_root: str | os.PathLike[str] = ".",
    *,
    accepted_review: Mapping[str, Any],
    human_accept_receipt: Mapping[str, Any],
    research_provenance: Any = None,
    stage_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one PLANNED Stage directly from an accepted Design Package.

    This is the Design Approval → Stage Execution Handoff V1 entry point.  It
    intentionally never loads or writes ``bootstrap_state.json`` and never
    interprets Discovery/Blueprint artifacts.  Stage registration still goes
    through :class:`StageController`, and reruns with unchanged evidence are
    idempotent.
    """

    try:
        root = resolve_project_root(project_root)
    except ProjectStateError as exc:
        raise _fail("PROJECT_ROOT_INVALID", "project root could not be resolved", exc) from exc
    if not isinstance(accepted_review, Mapping) or accepted_review.get("state") != "ACCEPTED":
        raise _fail("DESIGN_REVIEW_NOT_ACCEPTED", "an accepted Design Review is required")
    try:
        brief_raw = load_project_brief(root)
        brief = validate_project_brief(brief_raw, root)
    except (ProjectStateError, ProjectIntakeError, RuntimeError) as exc:
        raise _fail("BRIEF_INVALID", "approved project brief could not be loaded", exc) from exc
    if brief.get("state") != "APPROVED" or brief.get("status") != "APPROVED":
        raise _fail("BRIEF_NOT_APPROVED", "accepted-design planning requires state=APPROVED")
    project_id = str(brief.get("project_id"))
    brief_digest = _brief_digest(brief)
    summary = accepted_review.get("summary")
    design_digest = _validate_digest(accepted_review.get("design_digest"), "design_digest")
    if not isinstance(summary, Mapping) or design_summary_digest(summary) != design_digest:
        raise _fail("DESIGN_REVIEW_INVALID", "accepted Design Review summary digest is invalid")
    package_path = root / DESIGN_PACKAGE_RELATIVE_PATH
    design_package, package_digest, package_file_digest, _ = _read_design_package(root, package_path)
    receipt = validate_human_accept_receipt(
        human_accept_receipt,
        project_id=project_id,
        brief_digest=brief_digest,
        design_digest=design_digest,
    )
    human_decision = accepted_review.get("human_decision")
    if not isinstance(human_decision, Mapping) or human_decision.get("decision") != "ACCEPT":
        raise _fail("HUMAN_ACCEPT_RECEIPT_INVALID", "accepted Design Review has no ACCEPT decision")
    if human_decision.get("actor") != receipt.get("actor"):
        raise _fail("HUMAN_ACCEPT_RECEIPT_BINDING_MISMATCH", "Human ACCEPT actor differs from the accepted decision")
    if human_decision.get("receipt_digest") is not None and human_decision.get("receipt_digest") != receipt.get("receipt_digest"):
        raise _fail("HUMAN_ACCEPT_RECEIPT_BINDING_MISMATCH", "accepted decision receipt digest is stale")
    embedded_receipt = accepted_review.get("human_accept_receipt")
    if embedded_receipt is not None and canonical_json(dict(embedded_receipt)) != canonical_json(receipt):
        raise _fail("HUMAN_ACCEPT_RECEIPT_BINDING_MISMATCH", "embedded Human ACCEPT receipt differs from the accepted receipt")
    receipt_path = root / HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH
    if receipt_path.exists() or receipt_path.is_symlink():
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise _fail("HUMAN_ACCEPT_RECEIPT_INVALID", "canonical Human ACCEPT receipt is not a regular file")
        persisted_receipt = _read_json(receipt_path, "Human ACCEPT receipt")
        checked_persisted = validate_human_accept_receipt(
            persisted_receipt,
            project_id=project_id,
            brief_digest=brief_digest,
            design_digest=design_digest,
        )
        if canonical_json(checked_persisted) != canonical_json(receipt):
            raise _fail("HUMAN_ACCEPT_RECEIPT_BINDING_MISMATCH", "provided Human ACCEPT receipt differs from the canonical receipt")
    else:
        _atomic_json(receipt_path, receipt, max_bytes=MAX_ARTIFACT_BYTES)
    receipt_file_digest, receipt_size = _file_digest(receipt_path, "Human ACCEPT receipt")
    if receipt_file_digest != sha256_json(receipt):
        # The byte digest is retained for mutation detection; the logical
        # receipt digest is the contract binding.  Do not conflate them.
        pass

    provenance = _load_research_provenance(
        root,
        brief=brief,
        accepted_review=accepted_review,
        supplied=research_provenance,
    )
    options = dict(stage_options or {})
    allowed_options = {
        "stage_id", "stage_name", "allowed_paths", "required_checks",
        "review_artifact_requirements", "protected_paths", "max_iterations", "retry_budget",
    }
    unknown = sorted(set(options) - allowed_options)
    if unknown:
        raise _fail("EXECUTION_HANDOFF_INPUT_INVALID", f"unsupported stage option(s): {unknown!r}")
    input_paths, input_digests, input_sizes = _accepted_input_descriptors(root, research_provenance=provenance)
    # The receipt file is an explicit Human ACCEPT input even if an unusual
    # caller supplied a differently shaped provenance object.
    if HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix() not in input_digests:
        input_paths.append(HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix())
        input_digests[HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix()] = receipt_file_digest
        input_sizes[HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix()] = receipt_size

    legacy_existing = _load_existing_plan(root)
    requested_stage_id = options.get("stage_id")
    if (
        isinstance(legacy_existing, Mapping)
        and isinstance(requested_stage_id, str)
        and requested_stage_id.strip()
        and legacy_existing.get("stage_id") != requested_stage_id.strip()
    ):
        stage_plan_relative = _accepted_stage_plan_relative(requested_stage_id.strip())
    else:
        stage_plan_relative = STAGE_PLAN_RELATIVE_PATH.as_posix()

    contract, handoff = _accepted_stage_contract(
        root,
        brief=brief,
        design_review=accepted_review,
        design_package=design_package,
        design_package_digest=package_digest,
        human_accept_receipt=receipt,
        human_accept_receipt_digest=receipt["receipt_digest"],
        research_provenance=provenance,
        input_paths=input_paths,
        input_digests=input_digests,
        input_sizes=input_sizes,
        stage_options=options,
        stage_plan_relative=stage_plan_relative,
    )
    contract_path = root / ".research" / "stages" / contract["stage_id"] / STAGE_CONTRACT_FILENAME
    state_path = root / STAGE_STATE_RELATIVE_PATH
    existing = _load_existing_plan(root, relative_path=stage_plan_relative)
    if existing is not None:
        if existing.get("planning_mode") != "accepted_design_to_stage_contract":
            raise _fail("STAGE_PLAN_INPUT_MISMATCH", "existing Stage plan belongs to the legacy Bootstrap route")
        if existing.get("project_id") != project_id or existing.get("stage_id") != contract["stage_id"] or existing.get("input_digests") != input_digests:
            raise _fail("STAGE_PLAN_INPUT_MISMATCH", "existing accepted-design Stage plan has different evidence")
        existing_contract = _read_json(contract_path, "stage contract")
        if canonical_json(validate_stage_contract_v1(existing_contract)) != canonical_json(contract):
            raise _fail("STAGE_PLAN_INPUT_MISMATCH", "existing accepted-design contract differs from current design")
        try:
            controller = StageController.from_state(state_path)
            stage = controller.show_stage(contract["stage_id"])
        except (StageControllerError, ContractValidationError, OSError) as exc:
            raise _fail("STAGE_STATE_INVALID", "existing accepted-design Stage state failed validation", exc) from exc
        if stage.get("status") != StageState.PLANNED.value:
            raise _fail("STAGE_PLAN_BOUNDARY_VIOLATION", "existing accepted-design Stage is no longer PLANNED")
        handoff["stage"] = {"stage_id": contract["stage_id"], "stage_created": True, "stage_started": False}
        handoff["status"] = "STAGE_PLANNED"
        handoff.pop("handoff_digest", None)
        handoff["handoff_digest"] = sha256_json(handoff)
        _atomic_json(root / EXECUTION_HANDOFF_RELATIVE_PATH, handoff, max_bytes=MAX_ARTIFACT_BYTES)
        return _accepted_plan_payload(
            root,
            contract=contract,
            handoff=handoff,
            input_paths=input_paths,
            input_digests=input_digests,
            input_sizes=input_sizes,
            receipt_digest=receipt["receipt_digest"],
            reused=True,
            stage_view=stage,
            stage_plan_relative=stage_plan_relative,
        )

    _atomic_json(contract_path, contract, max_bytes=MAX_ARTIFACT_BYTES)
    _atomic_json(root / EXECUTION_HANDOFF_RELATIVE_PATH, handoff, max_bytes=MAX_ARTIFACT_BYTES)
    try:
        controller = StageController(state_path=state_path)
        registered = controller.prepare_stage(contract)
    except (StageControllerError, ContractValidationError) as exc:
        raise _fail("STAGE_REGISTRATION_REJECTED", "StageController rejected the accepted-design contract", exc) from exc
    stage = (
        registered.get("stage")
        if isinstance(registered, Mapping) and isinstance(registered.get("stage"), Mapping)
        else registered
        if isinstance(registered, Mapping) and isinstance(registered.get("status"), str)
        else None
    )
    if not isinstance(stage, Mapping) or stage.get("status") != StageState.PLANNED.value:
        raise _fail("STAGE_REGISTRATION_INVALID", "accepted-design Stage registration did not result in PLANNED")
    handoff["stage"] = {"stage_id": contract["stage_id"], "stage_created": True, "stage_started": False}
    handoff["status"] = "STAGE_PLANNED"
    handoff.pop("handoff_digest", None)
    handoff["handoff_digest"] = sha256_json(handoff)
    _atomic_json(root / EXECUTION_HANDOFF_RELATIVE_PATH, handoff, max_bytes=MAX_ARTIFACT_BYTES)
    plan = _accepted_plan_payload(
        root,
        contract=contract,
        handoff=handoff,
        input_paths=input_paths,
        input_digests=input_digests,
        input_sizes=input_sizes,
        receipt_digest=receipt["receipt_digest"],
        reused=False,
        stage_view=stage,
        stage_plan_relative=stage_plan_relative,
    )
    _atomic_json(root / stage_plan_relative, plan)
    return plan


plan_from_accepted_design = plan_stage_from_accepted_design
plan_accepted_design = plan_stage_from_accepted_design


build_stage_plan = plan_stage
plan_from_bootstrap = plan_stage
create_stage_plan = plan_stage
generate_stage_plan = plan_stage
prepare_stage = plan_stage


def load_stage_plan(project_root: str | os.PathLike[str] = ".") -> dict[str, Any]:
    root = resolve_project_root(project_root)
    # Accepted-design plans are immutable per-Stage artifacts.  Prefer the
    # plan belonging to the controller's current Stage, while retaining the
    # legacy shared path for older Bootstrap plans and historical evidence.
    existing = None
    state_path = root / STAGE_STATE_RELATIVE_PATH
    if state_path.is_file() and not state_path.is_symlink():
        try:
            controller = StageController.from_state(state_path)
            current_stage_id = controller.state.get("current_stage_id")
            if isinstance(current_stage_id, str) and _STAGE_ID_RE.fullmatch(current_stage_id):
                existing = _load_existing_plan(
                    root,
                    relative_path=_accepted_stage_plan_relative(current_stage_id),
                )
        except (StageControllerError, ContractValidationError, OSError, ValueError):
            existing = None
    if existing is None:
        existing = _load_existing_plan(root)
    if existing is None:
        raise _fail("STAGE_PLAN_NOT_FOUND", "no Step 15 Stage plan exists")
    return existing


load_plan = load_stage_plan


def validate_stage_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise _fail("STAGE_PLAN_INVALID", "Stage plan must be an object")
    try:
        validate_instance(dict(plan), load_schema("stage_planning"))
    except ContractValidationError as exc:
        raise _fail("STAGE_PLAN_INVALID", "Stage plan failed schema validation", exc) from exc
    _assert_safe(plan, path="stage_plan")
    return copy.deepcopy(dict(plan))


def _verify_accepted_design_stage_plan(root: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a Stage plan produced from the approved-design handoff.

    This path is intentionally independent from the legacy Bootstrap
    verifier.  Every input is still re-read and byte-bound, while the
    lifecycle assertion is delegated to ``StageController``.
    """

    try:
        plan = validate_stage_plan(plan)
    except StagePlanningError:
        raise
    if plan.get("planning_mode") != "accepted_design_to_stage_contract":
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "accepted-design verifier received a legacy Stage plan")
    if plan.get("handoff_marker") != EXECUTION_HANDOFF_MARKER:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "accepted-design Stage plan is missing its handoff marker")
    try:
        brief_raw = load_project_brief(root)
        brief = validate_project_brief(brief_raw, root)
    except (ProjectStateError, ProjectIntakeError, RuntimeError) as exc:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "approved Requirement Baseline could not be verified", exc) from exc
    if brief.get("state") != "APPROVED" or brief.get("status") != "APPROVED":
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "accepted-design Stage plan requires an approved Requirement Baseline")
    brief_digest = _brief_digest(brief)
    if plan.get("project_id") != brief.get("project_id") or plan.get("requirement_digest") != brief_digest:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "accepted-design Stage plan requirement identity is stale")

    package_path = root / DESIGN_PACKAGE_RELATIVE_PATH
    package, package_digest, package_file_digest, _ = _read_design_package(root, package_path)
    if plan.get("design_package_path") != DESIGN_PACKAGE_RELATIVE_PATH.as_posix() or plan.get("design_package_digest") != package_digest:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "accepted-design Stage plan Design Package digest is stale")

    handoff_relative, handoff_path = _checked_repo_path(
        root,
        plan.get("execution_handoff_path"),
        "execution_handoff_path",
        require_file=True,
    )
    if handoff_relative != EXECUTION_HANDOFF_RELATIVE_PATH.as_posix():
        raise _fail("EXECUTION_HANDOFF_PATH_INVALID", "execution handoff path is not canonical")
    handoff = _read_json(handoff_path, "execution handoff", max_bytes=MAX_ARTIFACT_BYTES)
    checked_handoff = validate_execution_handoff(
        handoff,
        project_id=str(brief["project_id"]),
        brief_digest=brief_digest,
        design_package_digest=package_digest,
        design_digest=str(plan.get("design_digest")),
    )
    if checked_handoff.get("status") != "STAGE_PLANNED":
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "execution handoff has not reached STAGE_PLANNED")
    if plan.get("execution_handoff_digest") != checked_handoff.get("handoff_digest"):
        raise _fail("STAGE_PLAN_BINDING_MISMATCH", "Stage plan and execution handoff digests disagree")
    if checked_handoff.get("stage", {}).get("stage_id") != plan.get("stage_id"):
        raise _fail("STAGE_PLAN_BINDING_MISMATCH", "execution handoff stage identity disagrees with Stage plan")

    receipt_relative, receipt_path = _checked_repo_path(
        root,
        plan.get("human_accept_receipt_path"),
        "human_accept_receipt_path",
        require_file=True,
    )
    if receipt_relative != HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix():
        raise _fail("HUMAN_ACCEPT_RECEIPT_PATH_INVALID", "Human ACCEPT receipt path is not canonical")
    receipt = _read_json(receipt_path, "Human ACCEPT receipt")
    checked_receipt = validate_human_accept_receipt(
        receipt,
        project_id=str(brief["project_id"]),
        brief_digest=brief_digest,
        design_digest=str(plan.get("design_digest")),
    )
    if plan.get("human_accept_receipt_digest") != checked_receipt.get("receipt_digest"):
        raise _fail("STAGE_PLAN_BINDING_MISMATCH", "Stage plan Human ACCEPT receipt digest is stale")
    decision = checked_handoff.get("human_decision")
    if not isinstance(decision, Mapping) or decision.get("receipt_digest") != checked_receipt.get("receipt_digest"):
        raise _fail("EXECUTION_HANDOFF_BINDING_MISMATCH", "execution handoff Human ACCEPT decision is stale")

    input_paths, input_digests, input_sizes = _recompute_input_digests(root, plan.get("input_paths", []))
    if input_paths != list(plan.get("input_paths", [])) or input_digests != dict(plan.get("input_digests", {})):
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "accepted-design Stage inputs no longer match the repository")
    if plan.get("input_sizes") is not None and dict(plan.get("input_sizes", {})) != input_sizes:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "accepted-design Stage input sizes no longer match the repository")
    if input_digests.get(DESIGN_PACKAGE_RELATIVE_PATH.as_posix()) != package_file_digest:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "Design Package file digest is stale")
    receipt_file_digest = input_digests.get(HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH.as_posix())
    if not isinstance(receipt_file_digest, str):
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "Human ACCEPT receipt is absent from Stage inputs")
    if decision.get("receipt_file_digest") not in {None, receipt_file_digest}:
        raise _fail("EXECUTION_HANDOFF_BINDING_MISMATCH", "execution handoff receipt file digest is stale")
    if checked_handoff.get("inputs") != input_paths or checked_handoff.get("input_digests") != input_digests:
        raise _fail("EXECUTION_HANDOFF_BINDING_MISMATCH", "execution handoff input evidence is stale")
    for item in checked_handoff.get("research_provenance", []):
        if not isinstance(item, Mapping):
            raise _fail("RESEARCH_PROVENANCE_INVALID", "execution handoff research provenance is invalid")
        path = item.get("path")
        if path not in input_digests or item.get("digest") != input_digests[path]:
            raise _fail("RESEARCH_PROVENANCE_BINDING_MISMATCH", "research receipt digest is stale")

    contract_relative, contract_path = _checked_repo_path(
        root,
        plan.get("stage_contract_path"),
        "stage_contract_path",
        require_file=True,
    )
    expected_contract_relative = f".research/stages/{plan['stage_id']}/{STAGE_CONTRACT_FILENAME}"
    if contract_relative != expected_contract_relative:
        raise _fail("STAGE_CONTRACT_PATH_INVALID", "accepted-design Stage contract path is not canonical")
    contract_payload = _read_json(contract_path, "stage contract", max_bytes=MAX_ARTIFACT_BYTES)
    try:
        contract = validate_stage_contract_v1(contract_payload)
    except ContractValidationError as exc:
        raise _fail("STAGE_CONTRACT_INVALID", "accepted-design Stage contract failed validation", exc) from exc
    if canonical_json(contract) != canonical_json(contract_payload):
        raise _fail("STAGE_CONTRACT_INVALID", "accepted-design Stage contract is not canonical")
    if contract.get("project_id") != plan.get("project_id") or contract.get("stage_id") != plan.get("stage_id"):
        raise _fail("STAGE_PLAN_BINDING_MISMATCH", "accepted-design Stage contract identity differs")
    if contract.get("inputs") != input_paths:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "accepted-design Stage contract inputs differ")
    baseline = contract.get("baseline")
    if not isinstance(baseline, Mapping):
        raise _fail("STAGE_CONTRACT_INVALID", "accepted-design Stage contract baseline is missing")
    if baseline.get("source_paths") != input_paths or baseline.get("source_digests") != input_digests:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "accepted-design baseline source evidence differs")
    if baseline.get("source_sizes") != input_sizes:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "accepted-design baseline source sizes differ")
    if baseline.get("brief_digest") != brief_digest or baseline.get("design_package_digest") != package_digest:
        raise _fail("STAGE_PLAN_BINDING_MISMATCH", "accepted-design baseline requirement or package digest is stale")
    if baseline.get("design_digest") != plan.get("design_digest") or baseline.get("human_accept_receipt_digest") != checked_receipt.get("receipt_digest"):
        raise _fail("STAGE_PLAN_BINDING_MISMATCH", "accepted-design baseline design or decision digest is stale")
    if plan.get("baseline_digest") != sha256_json(baseline):
        raise _fail("STAGE_BASELINE_DIGEST_MISMATCH", "accepted-design Stage baseline no longer matches the plan")
    if plan.get("contract_digest") != sha256_json(contract_payload):
        raise _fail("STAGE_CONTRACT_DIGEST_MISMATCH", "accepted-design Stage contract no longer matches the plan")

    state_relative, state_path = _checked_repo_path(
        root,
        plan.get("stage_state_path"),
        "stage_state_path",
        require_file=True,
    )
    if state_relative != STAGE_STATE_RELATIVE_PATH.as_posix() or plan.get("state_path") != state_relative:
        raise _fail("STAGE_PLAN_PATH_INVALID", "accepted-design Stage state path is not canonical")
    try:
        controller = StageController.from_state(state_path)
        stage = controller.show_stage(str(plan["stage_id"]))
        state_contract = validate_stage_contract_v1(stage.get("contract"))
    except (StageControllerError, ContractValidationError, OSError) as exc:
        raise _fail("STAGE_STATE_INVALID", "accepted-design Stage state failed validation", exc) from exc
    if stage.get("status") != StageState.PLANNED.value:
        raise _fail("STAGE_PLAN_BOUNDARY_VIOLATION", "accepted-design Stage is no longer PLANNED")
    if canonical_json(state_contract) != canonical_json(contract):
        raise _fail("STAGE_STATE_INVALID", "StageController state contract disagrees with accepted-design contract")
    if plan.get("stage_started") is not False or plan.get("stage_status") != StageState.PLANNED.value:
        raise _fail("STAGE_PLAN_BOUNDARY_VIOLATION", "accepted-design Stage plan records an invalid lifecycle state")
    stage_plan_relative = _relative_path(plan.get("stage_plan_path"), "stage_plan_path")
    expected_stage_plan_relative = _accepted_stage_plan_relative(str(plan["stage_id"]))
    if stage_plan_relative not in {STAGE_PLAN_RELATIVE_PATH.as_posix(), expected_stage_plan_relative}:
        raise _fail("STAGE_PLAN_PATH_INVALID", "accepted-design Stage plan path is not canonical")
    return {
        "passed": True,
        "marker": STAGE_PLANNING_MARKER,
        "schema_version": STAGE_PLANNING_SCHEMA_VERSION,
        "status": plan["status"],
        "stage_status": stage["status"],
        "stage_id": stage["contract"]["stage_id"],
        "plan_id": plan["plan_id"],
        "stage_plan_path": stage_plan_relative,
        "stage_contract_path": plan["stage_contract_path"],
        "stage_state_path": plan["stage_state_path"],
        "execution_handoff_path": EXECUTION_HANDOFF_RELATIVE_PATH.as_posix(),
        "execution_handoff_digest": checked_handoff["handoff_digest"],
        "baseline_digest": plan["baseline_digest"],
        "contract_digest": plan["contract_digest"],
        "stage_started": False,
    }


def verify_stage_plan(project_root: str | os.PathLike[str] = ".") -> dict[str, Any]:
    root = resolve_project_root(project_root)
    plan = load_stage_plan(root)
    if plan.get("planning_mode") == "accepted_design_to_stage_contract":
        return _verify_accepted_design_stage_plan(root, plan)
    if plan.get("repository_root") != root.as_posix():
        try:
            plan_root = Path(str(plan.get("repository_root"))).expanduser()
            if not plan_root.is_absolute() or plan_root.resolve(strict=True) != root:
                raise _fail("STAGE_PLAN_ROOT_INVALID", "Stage plan repository_root disagrees with the repository")
        except (OSError, RuntimeError, ValueError) as exc:
            raise _fail("STAGE_PLAN_ROOT_INVALID", "Stage plan repository_root could not be resolved", exc) from exc
    if plan.get("stage_plan_path") != STAGE_PLAN_RELATIVE_PATH.as_posix():
        raise _fail("STAGE_PLAN_PATH_INVALID", "Stage plan path is not canonical")

    contract_relative, contract_path = _checked_repo_path(
        root,
        plan.get("stage_contract_path"),
        "stage_contract_path",
        require_file=True,
    )
    state_relative, state_path = _checked_repo_path(
        root,
        plan.get("stage_state_path"),
        "stage_state_path",
        require_file=True,
    )
    if state_relative != STAGE_STATE_RELATIVE_PATH.as_posix() or plan.get("state_path") != state_relative:
        raise _fail("STAGE_PLAN_PATH_INVALID", "Stage state path is not canonical")

    current_paths, current_digests, current_sizes = _recompute_input_digests(root, plan.get("input_paths", []))
    if current_paths != list(plan.get("input_paths", [])):
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "Stage plan input paths are not canonical")
    if current_digests != dict(plan.get("input_digests", {})):
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "Stage plan input digests no longer match the repository")
    expected_paths, expected_digests, expected_sizes = _canonical_input_descriptors(root, bootstrap_state={})
    if current_paths != expected_paths or current_digests != expected_digests:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "the canonical Bootstrap input set has changed")
    if current_sizes != expected_sizes:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "the canonical Bootstrap input sizes have changed")

    contract_payload = _read_json(contract_path, "stage contract", max_bytes=MAX_ARTIFACT_BYTES)
    checked_contract = _validate_contract_against_plan(
        root,
        plan=plan,
        contract_path=contract_path,
        contract_payload=contract_payload,
        input_paths=current_paths,
        input_digests=current_digests,
        input_sizes=current_sizes,
    )
    baseline = checked_contract["baseline"]
    _validate_baseline_sources(
        root,
        contract=checked_contract,
        plan=plan,
        input_digests=current_digests,
    )
    bootstrap_state, bootstrap_path = _load_bootstrap_state(root)
    _validate_bootstrap_state(
        bootstrap_state,
        project_id=str(plan["project_id"]),
        brief_digest=str(baseline["brief_digest"]),
        context_digest=str(baseline["context_digest"]),
        discovery_digest=str(baseline["discovery_digest"]),
        blueprint_digest=str(baseline["blueprint_digest"]),
        input_digests=current_digests,
    )
    if _digest_from_file(bootstrap_path, "bootstrap state") != current_digests[BOOTSTRAP_STATE_RELATIVE_PATH.as_posix()]:
        raise _fail("STAGE_PLAN_INPUT_MISMATCH", "bootstrap state changed while verifying the Stage plan")
    try:
        controller = StageController.from_state(state_path)
        stage = controller.show_stage(str(plan["stage_id"]))
    except (StageControllerError, ContractValidationError, OSError) as exc:
        raise _fail("STAGE_STATE_INVALID", "Stage state failed validation", exc) from exc
    if stage.get("status") != StageState.PLANNED.value:
        raise _fail("STAGE_PLAN_BOUNDARY_VIOLATION", "Stage is no longer PLANNED")
    if plan.get("stage_started") is not False or plan.get("stage_status") != StageState.PLANNED.value:
        raise _fail("STAGE_PLAN_BOUNDARY_VIOLATION", "Stage plan records an invalid lifecycle state")
    try:
        state_contract = validate_stage_contract_v1(stage.get("contract"))
    except ContractValidationError as exc:
        raise _fail("STAGE_STATE_INVALID", "Stage state contract failed validation", exc) from exc
    if canonical_json(state_contract) != canonical_json(checked_contract):
        raise _fail("STAGE_STATE_INVALID", "Stage state contract disagrees with the canonical contract")
    stage_snapshot = plan.get("stage")
    if isinstance(stage_snapshot, Mapping):
        if stage_snapshot.get("status") != stage.get("status"):
            raise _fail("STAGE_PLAN_BINDING_MISMATCH", "Stage plan snapshot has a stale Stage status")
        snapshot_contract = stage_snapshot.get("contract")
        if isinstance(snapshot_contract, Mapping) and canonical_json(validate_stage_contract_v1(snapshot_contract)) != canonical_json(checked_contract):
            raise _fail("STAGE_PLAN_BINDING_MISMATCH", "Stage plan snapshot has a stale Stage contract")
    return {
        "passed": True,
        "marker": STAGE_PLANNING_MARKER,
        "schema_version": STAGE_PLANNING_SCHEMA_VERSION,
        "status": plan["status"],
        "stage_status": stage["status"],
        "stage_id": stage["contract"]["stage_id"],
        "plan_id": plan["plan_id"],
        "stage_plan_path": STAGE_PLAN_RELATIVE_PATH.as_posix(),
        "stage_contract_path": plan["stage_contract_path"],
        "stage_state_path": plan["stage_state_path"],
        "baseline_digest": plan["baseline_digest"],
        "contract_digest": plan["contract_digest"],
        "stage_started": False,
    }


verify_plan = verify_stage_plan


__all__ = [
    "BOOTSTRAP_STATE_MARKER",
    "BOOTSTRAP_STATE_NEXT_ACTION",
    "BOOTSTRAP_STATE_PATH",
    "BOOTSTRAP_STATE_RELATIVE_PATH",
    "BOOTSTRAP_STATE_SCHEMA_VERSION",
    "BOOTSTRAP_STATE_STATUS",
    "BLUEPRINT_STATE_PATH",
    "DESIGN_PACKAGE_PATH",
    "DESIGN_PACKAGE_RELATIVE_PATH",
    "DISCOVERY_STATE_PATH",
    "EXECUTION_HANDOFF_PATH",
    "EXECUTION_HANDOFF_RELATIVE_PATH",
    "EXECUTION_HANDOFF_SCHEMA_VERSION",
    "EXECUTION_HANDOFF_MARKER",
    "HUMAN_ACCEPT_RECEIPT_PATH",
    "HUMAN_ACCEPT_RECEIPT_RELATIVE_PATH",
    "HUMAN_ACCEPT_RECEIPT_SCHEMA_VERSION",
    "MAX_STAGE_PLAN_BYTES",
    "STAGE_CONTRACT_FILENAME",
    "STAGE_PLAN_FILENAME",
    "STAGE_PLAN_PATH",
    "STAGE_PLAN_RELATIVE_PATH",
    "STAGE_PLANNING_MARKER",
    "STAGE_PLANNING_NEXT_ACTION",
    "STAGE_PLANNING_SCHEMA_VERSION",
    "STAGE_PLANNING_STATUS",
    "STAGE_STATE_PATH",
    "STAGE_STATE_RELATIVE_PATH",
    "StagePlanError",
    "StagePlanningError",
    "bootstrap_state_digest",
    "build_human_accept_receipt",
    "build_stage_plan",
    "create_stage_plan",
    "generate_stage_plan",
    "load_plan",
    "load_stage_plan",
    "plan_from_bootstrap",
    "plan_from_accepted_design",
    "plan_accepted_design",
    "plan_stage",
    "plan_stage_from_accepted_design",
    "prepare_stage",
    "validate_stage_plan",
    "validate_execution_handoff",
    "validate_human_accept_receipt",
    "verify_plan",
    "verify_stage_plan",
]
