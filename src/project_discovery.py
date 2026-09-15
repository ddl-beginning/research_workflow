"""Bounded GPT Project Discovery (Step 13).

This module is deliberately a small layer between the approved requirements
brief and later, user-gated Stage work.  It performs one injected consultation
in ``fresh`` mode, verifies any repository claims locally, and writes one
canonical report at ``.research/discovery/DISCOVERY_REPORT.json``.  It never
creates or starts a Stage and it never writes a business/source file.

The default consultation transport is intentionally absent.  A real browser
bridge can be supplied by an explicit caller, while tests and offline
acceptance use a fake consultant.  A default Git verifier is provided because
repository claims must not be promoted to facts merely because GPT mentioned
them.  Candidate clones are made under a temporary directory and are deleted
when verification finishes.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit, urlunsplit

from .artifact_retention import (
    ARTIFACT_RETENTION_MANIFEST_RELATIVE_PATH,
    ArtifactClass,
    ArtifactRetentionError,
    build_retention_manifest,
    load_retention_manifest,
    save_retention_manifest,
)
from .bridge_adapter import (
    BridgeEnvelopeError,
    is_bridge_envelope,
    normalize_bridge_envelope,
    normalize_project_url as normalize_bridge_project_url,
)
from .asset_layer import (
    ASSET_LAYER_MARKER,
    AVAILABLE_ASSETS_RELATIVE_PATH,
    AssetLayerError,
    CANDIDATE_USER_PROJECT_ASSET_ID,
    LOCAL_PROJECT_PROFILE_RELATIVE_PATH,
    build_asset_pack,
    collect_available_assets,
    load_available_assets,
    load_local_project_profile,
    record_verified_candidates,
)
from .contracts import ContractValidationError, canonical_json, load_schema, sha256_json, validate_instance
from .project_context import (
    PROJECT_CONTEXT_RELATIVE_PATH,
    ProjectContextError,
    load_project_context,
)
from .project_intake import ProjectIntakeError, validate_project_brief
from .project_state import PROJECT_BRIEF_RELATIVE_PATH, ProjectStateError, load_project_brief, resolve_project_root
from .research_prompt_policy import method_evidence_policy_text


DISCOVERY_REPORT_SCHEMA_VERSION = "discovery_report.v1"
DISCOVERY_REPORT_RELATIVE_PATH = Path(".research") / "discovery" / "DISCOVERY_REPORT.json"
DISCOVERY_REPORT_FILENAME = DISCOVERY_REPORT_RELATIVE_PATH.name
DISCOVERY_REPORT_PATH = DISCOVERY_REPORT_RELATIVE_PATH
DISCOVERY_REPORT_MARKER = "GPT_PROJECT_DISCOVERY_PASS"
DISCOVERY_MARKER = "GPT_PROJECT_DISCOVERY_PASS"
PROJECT_DISCOVERY_MARKER = DISCOVERY_MARKER
DISCOVERY_STATUS = "DISCOVERY_COMPLETE"
CONSULTATION_MODE_FRESH = "fresh"
FRESH = CONSULTATION_MODE_FRESH
MAX_CANDIDATE_REPOSITORIES = 5
MAX_ALTERNATIVES = 4
MAX_NON_REPO_METHODS = 8
MAX_RISKS = 16
MAX_FACTS = 16
MAX_TOP_LEVEL_ENTRIES = 80
MAX_CODE_PATHS = 120
# This per-string bound is shared by semantic response fields and compact
# local evidence/pack excerpts.  Keep it unchanged when widening only the
# transient whole-response envelope below.
MAX_TEXT_LENGTH = 12_000
# The consultation transport may return a modestly larger envelope than one
# semantic field so a complete JSON object with all required keys and escaped
# controls can pass the lexical precheck.  This remains a finite parser guard,
# not an unbounded input allowance; semantic fields, arrays, packs, and the
# persisted report retain their own limits above/below.
MAX_RESPONSE_TEXT_LENGTH = 16_000
MAX_RESPONSE_BYTES = MAX_RESPONSE_TEXT_LENGTH * 4
MAX_RESPONSE_PROSE_LENGTH = 2_000
MAX_REPORT_BYTES = 180_000
MAX_GIT_TIMEOUT_SECONDS = 30
MAX_CONTEXT_BYTES = 120_000

# These are the only semantic field labels that may cross the failed
# consultation boundary.  They are deliberately a closed enum: diagnostics
# may say which bounded part of the response failed, but must never retain a
# response value, path, key name, or parser message.
DISCOVERY_SEMANTIC_BRANCHES = (
    "REAL_GOAL",
    "SEARCH_SUMMARY",
    "NO_DIRECT_MATCH",
    "PRIMARY",
    "ALTERNATIVE",
    "CANDIDATE",
    "METHODS",
    "SIMPLER_ALTERNATIVE",
    "RISKS",
    "FACTS",
    "SCHEMA",
)
_DISCOVERY_SEMANTIC_BRANCH_SET = frozenset(DISCOVERY_SEMANTIC_BRANCHES)

# This text is intentionally part of every consultation request.  It is never
# persisted in the report, so the canonical artifact does not become a prompt
# or transcript store.
ANTI_TUNNEL_RESEARCH_RULE = """Anti-Tunnel Research Rule (must be applied to this discovery):
- Start from the real, user-visible output goal rather than an attractive implementation route.
- Distinguish physical/real-world difficulties from representation or software difficulties.
- Delete unnecessary intermediate representations and actively look for a simpler method.
- Prefer maintaining good existing code when it already addresses the real goal.
- Use cheap falsification and small tests before committing to an expensive route.
- Do not preserve a route merely because of sunk cost.
- Respect scale, physical, operational, and other real-world constraints.
- Avoid needless complexity; do not select a production architecture automatically.
"""

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
        "conversation",
        "messages",
        "turns",
        "chat_history",
        "raw_history",
        # A confidence field would be pseudo precision in this workflow.
        "confidence",
        "confidence_score",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
# These checks run on the transient response text before any prose is
# discarded.  A response must not smuggle a local path in a prefix/suffix that
# would otherwise never reach the structured-field safety checks.  Keep the
# patterns local to this parser; bridge attachment validation remains its own
# boundary and is intentionally not changed here.
_RESPONSE_ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s\"'`),;}\]]*"),
    re.compile(r"\\\\[^\\/\s]+[\\/][^\\/\s\"'`),;}\]]*"),
    re.compile(r"\bfile://[^\s\"'`)]*", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9_:/])/(?![/\s])[^\\/\s\"'`),;}\]]*"),
)
# The consultant may echo a local path despite the prompt contract.  The
# strict parser below must continue rejecting such text when called directly;
# only the real consultation adapter may replace an already-detected local
# path with this bounded, non-location-bearing representation.
LOCAL_PATH_PLACEHOLDER = "<LOCAL_PATH>"
_RESPONSE_PATH_MATCH_KINDS = ("windows", "unc", "file-url", "posix")
_RESPONSE_PATH_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_RESPONSE_HTTP_URL_PATTERN = re.compile(r"\bhttps?://[^\s\"'`),;}\]]+", re.IGNORECASE)
_RESPONSE_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?:^|[\"']?)(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd|secret|cookie|cookies|token|session(?:[_-]?id)?)"
    r"(?:[\"']?\s*[:=])",
    re.IGNORECASE,
)
_RESPONSE_CODE_LINE_PATTERN = re.compile(
    r"(?im)^\s*(?:#!|//|/\*|<script\b|<\?xml\b|"
    r"(?:import|from|def|class|function|const|let|var|return|print|raise|yield|async|await)\b|"
    r"[A-Za-z_]\w*\s*(?:=|:=|\+=|-=|\*=|/=)|"
    r"(?:if|for|while|with|try|except|elif)\b[^:\n]*:)"
)
# A fence is recognized only when the complete line is a Markdown fence
# marker.  Inline backticks, four-or-more backticks, and info strings other
# than the explicit ``json`` label are deliberately not part of this grammar.
# The parser below uses the line offsets rather than a regex over the body so
# prose before/after a fence can be handled without ever joining it to JSON.
_JSON_FENCE_LINE_RE = re.compile(r"[ \t]{0,3}```(?P<info>[^`]*)[ \t]*\Z")
_REPO_SCHEME = frozenset({"http", "https", "ssh", "git", "file", "git+http", "git+https", "git+ssh"})
_FAILURE_STATUS = frozenset({"FAILED", "FAILURE", "ERROR", "UNVERIFIED", "RATE_LIMITED", "BLOCKED"})
_FAILURE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_.-]{0,127}$")
_EXCEPTION_CLASS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_ASSET_SCHEMA_PATH_PATTERN = re.compile(
    r"^\$(?:(?:\.[A-Za-z_][A-Za-z0-9_-]{0,63})|(?:\[[0-9]{1,4}\])){0,24}$"
)
_ASSET_FAILURE_FIELD_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\[\]-]{0,127}$")
_FAILURE_PHASES = frozenset(
    {
        "CONSULTANT_INVOCATION",
        "CONTEXT_PACK_BUILD",
        "BRIDGE_RUNNER",
        "BRIDGE_SUBPROCESS",
        "BRIDGE_ADAPTER",
        "BRIDGE_RESULT",
        "RESPONSE_VALIDATION",
    }
)
_GENERIC_FAILURE_CODES = frozenset({"CONSULTATION_FAILED", "BRIDGE_EXTERNAL_FAILURE"})


class ProjectDiscoveryError(RuntimeError):
    """A bounded, stable-code failure from Step 13."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        semantic_branch: str | None = None,
        details: Mapping[str, Any] | None = None,
        cause: BaseException | None = None,
        forensic: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = str(code)
        self.details = dict(details or {})
        self.cause = cause
        self.forensic = dict(forensic or {}) if isinstance(forensic, Mapping) else None
        if semantic_branch is not None:
            self.semantic_branch = _sanitize_semantic_branch(semantic_branch)
        super().__init__(message)


# Compatibility spellings used by small integrations and acceptance scripts.
DiscoveryError = ProjectDiscoveryError
ProjectDiscoveryFailure = ProjectDiscoveryError


class DiscoveryConsultant(Protocol):
    """Protocol for an explicitly supplied, one-shot consultation transport."""

    def consult(self, **kwargs: Any) -> Mapping[str, Any] | str: ...


class RepositoryVerifier(Protocol):
    """Protocol for local repository fact verification."""

    def verify(self, **kwargs: Any) -> Mapping[str, Any]: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sanitize_semantic_branch(value: Any) -> str:
    """Return only a known semantic branch token for failure diagnostics."""

    if isinstance(value, str) and value in _DISCOVERY_SEMANTIC_BRANCH_SET:
        return value
    return "SCHEMA"


def _sanitize_grammar_branch(value: Any) -> str:
    """Return only a de-identified grammar branch token."""

    if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]+", value):
        return value[:128]
    return "UNSPECIFIED"


def _stable_failure_code(value: Any, *, default: str | None = "CONSULTATION_FAILED") -> str | None:
    """Keep a failure code as a closed, non-message token.

    Exception messages and bridge output are intentionally not allowed to
    cross the persisted failure boundary.  A code is useful only when it is a
    bounded upper-case token; anything else is treated as an unknown failure.
    """

    if not isinstance(value, str):
        return default
    candidate = value.strip().upper()
    if not candidate or not _FAILURE_CODE_PATTERN.fullmatch(candidate):
        return default
    if candidate.casefold().replace("-", "_") in _SENSITIVE_KEYS:
        return default
    return candidate


def _bounded_asset_diagnostic(value: Any, pattern: re.Pattern[str]) -> str | None:
    """Keep only a closed, non-secret asset diagnostic path token."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if pattern.fullmatch(candidate) else None


def _asset_error_details(error: BaseException) -> dict[str, str]:
    """Extract bounded asset-layer diagnostics without retaining messages."""

    details: dict[str, str] = {}
    for item in _exception_chain(error):
        if "asset_error_code" not in details:
            code = _stable_failure_code(getattr(item, "code", None), default=None)
            if code is not None:
                details["asset_error_code"] = code
        raw_details = getattr(item, "details", None)
        candidates: Mapping[str, Any] = raw_details if isinstance(raw_details, Mapping) else {}
        if "schema_path" not in details:
            schema_path = _bounded_asset_diagnostic(
                getattr(item, "schema_path", candidates.get("schema_path")),
                _ASSET_SCHEMA_PATH_PATTERN,
            )
            if schema_path is None and type(item).__name__ == "ContractValidationError":
                # The local validator's exception format is ``<path>: ...``.
                # Only retain the validated path prefix, never its message.
                schema_path = _bounded_asset_diagnostic(str(item).split(":", 1)[0].strip(), _ASSET_SCHEMA_PATH_PATTERN)
            if schema_path is not None:
                details["schema_path"] = schema_path
        if "failure_field" not in details:
            failure_field = _bounded_asset_diagnostic(
                getattr(item, "failure_field", candidates.get("failure_field")),
                _ASSET_FAILURE_FIELD_PATTERN,
            )
            if failure_field is not None:
                details["failure_field"] = failure_field
        if "asset_error_code" in details and "schema_path" in details and "failure_field" in details:
            break
    return details


def _stable_exception_class(value: Any) -> str:
    """Return only a bounded exception class name, never its message."""

    candidate = value if isinstance(value, str) else ""
    if _EXCEPTION_CLASS_PATTERN.fullmatch(candidate):
        return candidate
    return "UnknownException"


def _known_request_count(value: Any) -> int | None:
    """Return a request count only when the transport actually established it."""

    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value in {0, 1} else None


def _exception_chain(error: BaseException) -> list[BaseException]:
    """Walk explicit and implicit causes without retaining exception text."""

    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        explicit = getattr(current, "cause", None)
        implicit = getattr(current, "__cause__", None)
        current = explicit if isinstance(explicit, BaseException) else implicit
    return chain


def _phase_for_exception(error: BaseException, *, default: str = "CONSULTANT_INVOCATION") -> str:
    """Classify the failed boundary using fixed phase labels only."""

    for item in _exception_chain(error):
        class_name = type(item).__name__
        code = _stable_failure_code(getattr(item, "code", None), default=None) or ""
        detail_phase = getattr(item, "details", None)
        if isinstance(detail_phase, Mapping):
            candidate = detail_phase.get("failure_phase")
            if isinstance(candidate, str) and candidate in _FAILURE_PHASES:
                return candidate
        if class_name == "StageIntegrationError":
            if code.startswith(("BRIDGE_RESULT", "BRIDGE_REQUEST", "BRIDGE_RECEIPT", "BRIDGE_RESPONSE", "BRIDGE_MODE", "PROJECT_SCOPE")):
                return "BRIDGE_RESULT"
            return "BRIDGE_SUBPROCESS"
        if class_name == "BridgeEnvelopeError":
            if code.startswith("CONTEXT_PACK"):
                return "CONTEXT_PACK_BUILD"
            return "BRIDGE_ADAPTER"
    return default if default in _FAILURE_PHASES else "CONSULTANT_INVOCATION"


def _forensic_for_exception(
    error: BaseException,
    *,
    phase: str | None = None,
    request_count: Any = None,
    error_code: Any = None,
    attempt_count: Any = 1,
) -> dict[str, Any]:
    """Build the small, redacted failure record shared by discovery paths."""

    chain = _exception_chain(error)
    code = _stable_failure_code(error_code, default=None)
    if code is None:
        outer_code = _stable_failure_code(getattr(error, "code", None), default=None)
        code = outer_code
        if code in _GENERIC_FAILURE_CODES or code is None:
            for item in chain[1:]:
                candidate = _stable_failure_code(getattr(item, "code", None), default=None)
                if candidate is not None and candidate not in _GENERIC_FAILURE_CODES:
                    code = candidate
                    break
    code = code or "CONSULTATION_FAILED"

    count = _known_request_count(request_count)
    if count is None:
        for item in chain:
            details = getattr(item, "details", None)
            if isinstance(details, Mapping):
                count = _known_request_count(details.get("request_count", details.get("requestCount")))
                if count is not None:
                    break

    class_name = _stable_exception_class(type(chain[-1]).__name__ if chain else type(error).__name__)
    phase_value = phase if isinstance(phase, str) and phase in _FAILURE_PHASES else _phase_for_exception(error)
    attempts = attempt_count if isinstance(attempt_count, int) and not isinstance(attempt_count, bool) else 1
    if attempts < 1 or attempts > 1:
        attempts = 1
    return {
        "failure_phase": phase_value,
        "exception_class": class_name,
        "error_code": code,
        "request_count": count,
        "attempt_count": attempts,
    }


def _semantic_branch_for(field: str) -> str:
    """Map an internal field/path to a fixed, de-identified branch token."""

    normalized = str(field).casefold().replace("-", "_")
    # Candidate paths contain arbitrary candidate metadata names; classify
    # them before looking at generic repository/url wording.
    if any(token in normalized for token in ("candidate_repositories", "repositories", "candidates", "repo_url")):
        return "CANDIDATE"
    if "real_goal" in normalized or normalized.endswith(".goal") or normalized == "goal":
        return "REAL_GOAL"
    if "search_summary" in normalized or normalized.endswith(".summary"):
        return "SEARCH_SUMMARY"
    if "no_direct_match" in normalized or "no_match" in normalized:
        return "NO_DIRECT_MATCH"
    if "primary" in normalized or "selected_route" in normalized or "selected_repository" in normalized:
        return "PRIMARY"
    if "simpler" in normalized:
        return "SIMPLER_ALTERNATIVE"
    if "alternative" in normalized:
        return "ALTERNATIVE"
    if "non_repo_method" in normalized or "method" in normalized:
        return "METHODS"
    if "risk" in normalized:
        return "RISKS"
    if "fact" in normalized or "local_verification_needed" in normalized:
        return "FACTS"
    return "SCHEMA"


def _safe_text(value: Any, field: str, *, required: bool = False, max_length: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise ProjectDiscoveryError(
            "DISCOVERY_INPUT_INVALID",
            f"{field} must be text",
            semantic_branch=_semantic_branch_for(field),
        )
    result = value.strip()
    if required and not result:
        raise ProjectDiscoveryError(
            "DISCOVERY_INPUT_INVALID",
            f"{field} must be non-empty",
            semantic_branch=_semantic_branch_for(field),
        )
    if "\x00" in result or len(result) > max_length:
        raise ProjectDiscoveryError(
            "DISCOVERY_INPUT_INVALID",
            f"{field} is outside bounded limits",
            semantic_branch=_semantic_branch_for(field),
        )
    return result


def _assert_safe(value: Any, *, path: str = "$", depth: int = 0) -> None:
    """Reject secrets, transcripts and pseudo-confidence before persistence."""

    if depth > 12:
        raise ProjectDiscoveryError("DISCOVERY_INPUT_TOO_DEEP", "discovery data is too deeply nested")
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).strip().lower().replace("-", "_")
            if key_text in _SENSITIVE_KEYS:
                raise ProjectDiscoveryError("DISCOVERY_SECRET_REJECTED", f"forbidden field at {path}")
            _assert_safe(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > 256:
            raise ProjectDiscoveryError("DISCOVERY_INPUT_TOO_LARGE", f"too many values at {path}")
        for index, child in enumerate(value):
            _assert_safe(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str):
        if "\x00" in value or len(value) > MAX_TEXT_LENGTH:
            raise ProjectDiscoveryError(
                "DISCOVERY_INPUT_INVALID",
                f"unsafe text at {path}",
                semantic_branch=_semantic_branch_for(path),
            )
        if any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise ProjectDiscoveryError("DISCOVERY_SECRET_REJECTED", f"secret-like text at {path}")
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise ProjectDiscoveryError(
        "DISCOVERY_INPUT_INVALID",
        f"unsupported value at {path}",
        semantic_branch=_semantic_branch_for(path),
    )


def sanitize_project_url(value: str | os.PathLike[str]) -> str:
    """Validate one configured target without guessing its UI route shape.

    The generic bridge adapter owns the minimum origin/ambiguity check. The
    browser performs the actual Project-page reachability and scope proof.
    """

    try:
        return normalize_bridge_project_url(value)
    except BridgeEnvelopeError as exc:
        raise ProjectDiscoveryError(exc.code, str(exc)) from exc


def canonicalize_repo_url(value: str | os.PathLike[str]) -> str:
    """Return a stable, credential-free identity for a repository URL."""

    try:
        raw = os.fspath(value).strip()
    except (TypeError, AttributeError):
        raise ProjectDiscoveryError("REPO_URL_INVALID", "repository URL must be text")
    if not raw or "\x00" in raw or any(character.isspace() for character in raw):
        raise ProjectDiscoveryError("REPO_URL_INVALID", "repository URL is not safe")
    # Accept the common SCP form while converting it to a URL identity.
    scp_match = re.match(r"^[^/@\\s]+@([^:/\\s]+):(.+)$", raw)
    if scp_match:
        raw = f"https://{scp_match.group(1)}/{scp_match.group(2)}"
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ProjectDiscoveryError("REPO_URL_INVALID", "repository URL syntax is invalid") from exc
    scheme = parsed.scheme.casefold()
    if scheme not in _REPO_SCHEME or not hostname:
        raise ProjectDiscoveryError("REPO_URL_INVALID", "repository URL must have a supported scheme and host")
    if parsed.username is not None or parsed.password is not None:
        raise ProjectDiscoveryError("REPO_URL_INVALID", "repository URL cannot contain credentials")
    if scheme.startswith("git+"):
        scheme = scheme[4:]
    host = hostname.casefold()
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path.replace("\\", "/")
    path = "/" + "/".join(part for part in path.split("/") if part)
    if path.casefold().endswith(".git"):
        path = path[:-4]
    if path == "/":
        raise ProjectDiscoveryError("REPO_URL_INVALID", "repository URL must identify a repository")
    # Query and fragment are not part of a repository identity and may carry
    # access material, so do not retain them.
    return urlunsplit((scheme, netloc, path, "", ""))


def _repo_url_for_git(canonical: str) -> str:
    """Use the original URL for transport while retaining canonical identity."""

    return canonical


def _project_url_from_brief(brief: Mapping[str, Any]) -> str:
    nested = brief.get("brief") if isinstance(brief.get("brief"), Mapping) else {}
    values: list[Any] = []
    if isinstance(nested, Mapping):
        values.append(nested.get("chatgpt_project_url"))
        binding = nested.get("chatgpt_project_binding")
        if isinstance(binding, Mapping):
            values.append(binding.get("url"))
    # A few Step 12-compatible callers carry extension fields at document
    # root.  This fallback is explicit; it never silently derives a root URL.
    values.append(brief.get("chatgpt_project_url"))
    binding = brief.get("chatgpt_project_binding")
    if isinstance(binding, Mapping):
        values.append(binding.get("url"))
    for value in values:
        if isinstance(value, str) and value.strip():
            return sanitize_project_url(value)
    raise ProjectDiscoveryError(
        "PROJECT_URL_REQUIRED",
        "approved project brief must bind chatgpt_project_url or chatgpt_project_binding.url",
    )


def _unwrap_brief(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Accept either the canonical brief or an intake result wrapper."""

    nested = value.get("project_brief")
    if isinstance(nested, Mapping):
        return nested
    return value


def _brief_view(brief: Mapping[str, Any]) -> dict[str, Any]:
    nested = brief.get("brief") if isinstance(brief.get("brief"), Mapping) else {}
    allowed = (
        "title",
        "problem_statement",
        "goal",
        "desired_outcome",
        "priority",
        "success_criteria",
        "acceptance_criteria",
        "constraints",
        "non_goals",
        "scope",
        "stakeholders",
        "preferences",
    )
    result: dict[str, Any] = {}
    for key in allowed:
        if key in nested:
            result[key] = copy.deepcopy(nested[key])
    # A brief with a goal is expected after approval.  Keep a small fallback
    # for old, Step 11-compatible documents that used rough_requirement.
    if not result.get("goal") and isinstance(brief.get("rough_requirement"), str):
        result["goal"] = brief["rough_requirement"].strip()
    return result


def _real_goal(brief: Mapping[str, Any]) -> str:
    view = _brief_view(brief)
    for key in ("desired_outcome", "goal", "problem_statement"):
        value = view.get(key)
        if isinstance(value, str) and value.strip():
            return _safe_text(value, f"brief.{key}", required=True)
    raise ProjectDiscoveryError("REAL_GOAL_REQUIRED", "approved project brief has no usable real goal")


def _local_project_evidence(root: Path) -> dict[str, Any]:
    """Collect bounded, read-only evidence from the business repository."""

    try:
        entries = sorted(
            item.name
            for item in root.iterdir()
            if item.name not in {".research", ".git"}
        )[:MAX_TOP_LEVEL_ENTRIES]
    except OSError as exc:
        raise ProjectDiscoveryError("LOCAL_EVIDENCE_UNREADABLE", "project directory cannot be inspected") from exc
    markers = {
        "README.md": "readme",
        "README.rst": "readme",
        "pyproject.toml": "python",
        "requirements.txt": "python",
        "package.json": "node",
        "Cargo.toml": "rust",
        "go.mod": "go",
        "pom.xml": "java",
    }
    detected = sorted({label for filename, label in markers.items() if (root / filename).exists()})
    head: str | None = None
    git_present = (root / ".git").exists()
    if git_present:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=MAX_GIT_TIMEOUT_SECONDS,
                check=False,
            )
            if result.returncode == 0:
                candidate = (result.stdout or "").strip()
                if re.fullmatch(r"[0-9a-fA-F]{40}", candidate):
                    head = candidate.lower()
        except (OSError, subprocess.SubprocessError):
            # Local evidence remains useful without a HEAD; do not make a
            # read-only optional probe turn the whole brief into a write.
            head = None
    return {
        "checked_read_only": True,
        "repository_name": root.name,
        "has_git_metadata": git_present,
        "top_level_entries": entries,
        "detected_project_markers": detected,
        "local_head": head,
    }


def _context_evidence(root: Path) -> dict[str, Any]:
    path = root / ".research" / "PROJECT_CONTEXT.md"
    if not path.exists():
        return {"present": False, "path": PROJECT_CONTEXT_RELATIVE_PATH.as_posix()}
    try:
        loaded = load_project_context(root)
    except (ProjectContextError, ArtifactRetentionError, OSError, UnicodeError) as exc:
        raise ProjectDiscoveryError("PROJECT_CONTEXT_INVALID", "existing project context failed verification") from exc
    markdown = loaded.get("markdown", "")
    if not isinstance(markdown, str) or len(markdown.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise ProjectDiscoveryError("PROJECT_CONTEXT_TOO_LARGE", "project context exceeds discovery bounds")
    # Keep the verified context in the bounded packet.  A valid Step 12
    # context is already secret/transcript filtered; the final safety check
    # below still protects this layer if that contract changes later.
    return {
        "present": True,
        "path": PROJECT_CONTEXT_RELATIVE_PATH.as_posix(),
        "context_digest": str(loaded.get("context_digest", "")),
        "brief_digest": str(loaded.get("brief_digest", "")),
        "resource_count": int(loaded.get("context_resource_count", 0)),
        "markdown": markdown[:MAX_TEXT_LENGTH],
        "markdown_truncated": len(markdown) > MAX_TEXT_LENGTH,
    }


def build_discovery_evidence(
    project_root: str | os.PathLike[str] = ".",
    *,
    brief: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    """Build the bounded local evidence pack and its content digest."""

    root = resolve_project_root(project_root)
    if brief is None:
        try:
            brief = load_project_brief(root)
        except ProjectStateError as exc:
            raise ProjectDiscoveryError("BRIEF_UNREADABLE", "canonical project brief could not be loaded") from exc
    if not isinstance(brief, Mapping):
        raise ProjectDiscoveryError("BRIEF_NOT_FOUND", "an approved project brief is required")
    brief = _unwrap_brief(brief)
    try:
        checked = validate_project_brief(brief, root)
    except ProjectIntakeError as exc:
        raise ProjectDiscoveryError("BRIEF_INVALID", "canonical project brief failed validation") from exc
    if checked.get("state") != "APPROVED" or checked.get("status") != "APPROVED":
        raise ProjectDiscoveryError("BRIEF_NOT_APPROVED", "Project Discovery requires state=APPROVED")
    # URL validation happens before a consultation but is not included in the
    # evidence digest; changing only a binding must still require a new brief
    # revision rather than silently mixing conversations.
    _project_url_from_brief(checked)
    # Build the canonical asset inventory before constructing the consultant
    # packet.  This is intentionally metadata-only: no source tree is copied
    # and no local project file is selected as a solution automatically.
    try:
        collect_available_assets(root, brief=checked)
        available_assets = load_available_assets(root)
        asset_pack = build_asset_pack(root, brief=checked, purpose="discovery")
        local_profile = load_local_project_profile(root)
    except AssetLayerError as exc:
        # Keep only the bounded asset code/path diagnostics.  The underlying
        # exception text can contain local values and must remain transient.
        raise ProjectDiscoveryError(
            "ASSET_LAYER_INVALID",
            "asset layer validation failed",
            details=_asset_error_details(exc),
            cause=exc,
        ) from exc
    if not isinstance(available_assets, Mapping):
        raise ProjectDiscoveryError("ASSET_LAYER_INVALID", "canonical available assets are missing")
    profile_evidence: dict[str, Any] = {"present": False, "path": LOCAL_PROJECT_PROFILE_RELATIVE_PATH.as_posix()}
    if isinstance(local_profile, Mapping):
        markdown = local_profile.get("markdown", "")
        if not isinstance(markdown, str):
            raise ProjectDiscoveryError("LOCAL_PROFILE_INVALID", "local project profile is not text")
        profile_evidence = {
            "present": True,
            "path": LOCAL_PROJECT_PROFILE_RELATIVE_PATH.as_posix(),
            "profile_digest": str(local_profile.get("profile_digest", "")),
            # The full profile remains local; only a compact excerpt enters
            # the bounded consultation packet.
            "markdown": markdown[:MAX_TEXT_LENGTH],
            "markdown_truncated": len(markdown) > MAX_TEXT_LENGTH,
        }
    evidence = {
        "schema_version": "discovery_evidence.v1",
        "project_id": str(checked["project_id"]),
        "brief_digest": sha256_json(checked),
        "brief_revision": int(checked.get("revision", 1)),
        "real_goal": _real_goal(checked),
        "brief": _brief_view(checked),
        "project_context": _context_evidence(root),
        "local_project": _local_project_evidence(root),
        "available_assets": copy.deepcopy(dict(available_assets)),
        "local_project_profile": profile_evidence,
        "asset_pack": copy.deepcopy(asset_pack),
    }
    _assert_safe(evidence)
    # External candidate records are a post-consultation enrichment of the
    # canonical inventory.  They must not make an identical Discovery input
    # look new on a second invocation (which would permit an accidental second
    # GPT request).  Local requirement/context/profile facts remain part of the
    # digest and still invalidate it when they change.
    digest_evidence = copy.deepcopy(evidence)
    digest_assets = digest_evidence.get("available_assets")
    if isinstance(digest_assets, Mapping):
        stable_assets = [
            item for item in digest_assets.get("assets", [])
            if not isinstance(item, Mapping) or item.get("kind") != "external_repository"
        ]
        digest_assets["assets"] = stable_assets
        # Candidate relationships and rejected-candidate records are written
        # after the consultant returns.  They are useful canonical
        # provenance, but are not input evidence for a second FRESH
        # Discovery request.  Leaving them in this digest would make a local
        # Candidate 0 + verified external candidate appear to be new evidence
        # solely because the previous consultation enriched the inventory.
        for key in (
            "assets_digest",
            "candidate_relationships",
            "created_at",
            "digest",
            "rejected_candidates",
            "updated_at",
            "revision",
        ):
            digest_assets.pop(key, None)
    digest_pack = digest_evidence.get("asset_pack")
    if isinstance(digest_pack, Mapping):
        stable_pack_assets = [
            item for item in digest_pack.get("assets", [])
            if not isinstance(item, Mapping) or item.get("kind") != "external_repository"
        ]
        digest_pack["assets"] = stable_pack_assets
        digest_pack["asset_ids"] = [item.get("asset_id") for item in stable_pack_assets if isinstance(item, Mapping)]
        # The pack is rebuilt from the canonical inventory after a successful
        # consultation. That enrichment changes the pack id and the on-disk
        # AVAILABLE_ASSETS resource digest even though the input evidence for
        # a second FRESH request is unchanged. Keep those provenance fields
        # out of the duplicate guard projection; the stable asset records
        # above remain the actual input signal.
        for key in ("available_assets_digest", "pack_digest", "pack_id"):
            digest_pack.pop(key, None)
        resource_id = "resource-available-assets-json"
        resource_ids = digest_pack.get("resource_ids")
        if isinstance(resource_ids, list):
            digest_pack["resource_ids"] = [item for item in resource_ids if item != resource_id]
        resource_digests = digest_pack.get("resource_digests")
        if isinstance(resource_digests, Mapping):
            resource_digests.pop(resource_id, None)
        resource_provenance = digest_pack.get("resource_provenance")
        if isinstance(resource_provenance, list):
            digest_pack["resource_provenance"] = [
                item
                for item in resource_provenance
                if not isinstance(item, Mapping) or item.get("resource_id") != resource_id
            ]
    return evidence, sha256_json(digest_evidence)


def build_approved_discovery_packet(
    project_root: str | os.PathLike[str] = ".",
    *,
    brief: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the bounded, approval-bound packet sent to a consultant.

    The packet is intentionally detached from the caller and contains only
    local brief/context evidence plus its digest.  It does not invoke a
    consultant.  This small public helper makes the preflight boundary easy
    to test and for integrations to inspect before deciding to spend their
    single FRESH request.
    """

    evidence, digest = build_discovery_evidence(project_root, brief=brief)
    return {"evidence": copy.deepcopy(evidence), "evidence_digest": digest, "mode": CONSULTATION_MODE_FRESH}


# Short aliases used by thin callers and acceptance fixtures.
build_approved_packet = build_approved_discovery_packet
prepare_discovery_packet = build_approved_discovery_packet


def _prompt_for(evidence: Mapping[str, Any]) -> str:
    # This bounded prompt is constructed only for the supplied transport.  It
    # intentionally has no credentials, browser state, or prior response.
    # Keep the Dialogue Policy/RFC 8259 output contract strict.  The parser has
    # one separate, finite compatibility recovery for literal LF/CR/TAB inside
    # JSON strings only; that recovery is not a license to emit prose, repair
    # structure, or broaden the consultation policy.
    goal = _safe_text(evidence.get("real_goal"), "real_goal", required=True)
    brief = canonical_json(evidence.get("brief", {}))
    asset_pack = canonical_json(evidence.get("asset_pack", {}))
    return (
        "Perform one bounded FRESH Project Discovery consultation.\n"
        f"{method_evidence_policy_text(context='FRESH Project Discovery')}\n"
        f"Real user-visible goal: {goal}\n"
        f"Approved brief summary: {brief}\n"
        f"Bounded available asset pack (metadata only): {asset_pack}\n"
        f"Local evidence digest: {sha256_json(dict(evidence))}\n\n"
        "The final response must be exactly one RFC 8259-valid JSON object. It must "
        "contain only the keys in the schema below: no unknown fields, no Markdown "
        "fence, no leading or trailing prose, no comments, or other text. Do not output "
        "multiple objects, an array, a scalar, or code. Every JSON string must use "
        "JSON escapes for newline, tab, and every other control character (for "
        "example \\n, \\r, \\t, or \\u00XX); never emit a literal control character. "
        "Escape embedded double quotes and backslashes according to JSON string "
        "syntax.\n\n"
        "Path-safe output contract (applies to every output field, including "
        "nested objects and arrays): never emit a local absolute path or relative "
        "file path. This includes Windows drive paths, UNC paths, POSIX paths "
        "such as /home/ or /tmp/, ./ or ../ relative paths, and any other local "
        "file reference. Never repeat attachment paths, the repository root, a "
        "profile path, workspace paths, log paths, prompt filenames, or attachment "
        "filenames. If a local path must be described, use exactly the fixed "
        "placeholder <LOCAL_PATH>; use exactly <LOCAL_REPOSITORY_ROOT> for a "
        "repository-root label. These placeholders are labels, not paths.\n\n"
        "Keep the output compact: target <=10000 chars and compress summaries "
        "when necessary rather than adding explanation. The complete semantic "
        "schema and all required fields are still mandatory; never omit required "
        "fields to meet the compactness target. The hard whole-response "
        "bound is 16,000 characters; the field, array, evidence-pack, and report "
        "limits below remain in force.\n\n"
        "Output schema (canonical keys only):\n"
        "- real_goal: non-empty string, maximum 12,000 characters.\n"
        "- search_summary: non-empty string, including when no_direct_match_found "
        "is true; maximum 12,000 characters.\n"
        "- no_direct_match_found: boolean (true or false, never a string). If false, "
        "primary_recommendation and why_primary must both be non-empty. If true, "
        "primary_recommendation may be null and why_primary may be an empty string.\n"
        "- primary_recommendation: null, a string, or an object. For an object, use "
        "a repository-style object only when it has a repo_url that can be "
        "canonicalized, or a bounded recommendation object; do not invent a URL. "
        "A local Candidate 0 recommendation may use only asset_id exactly "
        "candidate-user-project and a bounded summary; do not add a local filename, "
        "path, profile, workspace, repository-root, or clone-path field.\n"
        "- why_primary: string, maximum 12,000 characters; it must be non-empty when "
        "no_direct_match_found is false and may be empty (or explanatory text) when "
        "it is true.\n"
        "- candidate_repositories: an array (use [] when empty), with at most five "
        "objects. Each external-repository object must have a canonical HTTPS "
        "repo_url (https://... only) that can be canonicalized; never use an SSH, "
        "git, SCP, file URL, local path, or clone path. The same canonical HTTPS "
        "repo_url-only rule applies to external repositories in primary_recommendation "
        "or alternatives; optional metadata keys name, apparent_match, reusable_part, "
        "expected_modification, license, maintenance, and reason_to_consider are "
        "strings of at most 4,000 characters.\n"
        "- repositories/candidates are not alternate shapes: use the canonical "
        "candidate_repositories array and never more than five entries.\n"
        "- alternatives: an array (use [] when empty), with at most four (4) strings or "
        "objects; do not use null or a scalar.\n"
        "- relevant_non_repo_methods: an array of strings (each at most 4,000 "
        "characters), at most eight (8) entries (use [] when empty).\n"
        "- simpler_alternative: string of at most 12,000 characters (use an empty "
        "string when there is no bounded simpler route).\n"
        "- important_risks: an array of strings (each at most 4,000 characters), at "
        "most sixteen (16) entries (use [] when empty).\n"
        "- facts_needing_local_verification: an array of strings (each at most 4,000 "
        "characters), at most sixteen (16) entries (use [] when empty).\n"
        "Use null only for primary_recommendation and [] for empty arrays; do not "
        "emit {} as a substitute for a missing array or string. Do not include "
        "confidence scores or other unknown fields. Repository and web claims are "
        "unverified claims, not established local facts; list claims needing local "
        "verification rather than asserting them as facts. Never output absolute "
        "paths, credentials, secrets, browser state, or transcript content.\n\n"
        "Minimal valid no-direct-match example (the response must still be JSON, not "
        "prose):\n"
        '{"real_goal":"find the real user-visible outcome",'
        '"search_summary":"No direct match was found in the bounded search",'
        '"no_direct_match_found":true,"primary_recommendation":null,"why_primary":"",'
        '"candidate_repositories":[],"alternatives":[],"relevant_non_repo_methods":[],'
        '"simpler_alternative":"","important_risks":[],"facts_needing_local_verification":[]}\n\n'
        f"{ANTI_TUNNEL_RESEARCH_RULE}"
    )


def build_discovery_prompt(
    project_root: str | os.PathLike[str] = ".",
    *,
    brief: Mapping[str, Any] | None = None,
) -> str:
    """Build the Anti-Tunnel prompt after the approved-brief preflight."""

    evidence, _ = build_discovery_evidence(project_root, brief=brief)
    return _prompt_for(evidence)


def _signature_call(target: Callable[..., Any], values: Mapping[str, Any]) -> Any:
    """Call an injected object once, adapting common small test transports."""

    payload = dict(values)
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return target(
            project_url=payload["project_url"],
            mode=payload["mode"],
            prompt=payload["prompt"],
        )
    parameters = list(signature.parameters.values())
    has_var_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
    aliases: dict[str, Any] = {
        "project_url": payload["project_url"],
        "url": payload["project_url"],
        "mode": payload["mode"],
        "consultation_mode": payload["mode"],
        "prompt": payload["prompt"],
        "context": payload["evidence"],
        "evidence": payload["evidence"],
        "brief": payload["brief"],
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
            if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                positional.append(aliases[parameter.name])
            else:
                kwargs[parameter.name] = aliases[parameter.name]
        elif parameter.default is inspect.Parameter.empty:
            # A one-argument ``consult(request)`` fake is common.  Pass the
            # bounded request object rather than inventing an extra value.
            if len(parameters) == 1:
                if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                    positional.append(payload["request"])
                else:
                    kwargs[parameter.name] = payload["request"]
            else:
                raise ProjectDiscoveryError("CONSULTANT_SIGNATURE_INVALID", "consultant has an unsupported required parameter")
    return target(*positional, **kwargs)


def _call_consultant(
    consultant: Any,
    *,
    project_url: str,
    evidence: Mapping[str, Any],
    brief: Mapping[str, Any],
) -> Any:
    if consultant is None:
        raise ProjectDiscoveryError("CONSULTANT_REQUIRED", "an explicit one-shot discovery consultant is required")
    target = getattr(consultant, "consult", consultant)
    if not callable(target):
        raise ProjectDiscoveryError("CONSULTANT_INVALID", "consultant must be callable or expose consult()")
    prompt = _prompt_for(evidence)
    request = {
        "project_url": project_url,
        "mode": CONSULTATION_MODE_FRESH,
        "prompt": prompt,
        "brief": copy.deepcopy(_brief_view(brief)),
        "evidence_digest": sha256_json(dict(evidence)),
    }
    values = {
        "project_url": project_url,
        "mode": CONSULTATION_MODE_FRESH,
        "prompt": prompt,
        "evidence": copy.deepcopy(dict(evidence)),
        "brief": copy.deepcopy(_brief_view(brief)),
        "request": request,
    }
    try:
        # Exactly one invocation.  TypeError is not retried because retrying
        # could duplicate a real bridge request.
        result = _signature_call(target, values)
        if is_bridge_envelope(result):
            try:
                # A real bridge response must carry a complete receipt bound to
                # the same approved Project URL.  Offline consultants continue
                # to return raw structured discovery data and are not coerced
                # into this transport contract.
                return normalize_bridge_envelope(
                    result,
                    expected_project_url=project_url,
                    expected_mode=CONSULTATION_MODE_FRESH,
                    require_receipt=True,
                )
            except BridgeEnvelopeError as exc:
                raise ProjectDiscoveryError(
                    exc.code,
                    "bridge envelope was rejected",
                    details=exc.details,
                    cause=exc,
                ) from exc
        return result
    except ProjectDiscoveryError as exc:
        # Signature, envelope, and other adapter failures previously escaped
        # this boundary without any structured cause.  Attach only the fixed
        # forensic fields; the exception message remains transient.
        if not isinstance(getattr(exc, "forensic", None), Mapping):
            exc.forensic = _forensic_for_exception(exc)
        raise
    except Exception as exc:  # pragma: no cover - transport-specific errors
        name = type(exc).__name__.casefold()
        inner_code = _stable_failure_code(getattr(exc, "code", None), default=None)
        code = inner_code or ("CONSULTATION_RATE_LIMITED" if "rate" in name or "limit" in name else "CONSULTATION_FAILED")
        details = getattr(exc, "details", None)
        wrapped = ProjectDiscoveryError(
            code,
            "one-shot discovery consultation failed",
            details=details if isinstance(details, Mapping) else None,
            cause=exc,
        )
        wrapped.forensic = _forensic_for_exception(wrapped)
        raise wrapped from exc


def _string_list(value: Any, field: str, *, maximum: int) -> list[str]:
    if not isinstance(value, list):
        raise ProjectDiscoveryError(
            "DISCOVERY_RESPONSE_INVALID",
            f"{field} must be a list of text",
            semantic_branch=_semantic_branch_for(field),
        )
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ProjectDiscoveryError(
                "DISCOVERY_INPUT_INVALID",
                f"{field}[{index}] must be text",
                semantic_branch=_semantic_branch_for(field),
            )
        text = _safe_text(item, f"{field}[{index}]", max_length=4_000)
        if text and text not in result:
            result.append(text)
        if len(result) > maximum:
            raise ProjectDiscoveryError(
                "DISCOVERY_RESPONSE_TOO_LARGE",
                f"{field} exceeds its bound",
                semantic_branch=_semantic_branch_for(field),
            )
    return result


def _candidate_value(value: Any, field: str) -> Any:
    if isinstance(value, str):
        return _safe_text(value, field, max_length=4_000)
    raise ProjectDiscoveryError(
        "DISCOVERY_RESPONSE_INVALID",
        f"{field} has unsupported value",
        semantic_branch=_semantic_branch_for(field),
    )


def _candidate_from(raw: Any, index: int) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = {"repo_url": raw}
    if not isinstance(raw, Mapping):
        raise ProjectDiscoveryError(
            "DISCOVERY_RESPONSE_INVALID",
            f"candidate_repositories[{index}] must be an object or URL",
            semantic_branch="CANDIDATE",
        )
    _assert_safe(raw, path=f"candidate_repositories[{index}]")
    source = dict(raw)
    raw_url = source.get(
        "repo_url",
        source.get(
            "repository_url",
            source.get("github_url", source.get("url", source.get("repo"))),
        ),
    )
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ProjectDiscoveryError(
            "REPO_URL_REQUIRED",
            f"candidate_repositories[{index}] needs repo_url",
            semantic_branch="CANDIDATE",
        )
    try:
        canonical = canonicalize_repo_url(raw_url)
    except ProjectDiscoveryError as exc:
        if exc.code == "REPO_URL_INVALID":
            exc.semantic_branch = "CANDIDATE"
        raise
    candidate: dict[str, Any] = {"repo_url": canonical}
    aliases = {
        "name": ("name", "repository_name", "repo_name"),
        "apparent_match": ("apparent_match", "match", "match_summary"),
        "reusable_part": ("reusable_part", "reusable", "reusable_components"),
        "likely_gap": ("likely_gap", "gap", "limitations"),
        "expected_modification": ("expected_modification", "modification", "changes"),
        "license": ("license", "license_info"),
        "maintenance": ("maintenance", "maintenance_status"),
        "reason_to_consider": ("reason_to_consider", "reason", "why_consider"),
    }
    for field, names in aliases.items():
        chosen = next((source[name] for name in names if name in source), "")
        candidate[field] = _candidate_value(chosen, f"candidate_repositories[{index}].{field}")
    return candidate


def _raw_candidates(response: Mapping[str, Any]) -> list[Any]:
    value = response.get("candidate_repositories", response.get("repositories", response.get("candidates", [])))
    if not isinstance(value, list):
        raise ProjectDiscoveryError(
            "DISCOVERY_RESPONSE_INVALID",
            "candidate_repositories must be an array",
            semantic_branch="CANDIDATE",
        )
    return list(value)


def _dedupe_candidates(raw: Sequence[Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        candidate = _candidate_from(item, index)
        identity = candidate["repo_url"]
        if identity in seen:
            # Duplicate canonical repository identities are deliberately
            # collapsed before verification; no second clone or ls-remote.
            continue
        seen.add(identity)
        candidates.append(candidate)
        if len(candidates) > MAX_CANDIDATE_REPOSITORIES:
            raise ProjectDiscoveryError(
                "CANDIDATE_LIMIT",
                "at most five candidate repositories are allowed",
                semantic_branch="CANDIDATE",
            )
    return candidates


def _recommendation_value(value: Any, field: str) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        text = _safe_text(value, field, max_length=4_000)
        # Canonicalize repository-like recommendation strings when possible;
        # ordinary prose/non-repository methods remain unchanged.
        try:
            if "://" in text or re.match(r"^[^/@\\s]+@[^:/\\s]+:", text):
                return canonicalize_repo_url(text)
        except ProjectDiscoveryError:
            # A prose recommendation containing punctuation is not a repo URL.
            pass
        return text
    if isinstance(value, Mapping):
        _assert_safe(value, path=field)
        result = copy.deepcopy(dict(value))
        for key in ("repo_url", "repository_url", "github_url", "url", "repo"):
            if isinstance(result.get(key), str):
                result["repo_url"] = canonicalize_repo_url(result[key])
                if key != "repo_url":
                    result.pop(key, None)
                break
        return result
    raise ProjectDiscoveryError(
        "DISCOVERY_RESPONSE_INVALID",
        f"{field} must be text, object, or null",
        semantic_branch=_semantic_branch_for(field),
    )


def _recommendation_repo_identity(value: Any) -> str | None:
    if isinstance(value, str):
        try:
            return canonicalize_repo_url(value)
        except ProjectDiscoveryError:
            return None
    if isinstance(value, Mapping):
        for key in ("repo_url", "repository_url", "github_url", "url", "repo"):
            if isinstance(value.get(key), str):
                try:
                    return canonicalize_repo_url(value[key])
                except ProjectDiscoveryError:
                    return None
    return None


class _DuplicateResponseKey(ValueError):
    """Raised when strict response JSON repeats an object key."""


def _response_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateResponseKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_response_constant(value: str) -> Any:
    # ``json`` accepts NaN/Infinity by default even though they are not JSON.
    raise ValueError(f"non-JSON constant: {value}")


def _response_json_decoder() -> json.JSONDecoder:
    """Build the strict decoder used by the bounded response grammar."""

    return json.JSONDecoder(
        object_pairs_hook=_response_object_pairs,
        parse_constant=_reject_response_constant,
    )


def _response_invalid(branch: str) -> None:
    """Raise a de-identified, stable error for a response grammar failure.

    ``DISCOVERY_RESPONSE_INVALID`` is the compatibility-facing outer code.
    The branch is intentionally a closed-set token rather than a parser
    exception, key name, source location, or response excerpt.  This keeps a
    failed consultation receipt useful for diagnostics without turning it into
    a transcript or an accidental secret channel.
    """

    branch_text = branch if re.fullmatch(r"[A-Z0-9_]+", branch) else "UNSPECIFIED"
    error = ProjectDiscoveryError("DISCOVERY_RESPONSE_INVALID", f"response grammar rejected [{branch_text}]")
    # Keep the branch available to offline callers without changing the
    # established public error code or requiring a new exception type.
    error.grammar_branch = branch_text  # type: ignore[attr-defined]
    # Suppress decoder/scanner exception context as well as its message: a
    # traceback should not reveal a duplicate key, response excerpt, or other
    # transient consultation content through ``__context__``.
    raise error from None


def _response_text_precheck(response: str) -> None:
    """Check transient response text before parsing or discarding prose.

    This is intentionally a preflight over the complete text.  Prefix/suffix
    prose is allowed only after it passes this check, so an unsafe value cannot
    hide outside the object that will be persisted.
    """

    if "\x00" in response:
        _response_invalid("NUL")
    try:
        encoded = response.encode("utf-8")
    except UnicodeEncodeError as exc:
        _response_invalid("INVALID_UTF8")
    if len(response) > MAX_RESPONSE_TEXT_LENGTH or len(encoded) > MAX_RESPONSE_BYTES:
        _response_invalid("TOO_LARGE")
    if _RESPONSE_SENSITIVE_ASSIGNMENT_PATTERN.search(response):
        raise ProjectDiscoveryError("DISCOVERY_SECRET_REJECTED", "secret-like assignment in consultation response")
    if any(pattern.search(response) for pattern in _SECRET_PATTERNS):
        raise ProjectDiscoveryError("DISCOVERY_SECRET_REJECTED", "secret-like text in consultation response")
    if any(pattern.search(response) for pattern in _RESPONSE_ABSOLUTE_PATH_PATTERNS):
        _response_invalid("ABSOLUTE_PATH")


def _response_value_path_precheck(value: Any, *, path: str = "response", depth: int = 0) -> None:
    """Apply the path portion of the response precheck to object inputs.

    Offline callers may provide an already-decoded mapping, so there is no
    raw text pass in that case.  Walk those values before semantic
    normalization to keep the same local-path boundary for both transports.
    Secret keys/values remain owned by ``_assert_safe`` so their established
    ``DISCOVERY_SECRET_REJECTED`` behavior is preserved.
    """

    if depth > 12:
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _response_value_path_precheck(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _response_value_path_precheck(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str) and any(pattern.search(value) for pattern in _RESPONSE_ABSOLUTE_PATH_PATTERNS):
        _response_invalid("ABSOLUTE_PATH")


def _response_text_sha256(value: str) -> str:
    """Hash transient response text without retaining or exposing its value."""

    # Strict parsing still rejects unpaired surrogates.  ``errors=replace``
    # keeps this diagnostic helper total while ensuring its output is always a
    # bounded digest rather than an exception message containing response text.
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _response_path_match_records(value: str) -> list[tuple[int, int, str, str]]:
    """Return ordered, non-overlapping absolute-path matches.

    The patterns are deliberately the exact strict-parser patterns above.  A
    match is classified by pattern index and carries its transient value only
    until the caller computes its digest/replacement; no raw path crosses the
    telemetry boundary.
    """

    candidates: list[tuple[int, int, int, str]] = []
    for pattern_index, pattern in enumerate(_RESPONSE_ABSOLUTE_PATH_PATTERNS):
        kind = _RESPONSE_PATH_MATCH_KINDS[pattern_index]
        for match in pattern.finditer(value):
            candidates.append((match.start(), match.end(), pattern_index, kind))
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    selected: list[tuple[int, int, str, str]] = []
    occupied_end = -1
    for start, end, _pattern_index, kind in candidates:
        if start < occupied_end:
            continue
        selected.append((start, end, kind, value[start:end]))
        occupied_end = end
    return selected


def _response_http_url_spans(value: str) -> list[tuple[int, int]]:
    """Return validated HTTP(S) URL spans that are not local paths."""

    spans: list[tuple[int, int]] = []
    for match in _RESPONSE_HTTP_URL_PATTERN.finditer(value):
        candidate = match.group(0)
        try:
            parsed = urlsplit(candidate)
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError:
            continue
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc or not hostname:
            continue
        # Credentials are not a legal safe URL representation for this
        # boundary; do not exempt a path inside one from strict checks.
        if parsed.username is not None or parsed.password is not None:
            continue
        spans.append((match.start(), match.end()))
    return spans


def _response_sanitization_match_records(value: str) -> list[tuple[int, int, str, str]]:
    """Expand strict matcher hits to one local-path token for sanitization.

    The strict matcher intentionally reports the first slash-delimited segment
    (for example ``/tmp``) and must remain unchanged.  The adapter may safely
    extend that already-detected hit across subsequent path separators until a
    response delimiter, preventing escaped JSON paths from leaving a suffix
    such as ``\\README.md`` behind after replacement.
    """

    candidates: list[tuple[int, int, int, str]] = []
    http_url_spans = _response_http_url_spans(value)
    strict_records = _response_path_match_records(value)
    for order, (start, end, kind, _raw) in enumerate(strict_records):
        if any(url_start <= start and end <= url_end for url_start, url_end in http_url_spans):
            continue
        expanded_end = end
        while expanded_end < len(value):
            character = value[expanded_end]
            if character.isspace() or character in "\"'`),;}]":
                break
            expanded_end += 1
        candidates.append((start, expanded_end, order, kind))
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0]), item[2]))
    selected: list[tuple[int, int, str, str]] = []
    occupied_end = -1
    for start, end, _order, kind in candidates:
        if start < occupied_end:
            continue
        selected.append((start, end, kind, value[start:end]))
        occupied_end = end
    return selected


def _response_string_token_spans(value: str) -> list[tuple[int, int, bool]]:
    """Find complete JSON-style string tokens without parsing the object.

    This lexical pass is intentionally tolerant: malformed response text is
    still rejected by the strict parser later.  It exists only so the adapter
    can decode valid JSON string values before replacing escaped local paths;
    replacing raw escaped fragments in-place can otherwise leave invalid JSON.
    """

    spans: list[tuple[int, int, bool]] = []
    index = 0
    length = len(value)
    while index < length:
        if value[index] != '"':
            index += 1
            continue
        start = index
        index += 1
        escaped = False
        while index < length:
            character = value[index]
            if escaped:
                escaped = False
                index += 1
                continue
            if character == "\\":
                escaped = True
                index += 1
                continue
            if character == '"':
                end = index + 1
                lookahead = end
                while lookahead < length and value[lookahead] in " \t\r\n":
                    lookahead += 1
                spans.append((start, end, lookahead < length and value[lookahead] == ":"))
                index = end
                break
            index += 1
        else:
            # An unclosed token is left to the strict grammar/error branch.
            break
    return spans


def _mask_http_urls(value: str) -> tuple[str, dict[str, str]]:
    """Mask valid HTTP(S) URLs for one adapter-only strict parse pass."""

    spans = _response_http_url_spans(value)
    if not spans:
        return value, {}
    replacements: list[tuple[int, int, str]] = []
    token_values: dict[str, str] = {}
    for index, (start, end) in enumerate(spans):
        token = f"<HTTP_URL_{index}>"
        while token in value or token in token_values:
            token = f"<HTTP_URL_{index}_{len(token_values)}>"
        token_values[token] = value[start:end]
        replacements.append((start, end, token))
    masked = value
    for start, end, replacement in reversed(replacements):
        masked = masked[:start] + replacement + masked[end:]
    return masked, token_values


def _restore_http_url_tokens(value: Any, token_values: Mapping[str, str]) -> Any:
    """Restore masked HTTP(S) URL values in a parsed response object."""

    if isinstance(value, str):
        restored = value
        for token, original in token_values.items():
            restored = restored.replace(token, original)
        return restored
    if isinstance(value, Mapping):
        return {key: _restore_http_url_tokens(child, token_values) for key, child in value.items()}
    if isinstance(value, list):
        return [_restore_http_url_tokens(child, token_values) for child in value]
    if isinstance(value, tuple):
        return tuple(_restore_http_url_tokens(child, token_values) for child in value)
    return value


def _parse_consultant_response_text(response: str) -> dict[str, Any]:
    """Use the unchanged strict parser while exempting validated HTTP URLs."""

    _response_nonpath_precheck(response)
    masked, token_values = _mask_http_urls(response)
    parsed = _parse_response_text(masked)
    return _restore_http_url_tokens(parsed, token_values)


def _response_value_path_precheck_for_consultant(value: Any, *, path: str = "response", depth: int = 0) -> None:
    """Apply strict local-path rejection while preserving legal HTTP URLs."""

    if depth > 12:
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _response_value_path_precheck_for_consultant(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _response_value_path_precheck_for_consultant(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, str) and _response_sanitization_match_records(value):
        _response_invalid("ABSOLUTE_PATH")


def _replace_response_path_records(
    value: str,
    records: Sequence[tuple[int, int, str, str]],
) -> str:
    """Replace non-overlapping path records with the canonical placeholder."""

    if not records:
        return value
    pieces: list[str] = []
    cursor = 0
    for start, end, _kind, _raw in records:
        pieces.append(value[cursor:start])
        pieces.append(LOCAL_PATH_PLACEHOLDER)
        cursor = end
    pieces.append(value[cursor:])
    return "".join(pieces)


def _replace_decoded_string_paths(value: str) -> tuple[str, list[tuple[int, int, str, str]]]:
    """Replace paths in one decoded JSON string value.

    The exact matcher is re-applied to the decoded value for bounded residual
    fragments (notably UNC/POSIX separators).  This never touches JSON syntax;
    the caller re-encodes the resulting string as one complete JSON token.
    """

    original_records = _response_sanitization_match_records(value)
    sanitized = value
    for _ in range(64):
        current_records = _response_sanitization_match_records(sanitized)
        if not current_records:
            break
        sanitized = _replace_response_path_records(sanitized, current_records)
    return sanitized, original_records


def _path_sanitization_digest(records: Sequence[tuple[int, int, str, str]]) -> str | None:
    """Return a digest of path kinds and hashes, never of raw path values."""

    if not records:
        return None
    redacted = [
        {
            "kind": kind,
            "sha256": hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
        }
        for _start, _end, kind, raw in records
    ]
    return hashlib.sha256(canonical_json(redacted).encode("utf-8")).hexdigest()


def _empty_path_sanitization_telemetry() -> dict[str, Any]:
    """Return the bounded default response-adapter telemetry."""

    return {
        "path_sanitization_count": 0,
        "path_sanitization_digest": None,
        "response_sha256_before": None,
        "response_sha256_after": None,
    }


def _response_nonpath_precheck(response: str) -> None:
    """Run bounded/secret checks before any URL masking or path handling."""

    if "\x00" in response:
        _response_invalid("NUL")
    try:
        encoded = response.encode("utf-8")
    except UnicodeEncodeError:
        _response_invalid("INVALID_UTF8")
    if len(response) > MAX_RESPONSE_TEXT_LENGTH or len(encoded) > MAX_RESPONSE_BYTES:
        _response_invalid("TOO_LARGE")
    if _RESPONSE_SENSITIVE_ASSIGNMENT_PATTERN.search(response):
        raise ProjectDiscoveryError("DISCOVERY_SECRET_REJECTED", "secret-like assignment in consultation response")
    if any(pattern.search(response) for pattern in _SECRET_PATTERNS):
        raise ProjectDiscoveryError("DISCOVERY_SECRET_REJECTED", "secret-like text in consultation response")


def _attach_response_telemetry(
    error: ProjectDiscoveryError,
    telemetry: Mapping[str, Any] | None,
) -> ProjectDiscoveryError:
    """Attach only bounded response-adapter telemetry to a failed parse.

    The exception itself remains de-identified.  This attribute is consumed
    by the one-shot discovery receipt writer and never contains response text,
    paths, or decoder details.
    """

    error.response_telemetry = _merge_path_sanitization_telemetry(telemetry)  # type: ignore[attr-defined]
    return error


def _records_response_telemetry(
    records: Sequence[tuple[int, int, str, str]],
    *,
    response_before: str | None = None,
    response_after: str | None = None,
) -> dict[str, Any]:
    """Build bounded telemetry from transient path records."""

    telemetry = _empty_path_sanitization_telemetry()
    telemetry["path_sanitization_count"] = min(len(records), MAX_RESPONSE_TEXT_LENGTH)
    telemetry["path_sanitization_digest"] = _path_sanitization_digest(records)
    if isinstance(response_before, str) and _RESPONSE_PATH_DIGEST_RE.fullmatch(response_before):
        telemetry["response_sha256_before"] = response_before
    if isinstance(response_after, str) and _RESPONSE_PATH_DIGEST_RE.fullmatch(response_after):
        telemetry["response_sha256_after"] = response_after
    return telemetry


def _sanitize_response_mapping_values(
    value: Any,
    records: list[tuple[int, int, str, str]],
    *,
    depth: int = 0,
) -> Any:
    """Sanitize decoded response values while enforcing the key boundary.

    This runs only after the bounded response grammar has selected one object.
    Consequently prose/path text outside that object is discarded rather than
    persisted.  Keys are never rewritten: an absolute-path key is a response
    grammar violation and fails closed.
    """

    if depth > 12:
        return value
    if isinstance(value, Mapping):
        result: dict[Any, Any] = {}
        for key, child in value.items():
            if isinstance(key, str) and _response_sanitization_match_records(key):
                try:
                    _response_invalid("ABSOLUTE_PATH")
                except ProjectDiscoveryError as exc:
                    raise _attach_response_telemetry(exc, _records_response_telemetry(records)) from None
            result[key] = _sanitize_response_mapping_values(child, records, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [_sanitize_response_mapping_values(child, records, depth=depth + 1) for child in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_response_mapping_values(child, records, depth=depth + 1) for child in value)
    if isinstance(value, str):
        sanitized, child_records = _replace_decoded_string_paths(value)
        records.extend(child_records)
        return sanitized
    return value


def _sanitize_response_text_paths(value: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Parse, sanitize, and return one bounded consultant response object.

    The adapter order is intentionally different from the public strict parser:
    the complete transient text is checked for controls/secrets first, then
    the existing fence/prose object grammar selects one mapping, keys are
    rejected if path-like, and only decoded string values are represented with
    ``<LOCAL_PATH>``.  The direct ``_parse_response_text`` behavior is not
    changed by this compatibility boundary.
    """

    telemetry = _empty_path_sanitization_telemetry()
    records: list[tuple[int, int, str, str]] = []
    try:
        # A path is not checked here.  It is handled only after extraction so
        # paths in ignored wrapper prose never become persistent telemetry.
        telemetry["response_sha256_before"] = _response_text_sha256(value)
        _response_nonpath_precheck(value)
        parsed = _parse_response_text_without_path_check(value)
        sanitized = _sanitize_response_mapping_values(parsed, records)
        if not isinstance(sanitized, dict):
            _response_invalid("ROOT_NOT_OBJECT")
        _response_value_path_precheck_for_consultant(sanitized)
        telemetry = _records_response_telemetry(
            records,
            response_before=telemetry["response_sha256_before"],
            response_after=_response_text_sha256(
                json.dumps(sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            ),
        )
        return sanitized, telemetry
    except ProjectDiscoveryError as exc:
        known = _merge_path_sanitization_telemetry(
            telemetry,
            _records_response_telemetry(records),
        )
        raise _attach_response_telemetry(exc, known) from None


def _merge_path_sanitization_telemetry(*values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge adapter telemetry while retaining only bounded digest fields."""

    result = _empty_path_sanitization_telemetry()
    records: list[dict[str, str]] = []
    total_count = 0
    for value in values:
        if not isinstance(value, Mapping):
            continue
        count = value.get("path_sanitization_count")
        if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= MAX_RESPONSE_TEXT_LENGTH:
            total_count += count
        digest = value.get("path_sanitization_digest")
        if isinstance(digest, str) and _RESPONSE_PATH_DIGEST_RE.fullmatch(digest):
            records.append({"digest": digest})
        for key in ("response_sha256_before", "response_sha256_after"):
            candidate = value.get(key)
            if result[key] is None and isinstance(candidate, str) and _RESPONSE_PATH_DIGEST_RE.fullmatch(candidate):
                result[key] = candidate
    result["path_sanitization_count"] = min(total_count, MAX_RESPONSE_TEXT_LENGTH)
    if records:
        result["path_sanitization_digest"] = hashlib.sha256(canonical_json(records).encode("utf-8")).hexdigest()
    return result


def _sanitize_consultant_response(response: Any) -> tuple[Any, dict[str, Any]]:
    """Run the bounded response adapter before semantic normalization.

    Raw text is reduced to one parsed object before local-path replacement;
    wrapper prose is therefore never retained.  A real bridge envelope keeps
    its verified receipt/scope metadata, while its response text is replaced
    by canonical JSON for the existing envelope normalizer.
    """

    if isinstance(response, str):
        return _sanitize_response_text_paths(response)
    if is_bridge_envelope(response):
        sanitized_envelope = copy.deepcopy(dict(response))
        response_key = next(
            (key for key in ("response_text", "responseText", "response") if isinstance(sanitized_envelope.get(key), str)),
            None,
        )
        if response_key is None:
            return sanitized_envelope, _empty_path_sanitization_telemetry()
        try:
            sanitized_mapping, telemetry = _sanitize_response_text_paths(str(sanitized_envelope[response_key]))
        except ProjectDiscoveryError:
            raise
        sanitized_envelope[response_key] = json.dumps(
            sanitized_mapping,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sanitized_envelope, telemetry

    if isinstance(response, Mapping):
        records: list[tuple[int, int, str, str]] = []
        try:
            sanitized_mapping = _sanitize_response_mapping_values(response, records)
            if not isinstance(sanitized_mapping, Mapping):
                raise ProjectDiscoveryError("DISCOVERY_RESPONSE_INVALID", "consultation response must be an object")
            _response_value_path_precheck_for_consultant(sanitized_mapping)
        except ProjectDiscoveryError as exc:
            raise _attach_response_telemetry(exc, _records_response_telemetry(records)) from None
        telemetry = _records_response_telemetry(records)
        return dict(sanitized_mapping), telemetry
    if isinstance(response, list):
        records = []
        try:
            sanitized_items = _sanitize_response_mapping_values(response, records)
            _response_value_path_precheck_for_consultant(sanitized_items)
        except ProjectDiscoveryError as exc:
            raise _attach_response_telemetry(exc, _records_response_telemetry(records)) from None
        return sanitized_items, _records_response_telemetry(records)
    if isinstance(response, tuple):
        sanitized, telemetry = _sanitize_consultant_response(list(response))
        return tuple(sanitized), telemetry
    return response, _empty_path_sanitization_telemetry()


def _is_json_value_only(text: str) -> bool:
    """Return whether *text* is exactly one JSON value.

    This helper is used only for prose segments.  A scalar such as ``true`` or
    ``42`` is not accepted as explanatory prose because it would make the
    response ambiguous; arrays/objects are rejected separately by delimiter
    checks.
    """

    # Keep the trim set to RFC 8259 JSON whitespace.  ``str.strip()`` would
    # silently discard other C0 controls (for example vertical tab), which
    # would turn an unsafe external control into accepted prose.
    candidate = text.strip(" \t\r\n")
    if not candidate:
        return False
    decoder = json.JSONDecoder()
    try:
        _, end = decoder.raw_decode(candidate)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        return False
    return not candidate[end:].strip(" \t\r\n")


def _validate_response_prose(text: str, *, side: str) -> None:
    """Validate one bounded prefix/suffix prose segment.

    Brackets and backticks are deliberately forbidden in prose.  This keeps
    the grammar unambiguous and ensures Markdown/code fences cannot be treated
    as harmless decoration around an arbitrary JSON fragment.
    """

    # Only JSON whitespace may be ignored at a prose boundary.  Any other
    # control character is outside a JSON string and must fail closed.
    prose = text.strip(" \t\r\n")
    if not prose:
        return
    if len(prose) > MAX_RESPONSE_PROSE_LENGTH:
        _response_invalid("PROSE_TOO_LARGE")
    if any(ord(character) < 0x20 for character in prose if character not in "\t\n\r"):
        _response_invalid("PROSE_CONTROL_CHAR")
    if any(character in prose for character in "{}[]`"):
        _response_invalid("PROSE_DELIMITER")
    if _RESPONSE_CODE_LINE_PATTERN.search(prose):
        _response_invalid("PROSE_CODE")
    if _is_json_value_only(prose):
        _response_invalid("PROSE_JSON_VALUE")


def _validate_fenced_prose(text: str, *, side: str) -> None:
    """Validate bounded prose outside an explicit JSON fence.

    A fence supplies the unambiguous JSON boundary, so ordinary prose braces
    and brackets are allowed here (for example, ``{candidate}`` in a heading
    or explanation).  Backticks remain forbidden outside the selected fence:
    they could introduce an unrecognized inline or secondary code fence.
    Secrets, paths, NULs, and the whole-response length bound are checked by
    :func:`_response_text_precheck` before this function runs.
    """

    # Do not let ``str.strip()`` erase an unsupported C0 control at a fence
    # boundary.  LF/CR/TAB remain ordinary bounded formatting whitespace.
    prose = text.strip(" \t\r\n")
    if not prose:
        return
    if len(prose) > MAX_RESPONSE_PROSE_LENGTH:
        _response_invalid("FENCE_PROSE_TOO_LARGE")
    if any(ord(character) < 0x20 for character in prose if character not in "\t\n\r"):
        _response_invalid("FENCE_PROSE_CONTROL_CHAR")
    if "`" in prose:
        _response_invalid("FENCE_PROSE_BACKTICK")
    if _RESPONSE_CODE_LINE_PATTERN.search(prose):
        _response_invalid("FENCE_PROSE_CODE")


def _normalise_json_string_controls(text: str, *, branch_prefix: str = "OBJECT") -> tuple[str, str | None]:
    """Escape only safe literal controls found inside JSON strings.

    The consultation prompt remains an RFC 8259/Dialogue Policy contract: a
    response should emit escaped controls.  This normalizer is a deliberately
    tiny compatibility boundary for a known renderer defect.  It runs only on
    the one candidate object selected by the existing balanced scanner, never
    on surrounding prose, and it does not attempt to repair escapes, quotes,
    UTF-8, or structure.  LF/CR/TAB inside a string become their deterministic
    JSON spellings; all external JSON whitespace is copied byte-for-byte and
    every other C0 control fails closed.

    The branch token tells the caller whether a safe recovery was applied.  It
    is used only to choose a de-identified grammar branch if strict decoding
    still fails; no response text or normalized raw payload is retained.
    """

    if not isinstance(text, str):
        _response_invalid(f"{branch_prefix}_INVALID_TEXT")
    normalized: list[str] = []
    in_string = False
    escaped = False
    changed = False
    for character in text:
        codepoint = ord(character)
        if in_string:
            if escaped:
                # Preserve the escape exactly.  In particular, an invalid
                # escape or a backslash before a literal control is not fixed.
                normalized.append(character)
                escaped = False
                continue
            if character == "\\":
                normalized.append(character)
                escaped = True
                continue
            if character == '"':
                normalized.append(character)
                in_string = False
                continue
            if codepoint < 0x20:
                if character == "\n":
                    normalized.append("\\n")
                elif character == "\r":
                    normalized.append("\\r")
                elif character == "\t":
                    normalized.append("\\t")
                elif character == "\x00":
                    _response_invalid("NUL")
                else:
                    _response_invalid(f"{branch_prefix}_CONTROL_CHAR_UNSUPPORTED")
                changed = True
                continue
            normalized.append(character)
            continue

        if character == '"':
            normalized.append(character)
            in_string = True
            continue
        if codepoint < 0x20 and character not in "\t\n\r":
            if character == "\x00":
                _response_invalid("NUL")
            _response_invalid(f"{branch_prefix}_CONTROL_CHAR_OUTSIDE_STRING")
        # Structural LF/CR/TAB and all other non-control text are copied
        # unchanged.  The strict decoder still decides whether the structure
        # is legal JSON whitespace in this location.
        normalized.append(character)
    return "".join(normalized), f"{branch_prefix}_CONTROL_CHAR_ESCAPED" if changed else None


def _reject_decoded_response_controls(value: Any, *, branch_prefix: str, depth: int = 0) -> None:
    """Reject escaped C0 controls that strict JSON decoding materializes.

    The lexical normalizer handles literal controls before decoding, but an
    already escaped ``\\u0001`` would otherwise materialize as the same unsafe
    character after ``raw_decode``.  Keep the accepted semantic set identical
    for both spellings: LF/CR/TAB are allowed, NUL and every other C0 control
    fail closed with a de-identified grammar branch.
    """

    if depth > 12:
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str):
                _reject_decoded_response_controls(key, branch_prefix=branch_prefix, depth=depth + 1)
            _reject_decoded_response_controls(child, branch_prefix=branch_prefix, depth=depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for child in value:
            _reject_decoded_response_controls(child, branch_prefix=branch_prefix, depth=depth + 1)
        return
    if isinstance(value, str):
        for character in value:
            if ord(character) < 0x20 and character not in "\t\n\r":
                if character == "\x00":
                    _response_invalid("NUL")
                _response_invalid(f"{branch_prefix}_CONTROL_CHAR_UNSUPPORTED")


def _decode_bare_response_object(text: str, *, branch_prefix: str = "FENCE_BODY") -> dict[str, Any]:
    """Decode exactly one strict JSON object from a bounded fragment.

    ``branch_prefix`` is an internal context token (currently ``FENCE_BODY``
    or ``OBJECT``) used only to keep offline diagnostics stable.  It is never
    populated from response content.
    """

    # Trim only legal JSON boundary whitespace.  This preserves unsupported
    # controls for the normalizer instead of silently deleting them.
    candidate = text.strip(" \t\r\n")
    if not candidate:
        _response_invalid(f"{branch_prefix}_EMPTY")
    candidate, recovery_branch = _normalise_json_string_controls(candidate, branch_prefix=branch_prefix)
    decoder = _response_json_decoder()
    parse_branch: str | None = None
    try:
        parsed, end = decoder.raw_decode(candidate)
    except _DuplicateResponseKey:
        parse_branch = f"{branch_prefix}_DUPLICATE_KEY"
    except RecursionError:
        parse_branch = f"{branch_prefix}_TOO_DEEP"
    except (TypeError, ValueError, json.JSONDecodeError):
        # Keep the outer compatibility error stable while identifying only the
        # bounded grammar path, never decoder text or a response excerpt.
        parse_branch = recovery_branch or f"{branch_prefix}_MALFORMED_JSON"
    if parse_branch is not None:
        _response_invalid(parse_branch)
    if not isinstance(parsed, dict):
        _response_invalid(f"{branch_prefix}_ROOT_NOT_OBJECT")
    if candidate[end:].strip(" \t\r\n"):
        _response_invalid(f"{branch_prefix}_TRAILING_DATA")
    _reject_decoded_response_controls(parsed, branch_prefix=branch_prefix)
    return parsed


def _balanced_json_object_end(text: str, start: int) -> int | None:
    """Return the end of one structurally balanced object, if any.

    This scanner is intentionally lexical rather than a first-brace/last-
    brace slice.  It tracks both object and array delimiters and ignores all
    delimiters inside JSON strings while honoring escaped quotes/backslashes.
    JSON syntax itself is still checked by the strict decoder after a range is
    found.
    """

    if start < 0 or start >= len(text) or text[start] != "{":
        return None
    stack: list[str] = ["{"]
    in_string = False
    escaped = False
    for index in range(start + 1, len(text)):
        character = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            stack.append(character)
        elif character in "}]":
            if not stack:
                return None
            expected = "}" if stack[-1] == "{" else "]"
            if character != expected:
                return None
            stack.pop()
            if not stack:
                return index + 1
    return None


def _scan_response_object_ranges(text: str) -> tuple[list[tuple[int, int]], list[int], bool]:
    """Find balanced object ranges and all unquoted object starts in one pass.

    Returning starts separately lets the caller distinguish a missing object
    from an unbalanced/malformed one without accepting a valid nested object
    hidden inside malformed outer text.  A range contained by another range
    is expected for nested objects and is filtered by the caller as such.
    The final boolean records a mismatched or unclosed delimiter anywhere in
    the complete no-fence text.  The response is rejected when it is set,
    even if another object happened to be balanced later in the text.
    """

    starts: list[int] = []
    ranges: list[tuple[int, int]] = []
    stack: list[tuple[str, int]] = []
    in_string = False
    escaped = False
    structural_error = False
    for index, character in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "{[":
            if character == "{":
                starts.append(index)
            stack.append((character, index))
        elif character in "}]":
            expected = "}" if stack and stack[-1][0] == "{" else "]"
            if not stack or character != expected:
                structural_error = True
                # A mismatched delimiter makes the current syntactic region
                # unusable.  Reset only the lexical stack so a later object
                # can be observed for diagnostics; the error flag prevents it
                # from being accepted as a hidden fallback.
                stack.clear()
                continue
            opener, opener_index = stack.pop()
            if opener == "{":
                ranges.append((opener_index, index + 1))
    if stack:
        structural_error = True
    return ranges, starts, structural_error


def _top_level_object_ranges(ranges: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Remove nested ranges, preserving only candidate object roots."""

    result: list[tuple[int, int]] = []
    for candidate in ranges:
        start, end = candidate
        contained = any(
            other != candidate
            and other[0] <= start
            and end <= other[1]
            and (other[0] < start or end < other[1])
            for other in ranges
        )
        if not contained:
            result.append(candidate)
    return result


def _decode_response_object_with_prose(text: str) -> dict[str, Any]:
    """Decode one unique balanced object surrounded by safe no-fence prose.

    Every object boundary comes from the string-aware balanced scanner.  The
    strict decoder then validates the exact range, and only the unique
    top-level range may be considered.  Prefix/suffix prose is deliberately
    delimiter-free in this no-fence grammar, so a second object, array, or
    bracket cannot be hidden in ignored text.
    """

    # Keep unsupported controls visible to the prose validator.  Only the
    # four JSON whitespace characters may be removed at the response boundary.
    candidate_text = text.strip(" \t\r\n")
    if not candidate_text:
        _response_invalid("EMPTY")

    # Fast root-type rejection for an exact scalar/array JSON response.  This
    # does not select an object; the balanced scanner below remains the sole
    # source of accepted no-fence object boundaries.
    whole_branch: str | None = None
    try:
        whole_value, whole_end = _response_json_decoder().raw_decode(candidate_text)
    except _DuplicateResponseKey:
        whole_branch = "OBJECT_DUPLICATE_KEY"
    except RecursionError:
        whole_branch = "OBJECT_TOO_DEEP"
    except (TypeError, ValueError, json.JSONDecodeError):
        whole_value = None
        whole_end = -1
    if whole_branch is not None:
        _response_invalid(whole_branch)
    if whole_end >= 0 and not candidate_text[whole_end:].strip() and not isinstance(whole_value, dict):
        _response_invalid("ROOT_NOT_OBJECT")

    ranges, starts, structural_error = _scan_response_object_ranges(candidate_text)
    if structural_error:
        _response_invalid("OBJECT_UNBALANCED")
    roots = _top_level_object_ranges(ranges)
    if len(roots) > 1:
        _response_invalid("MULTIPLE_OBJECTS")
    if not roots:
        _response_invalid("OBJECT_UNBALANCED" if starts else "OBJECT_MISSING")

    start, end = roots[0]
    parsed = _decode_bare_response_object(
        candidate_text[start:end],
        branch_prefix="OBJECT",
    )
    _validate_response_prose(candidate_text[:start], side="prefix")
    _validate_response_prose(candidate_text[end:], side="suffix")
    return parsed


def _response_fence_markers(text: str) -> list[tuple[int, int, str]]:
    """Return complete-line fence markers as ``(start, end, kind)`` tuples.

    ``kind`` is ``open`` for the explicit JSON opener, ``close`` for an empty
    closing fence, and ``unsupported`` for any other complete-line marker.
    Inline backticks are checked after a candidate fence is located (as prose)
    or by the body decoder (as JSON); this preserves valid backtick characters
    inside a JSON string without allowing inline prose to hide a fence.
    """

    markers: list[tuple[int, int, str]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        content = line[:-2] if line.endswith("\r\n") else line.rstrip("\r\n")
        if "```" in content:
            match = _JSON_FENCE_LINE_RE.fullmatch(content)
            if match is not None:
                info = match.group("info").strip()
                kind = "open" if info.casefold() == "json" else "close" if not info else "unsupported"
                markers.append((offset, offset + len(line), kind))
        offset += len(line)
    # ``splitlines`` returns a final non-newline-terminated line, so offset is
    # normally exactly len(text).  Keep this defensive path for unusual text
    # implementations without ever inspecting or reporting the content.
    if offset < len(text):
        content = text[offset:]
        if "```" in content:
            markers.append((offset, len(text), "unsupported"))
    return markers


def _parse_response_text(response: str) -> dict[str, Any]:
    """Parse the minimal Step13 response grammar from transient text."""

    _response_text_precheck(response)
    markers = _response_fence_markers(response)
    if markers:
        unsupported = [marker for marker in markers if marker[2] == "unsupported"]
        if unsupported:
            _response_invalid("UNSUPPORTED_FENCE")
        opens = [marker for marker in markers if marker[2] == "open"]
        closes = [marker for marker in markers if marker[2] == "close"]
        if len(opens) > 1 or len(closes) > 1:
            _response_invalid("MULTIPLE_FENCES")
        if not opens:
            _response_invalid("UNEXPECTED_FENCE")
        if not closes:
            _response_invalid("FENCE_UNCLOSED")
        if len(markers) != 2:
            _response_invalid("MULTIPLE_FENCES")
        opener = opens[0]
        closer = closes[0]
        if closer[0] < opener[1]:
            _response_invalid("FENCE_ORDER")
        prefix = response[: opener[0]]
        body = response[opener[1] : closer[0]]
        suffix = response[closer[1] :]
        _validate_fenced_prose(prefix, side="prefix")
        _validate_fenced_prose(suffix, side="suffix")
        return _decode_bare_response_object(body, branch_prefix="FENCE_BODY")
    if "```" in response:
        # This is defensive for a line-splitting edge case; all ordinary
        # triple-backtick text is already represented as an unsupported marker.
        _response_invalid("UNSUPPORTED_FENCE")
    return _decode_response_object_with_prose(response)


def _parse_response_text_without_path_check(response: str) -> dict[str, Any]:
    """Parse the same bounded grammar without the direct path precheck.

    This is an adapter-only parse phase.  It deliberately duplicates the
    control flow of :func:`_parse_response_text` so the public strict parser
    remains untouched, while allowing the adapter to inspect decoded object
    keys/values before applying its deterministic representation policy.
    """

    _response_nonpath_precheck(response)
    markers = _response_fence_markers(response)
    if markers:
        unsupported = [marker for marker in markers if marker[2] == "unsupported"]
        if unsupported:
            _response_invalid("UNSUPPORTED_FENCE")
        opens = [marker for marker in markers if marker[2] == "open"]
        closes = [marker for marker in markers if marker[2] == "close"]
        if len(opens) > 1 or len(closes) > 1:
            _response_invalid("MULTIPLE_FENCES")
        if not opens:
            _response_invalid("UNEXPECTED_FENCE")
        if not closes:
            _response_invalid("FENCE_UNCLOSED")
        if len(markers) != 2:
            _response_invalid("MULTIPLE_FENCES")
        opener = opens[0]
        closer = closes[0]
        if closer[0] < opener[1]:
            _response_invalid("FENCE_ORDER")
        prefix = response[: opener[0]]
        body = response[opener[1] : closer[0]]
        suffix = response[closer[1] :]
        _validate_fenced_prose(prefix, side="prefix")
        _validate_fenced_prose(suffix, side="suffix")
        return _decode_bare_response_object(body, branch_prefix="FENCE_BODY")
    if "```" in response:
        _response_invalid("UNSUPPORTED_FENCE")
    return _decode_response_object_with_prose(response)


def _normalise_response(
    response: Any,
    *,
    fallback_real_goal: str | None = None,
    allow_http_url_paths: bool = False,
) -> dict[str, Any]:
    if is_bridge_envelope(response):
        try:
            response = normalize_bridge_envelope(response)["response_text"]
        except BridgeEnvelopeError as exc:
            raise ProjectDiscoveryError(exc.code, str(exc)) from exc
    if isinstance(response, str):
        response = _parse_consultant_response_text(response) if allow_http_url_paths else _parse_response_text(response)
    if not isinstance(response, Mapping):
        raise ProjectDiscoveryError("DISCOVERY_RESPONSE_INVALID", "consultation response must be an object")
    if allow_http_url_paths:
        _response_value_path_precheck_for_consultant(response)
    else:
        _response_value_path_precheck(response)
    if not isinstance(response, dict):
        response = dict(response)
    _assert_safe(response, path="response")
    source = dict(response)
    no_direct = source.get(
        "no_direct_match_found",
        source.get("no_direct_match", source.get("no_match", False)),
    )
    if not isinstance(no_direct, bool):
        raise ProjectDiscoveryError(
            "DISCOVERY_RESPONSE_INVALID",
            "no_direct_match_found must be boolean",
            semantic_branch="NO_DIRECT_MATCH",
        )
    candidates = _dedupe_candidates(_raw_candidates(source))
    primary_raw = source.get(
        "primary_recommendation",
        source.get("primary", source.get("selected_repository", source.get("selected_route"))),
    )
    primary = _recommendation_value(primary_raw, "primary_recommendation")
    if primary is None and not no_direct:
        raise ProjectDiscoveryError(
            "PRIMARY_RECOMMENDATION_REQUIRED",
            "a direct result needs one primary recommendation",
            semantic_branch="PRIMARY",
        )
    if primary is not None and _recommendation_repo_identity(primary):
        identity = _recommendation_repo_identity(primary)
        if identity not in {candidate["repo_url"] for candidate in candidates}:
            # A primary URL is itself a candidate claim and must be locally
            # verified before it can appear as a recommendation.
            candidates.insert(0, _candidate_from({"repo_url": identity}, 0))
            if len(candidates) > MAX_CANDIDATE_REPOSITORIES:
                raise ProjectDiscoveryError(
                    "CANDIDATE_LIMIT",
                    "at most five candidate repositories are allowed",
                    semantic_branch="CANDIDATE",
                )
    alternatives_raw = source.get("alternatives", source.get("alternative_recommendations", []))
    if not isinstance(alternatives_raw, list):
        raise ProjectDiscoveryError(
            "DISCOVERY_RESPONSE_INVALID",
            "alternatives must be an array",
            semantic_branch="ALTERNATIVE",
        )
    alternatives: list[Any] = []
    primary_identity = _recommendation_repo_identity(primary)
    seen_alternatives: set[str] = set()
    for index, raw in enumerate(alternatives_raw):
        value = _recommendation_value(raw, f"alternatives[{index}]")
        identity = _recommendation_repo_identity(value)
        key = identity or canonical_json(value)
        if identity and identity == primary_identity:
            continue
        if key in seen_alternatives:
            continue
        seen_alternatives.add(key)
        alternatives.append(value)
        if len(alternatives) > MAX_ALTERNATIVES:
            raise ProjectDiscoveryError(
                "ALTERNATIVE_LIMIT",
                "at most four alternatives are allowed",
                semantic_branch="ALTERNATIVE",
            )
    # If the response provided repository candidates but no explicit
    # alternatives, expose candidates after the primary as bounded alternatives
    # without calling this an architecture decision.
    if not alternatives:
        for candidate in candidates:
            if candidate["repo_url"] == primary_identity:
                continue
            alternatives.append({"repo_url": candidate["repo_url"], "name": candidate.get("name", "")})
            if len(alternatives) >= MAX_ALTERNATIVES:
                break
    if len(candidates) > MAX_CANDIDATE_REPOSITORIES:
        raise ProjectDiscoveryError(
            "CANDIDATE_LIMIT",
            "at most five candidate repositories are allowed",
            semantic_branch="CANDIDATE",
        )
    supplied_goal = source.get("real_goal", source.get("goal", fallback_real_goal or ""))
    result: dict[str, Any] = {
        "real_goal": _safe_text(supplied_goal, "real_goal", required=True),
        "search_summary": _safe_text(source.get("search_summary", source.get("summary", "")), "search_summary", required=True),
        "candidate_repositories": candidates,
        "relevant_non_repo_methods": _string_list(
            source.get("relevant_non_repo_methods", source.get("non_repo_methods", [])),
            "relevant_non_repo_methods",
            maximum=MAX_NON_REPO_METHODS,
        ),
        "primary_recommendation": primary,
        "why_primary": _safe_text(source.get("why_primary", source.get("primary_reason", "")), "why_primary", required=not no_direct),
        "simpler_alternative": _safe_text(source.get("simpler_alternative", source.get("simpler_route", "")), "simpler_alternative"),
        "important_risks": _string_list(source.get("important_risks", source.get("risks", [])), "important_risks", maximum=MAX_RISKS),
        "facts_needing_local_verification": _string_list(
            source.get("facts_needing_local_verification", source.get("local_verification_needed", [])),
            "facts_needing_local_verification",
            maximum=MAX_FACTS,
        ),
        "no_direct_match_found": no_direct,
        "alternatives": alternatives,
    }
    if no_direct and primary is None and not result["search_summary"]:
        raise ProjectDiscoveryError(
            "DISCOVERY_RESPONSE_INVALID",
            "no-direct-match result needs a bounded search summary",
            semantic_branch="SEARCH_SUMMARY",
        )
    return result


def _run_checked(command: Sequence[str], *, cwd: Path | None = None, timeout: int = MAX_GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProjectDiscoveryError("REPOSITORY_VERIFICATION_TIMEOUT", "repository verification exceeded its bound") from exc
    except OSError as exc:
        raise ProjectDiscoveryError("GIT_UNAVAILABLE", "local git verification could not start") from exc


def _read_bounded(path: Path, limit: int = 120_000) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _resource_id(logical_name: str) -> str:
    """Return a stable resource id derived only from a logical name."""

    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(logical_name)).strip("-").casefold()
    return f"resource-{slug or 'canonical'}"


def _canonical_resource_record(root: Path, relative_path: str, logical_name: str, *, limit: int = MAX_REPORT_BYTES) -> dict[str, Any]:
    """Describe one workflow-owned file without retaining an absolute path."""

    path = root / Path(relative_path)
    data = _read_bounded(path, limit)
    if data is None:
        raise ProjectDiscoveryError("CANONICAL_RESOURCE_INVALID", "required canonical resource is not readable")
    return {
        "resource_id": _resource_id(logical_name),
        "logical_name": logical_name,
        "path": relative_path,
        "digest": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


class GitRepositoryVerifier:
    """Read-only verifier used when callers do not inject another verifier."""

    def verify(
        self,
        candidate: Mapping[str, Any],
        *,
        project_root: Path,
        cache_dir: Path,
    ) -> dict[str, Any]:
        raw_url = candidate.get("repo_url") if isinstance(candidate, Mapping) else None
        canonical = canonicalize_repo_url(raw_url)
        transport_url = _repo_url_for_git(canonical)
        ls_remote = _run_checked(["git", "ls-remote", "--symref", transport_url, "HEAD"])
        if ls_remote.returncode != 0:
            raise ProjectDiscoveryError("REPOSITORY_NOT_REACHABLE", "git ls-remote could not verify repository")
        lines = [line.strip() for line in (ls_remote.stdout or "").splitlines() if line.strip()]
        head_sha: str | None = None
        default_ref: str | None = None
        for line in lines:
            if line.startswith("ref:") and line.endswith("\tHEAD"):
                default_ref = line.split()[1]
            elif "\tHEAD" in line:
                maybe = line.split("\t", 1)[0].strip()
                if re.fullmatch(r"[0-9a-fA-F]{40}", maybe):
                    head_sha = maybe.lower()
        if head_sha is None:
            raise ProjectDiscoveryError("REPOSITORY_HEAD_UNAVAILABLE", "git ls-remote did not expose default HEAD")
        clone_dir = cache_dir / ("repo-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16])
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        cloned = _run_checked(
            ["git", "clone", "--depth", "1", "--no-tags", transport_url, str(clone_dir)],
            timeout=MAX_GIT_TIMEOUT_SECONDS,
        )
        if cloned.returncode != 0:
            raise ProjectDiscoveryError("REPOSITORY_CLONE_FAILED", "candidate repository could not be cloned to temp cache")
        try:
            top_level = sorted(
                item.name
                for item in clone_dir.iterdir()
                if item.name not in {".git"}
            )[:MAX_TOP_LEVEL_ENTRIES]
        except OSError as exc:
            raise ProjectDiscoveryError("REPOSITORY_STRUCTURE_UNREADABLE", "candidate code structure could not be inspected") from exc
        readme: dict[str, Any] = {"present": False}
        for item in sorted(clone_dir.iterdir(), key=lambda path: path.name.casefold()):
            if item.name.casefold().startswith("readme"):
                data = _read_bounded(item)
                if data is not None:
                    readme = {"present": True, "path": item.name, "bytes": len(data), "digest": hashlib.sha256(data).hexdigest()}
                break
        license_info: dict[str, Any] = {"present": False}
        for item in sorted(clone_dir.iterdir(), key=lambda path: path.name.casefold()):
            if item.name.casefold().startswith("license"):
                data = _read_bounded(item)
                if data is not None:
                    license_info = {"present": True, "path": item.name, "bytes": len(data), "digest": hashlib.sha256(data).hexdigest()}
                break
        dependency_names = (
            "pyproject.toml",
            "requirements.txt",
            "setup.py",
            "package.json",
            "package-lock.json",
            "Cargo.toml",
            "go.mod",
            "pom.xml",
            "build.gradle",
            "Gemfile",
        )
        dependencies: list[dict[str, Any]] = []
        for name in dependency_names:
            item = clone_dir / name
            data = _read_bounded(item)
            if data is not None:
                dependencies.append({"path": name, "bytes": len(data), "digest": hashlib.sha256(data).hexdigest()})
        code_paths: list[str] = []
        relevant_paths: list[str] = []
        search_terms = set(re.findall(r"[A-Za-z0-9_]{3,}", " ".join(str(candidate.get(key, "")) for key in ("name", "reusable_part", "likely_gap"))))
        for item in clone_dir.rglob("*"):
            if not item.is_file() or ".git" in item.parts or len(code_paths) >= MAX_CODE_PATHS:
                continue
            try:
                relative = item.relative_to(clone_dir).as_posix()
            except ValueError:
                continue
            if item.stat().st_size > 120_000:
                continue
            code_paths.append(relative)
            if search_terms and any(term.casefold() in relative.casefold() for term in search_terms):
                relevant_paths.append(relative)
            elif search_terms and item.suffix.casefold() in {".py", ".js", ".ts", ".tsx", ".rs", ".go", ".java", ".cpp", ".h"}:
                data = _read_bounded(item, 120_000)
                if data is not None:
                    text = data.decode("utf-8", errors="replace").casefold()
                    if any(term.casefold() in text for term in search_terms):
                        relevant_paths.append(relative)
        return {
            "verified": True,
            "verification_source": "local_git_verifier",
            "repo_url": canonical,
            "default_head": head_sha,
            "default_ref": default_ref,
            "ls_remote": {"head": head_sha, "default_ref": default_ref},
            "readme": readme,
            "license": license_info,
            "dependencies": dependencies,
            "code_structure": {"top_level_entries": top_level, "file_paths": sorted(code_paths)[:MAX_CODE_PATHS]},
            "relevant_implementation": {"paths": sorted(set(relevant_paths))[:MAX_CODE_PATHS]},
            "checked_read_only": True,
            "cache_scope": "temporary_only",
        }


def _call_verifier(
    verifier: Any,
    *,
    candidate: Mapping[str, Any],
    project_root: Path,
    cache_dir: Path,
    brief: Mapping[str, Any],
) -> Mapping[str, Any]:
    target = getattr(verifier, "verify", verifier)
    if not callable(target):
        raise ProjectDiscoveryError("VERIFIER_INVALID", "repository verifier must be callable or expose verify()")
    values = {
        "candidate": copy.deepcopy(dict(candidate)),
        "repo_url": candidate["repo_url"],
        "url": candidate["repo_url"],
        "project_root": project_root,
        "root": project_root,
        "cache_dir": cache_dir,
        "cache_root": cache_dir,
        "brief": copy.deepcopy(_brief_view(brief)),
    }
    try:
        signature = inspect.signature(target)
        parameters = list(signature.parameters.values())
        has_var_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
    except (TypeError, ValueError):
        try:
            value = target(candidate=copy.deepcopy(dict(candidate)), project_root=project_root, cache_dir=cache_dir)
        except Exception as exc:  # pragma: no cover - unusual callable
            raise ProjectDiscoveryError("REPOSITORY_VERIFICATION_FAILED", "local repository verifier failed") from exc
        if isinstance(value, bool):
            return {"verified": value, "verification_source": "INJECTED_VERIFIER"}
        if not isinstance(value, Mapping):
            raise ProjectDiscoveryError("REPOSITORY_VERIFICATION_FAILED", "local verifier must return an object")
        return value
    positional: list[Any] = []
    if has_var_kwargs:
        kwargs = values
    else:
        kwargs: dict[str, Any] = {}
        for parameter in parameters:
            if parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
                continue
            if parameter.name in values:
                if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                    positional.append(values[parameter.name])
                else:
                    kwargs[parameter.name] = values[parameter.name]
            elif parameter.default is inspect.Parameter.empty:
                if len(parameters) == 1:
                    value = values["candidate"]
                    if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
                        positional.append(value)
                    else:
                        kwargs[parameter.name] = value
                else:
                    raise ProjectDiscoveryError("VERIFIER_SIGNATURE_INVALID", "verifier has an unsupported required parameter")
    try:
        value = target(*positional, **kwargs)
    except ProjectDiscoveryError:
        raise
    except Exception as exc:
        raise ProjectDiscoveryError("REPOSITORY_VERIFICATION_FAILED", "local repository verifier failed") from exc
    if isinstance(value, bool):
        return {"verified": value, "verification_source": "INJECTED_VERIFIER"}
    if not isinstance(value, Mapping):
        raise ProjectDiscoveryError("REPOSITORY_VERIFICATION_FAILED", "local verifier must return an object")
    return value


def _verification_ok(value: Mapping[str, Any]) -> bool:
    if value.get("verified") is False or value.get("passed") is False or value.get("ok") is False:
        return False
    status = value.get("status")
    if isinstance(status, str) and status.strip().upper() in _FAILURE_STATUS:
        return False
    return True


def _failure_state_path(root: Path) -> Path:
    return root / ".research" / "discovery" / "CONSULTATION_STATE.json"


def _read_json(path: Path, code: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ProjectDiscoveryError(code, "canonical discovery file is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProjectDiscoveryError(code, "canonical discovery file could not be read") from exc
    if not isinstance(value, dict):
        raise ProjectDiscoveryError(code, "canonical discovery file must contain an object")
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> Path:
    if path.exists() and (path.is_symlink() or path.is_dir()):
        raise ProjectDiscoveryError("DISCOVERY_WRITE_FAILED", "canonical discovery path is not a regular file")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if len(text.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ProjectDiscoveryError("DISCOVERY_REPORT_TOO_LARGE", "discovery report exceeds bounded size")
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
        raise ProjectDiscoveryError("DISCOVERY_WRITE_FAILED", "canonical discovery file could not be written") from exc
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
    return path


def _load_current_report(root: Path, *, project_id: str, evidence_digest: str) -> dict[str, Any] | None:
    path = root / DISCOVERY_REPORT_RELATIVE_PATH
    current = _read_json(path, "DISCOVERY_REPORT_INVALID")
    if current is None:
        return None
    # A previous partially written/tampered object must not be treated as a
    # duplicate receipt.  Validate it before comparing the digest.
    current = _verify_report(current)
    if current.get("project_id") != project_id:
        raise ProjectDiscoveryError("DISCOVERY_PROJECT_MISMATCH", "existing discovery report belongs to another project")
    if current.get("evidence_digest") == evidence_digest:
        raise ProjectDiscoveryError("DUPLICATE_EVIDENCE", "the same local evidence digest was already consulted")
    return current


def _check_failed_retry(root: Path, *, project_id: str, evidence_digest: str) -> None:
    state = _read_json(_failure_state_path(root), "CONSULTATION_STATE_INVALID")
    if state is None:
        return
    if state.get("project_id") == project_id and state.get("evidence_digest") == evidence_digest:
        raise ProjectDiscoveryError(
            "CONSULTATION_NOT_RETRIED",
            "a failed or rate-limited consultation for this evidence will not be retried",
        )


def _record_failure(
    root: Path,
    *,
    project_id: str,
    evidence_digest: str,
    project_url: str,
    code: str,
    semantic_branch: str | None = None,
    grammar_branch: str | None = None,
    forensic: Mapping[str, Any] | None = None,
    response_telemetry: Mapping[str, Any] | None = None,
) -> None:
    diagnostics = _forensic_for_exception(
        ProjectDiscoveryError(code, "bounded consultation failure"),
        error_code=code,
    )
    if isinstance(forensic, Mapping):
        # The caller may have recovered a transport-level request count or
        # phase.  Copy only the closed forensic vocabulary; never persist the
        # original details mapping or an exception message.
        candidate_phase = forensic.get("failure_phase")
        if isinstance(candidate_phase, str) and candidate_phase in _FAILURE_PHASES:
            diagnostics["failure_phase"] = candidate_phase
        candidate_class = _stable_exception_class(forensic.get("exception_class"))
        if candidate_class != "UnknownException":
            diagnostics["exception_class"] = candidate_class
        candidate_code = _stable_failure_code(forensic.get("error_code"), default=None)
        if candidate_code is not None:
            diagnostics["error_code"] = candidate_code
        candidate_count = _known_request_count(forensic.get("request_count"))
        diagnostics["request_count"] = candidate_count
        candidate_attempts = forensic.get("attempt_count")
        diagnostics["attempt_count"] = (
            candidate_attempts
            if isinstance(candidate_attempts, int) and not isinstance(candidate_attempts, bool) and candidate_attempts == 1
            else 1
        )
    stable_code = diagnostics["error_code"]
    payload = {
        "schema_version": "discovery_consultation_state.v1",
        "project_id": project_id,
        "evidence_digest": evidence_digest,
        "project_url": project_url,
        "mode": CONSULTATION_MODE_FRESH,
        "status": "RATE_LIMITED" if "RATE_LIMIT" in stable_code else "FAILED",
        "error_code": stable_code,
        "failure_phase": diagnostics["failure_phase"],
        "exception_class": diagnostics["exception_class"],
        "request_count": diagnostics["request_count"],
        "attempt_count": diagnostics["attempt_count"],
        "recorded_at": _now(),
    }
    # Response adapter telemetry is intentionally digest-only.  In particular,
    # do not copy an exception message, field value, response body, or raw
    # path into this persistent receipt.
    bounded_response_telemetry = _merge_path_sanitization_telemetry(response_telemetry)
    payload.update(bounded_response_telemetry)
    # Semantic/grammar diagnostics are closed-set tokens.  Persist both for
    # response-validation failures so a failed receipt can distinguish lexical
    # rejection from semantic rejection without retaining response content.
    if code in {"DISCOVERY_INPUT_INVALID", "DISCOVERY_RESPONSE_INVALID"}:
        payload["semantic_branch"] = _sanitize_semantic_branch(semantic_branch)
    if code == "DISCOVERY_RESPONSE_INVALID":
        payload["grammar_branch"] = _sanitize_grammar_branch(grammar_branch)
    _assert_safe(payload)
    try:
        _atomic_json(_failure_state_path(root), payload)
    except ProjectDiscoveryError:
        # Preserve the original consultation failure if a process receipt
        # cannot be written; no raw transport detail is ever persisted.
        return


def _update_retention(root: Path, *, checked_brief: Mapping[str, Any], context: Mapping[str, Any], report_path: Path) -> dict[str, Any]:
    try:
        existing = load_retention_manifest(root)
    except ArtifactRetentionError as exc:
        raise ProjectDiscoveryError("RETENTION_MANIFEST_INVALID", "existing retention manifest failed validation") from exc
    items: list[dict[str, Any]] = []
    if existing is not None:
        items.extend(copy.deepcopy(existing.get("artifacts", [])))
    report_relative = report_path.relative_to(root).as_posix()
    if not any(item.get("path") == report_relative for item in items if isinstance(item, Mapping)):
        items.append({"path": report_relative, "class": ArtifactClass.CANONICAL.value})
    for canonical_path in (AVAILABLE_ASSETS_RELATIVE_PATH, LOCAL_PROJECT_PROFILE_RELATIVE_PATH):
        candidate = root / canonical_path
        if candidate.is_file() and not candidate.is_symlink():
            relative = canonical_path.as_posix()
            if not any(item.get("path") == relative for item in items if isinstance(item, Mapping)):
                items.append({"path": relative, "class": ArtifactClass.CANONICAL.value})
    context_digest = context.get("context_digest") if context.get("present") else None
    try:
        manifest = build_retention_manifest(
            items,
            project_id=str(checked_brief["project_id"]),
            brief_digest=sha256_json(dict(checked_brief)),
            context_digest=context_digest if isinstance(context_digest, str) and context_digest else None,
            context_resource_count=int(context.get("resource_count", 0)) if context.get("present") else None,
        )
        save_retention_manifest(root, manifest)
    except (ArtifactRetentionError, TypeError, ValueError) as exc:
        raise ProjectDiscoveryError("RETENTION_WRITE_FAILED", "retention manifest could not be updated") from exc
    return manifest


def _verify_report(report: Mapping[str, Any]) -> dict[str, Any]:
    try:
        validate_instance(dict(report), load_schema("discovery_report"))
    except ContractValidationError as exc:
        raise ProjectDiscoveryError("DISCOVERY_REPORT_INVALID", "discovery report failed its schema") from exc
    return copy.deepcopy(dict(report))


def validate_discovery_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach a discovery report against the local schema."""

    if not isinstance(report, Mapping):
        raise ProjectDiscoveryError("DISCOVERY_REPORT_INVALID", "discovery report must be an object")
    return _verify_report(report)


def normalize_discovery_response(response: Any, *, fallback_real_goal: str | None = None) -> dict[str, Any]:
    """Normalize a consultant response without invoking a transport."""

    return _normalise_response(response, fallback_real_goal=fallback_real_goal)


def discover_project(
    project_root: str | os.PathLike[str] = ".",
    *,
    consultant: Any = None,
    verifier: Any = None,
    brief: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one bounded FRESH Project Discovery consultation.

    The operation is intentionally one-shot.  No response can trigger a
    second GPT call; only a later local evidence digest permits another call,
    and failed/rate-limited evidence is explicitly recorded as non-retryable.
    """

    root = resolve_project_root(project_root)
    if brief is None:
        try:
            loaded = load_project_brief(root)
        except ProjectStateError as exc:
            raise ProjectDiscoveryError("BRIEF_UNREADABLE", "canonical project brief could not be loaded") from exc
        brief = loaded
    if not isinstance(brief, Mapping):
        raise ProjectDiscoveryError("BRIEF_NOT_FOUND", "an approved project brief is required")
    brief = _unwrap_brief(brief)
    try:
        checked_brief = validate_project_brief(brief, root)
    except ProjectIntakeError as exc:
        raise ProjectDiscoveryError("BRIEF_INVALID", "canonical project brief failed validation") from exc
    if checked_brief.get("state") != "APPROVED" or checked_brief.get("status") != "APPROVED":
        raise ProjectDiscoveryError("BRIEF_NOT_APPROVED", "Project Discovery requires state=APPROVED")
    project_url = _project_url_from_brief(checked_brief)
    evidence, evidence_digest = build_discovery_evidence(root, brief=checked_brief)
    project_id = str(checked_brief["project_id"])
    _load_current_report(root, project_id=project_id, evidence_digest=evidence_digest)
    _check_failed_retry(root, project_id=project_id, evidence_digest=evidence_digest)
    if consultant is None:
        raise ProjectDiscoveryError("CONSULTANT_REQUIRED", "an explicit one-shot discovery consultant is required")
    transport: Mapping[str, Any] | None = None
    response_telemetry = _empty_path_sanitization_telemetry()
    try:
        response = _call_consultant(consultant, project_url=project_url, evidence=evidence, brief=checked_brief)
        response, response_telemetry = _sanitize_consultant_response(response)
        transport = response if is_bridge_envelope(response) else None
        normalized = _normalise_response(
            response,
            fallback_real_goal=evidence["real_goal"],
            allow_http_url_paths=True,
        )
    except ProjectDiscoveryError as exc:
        # Everything in this block is part of the one consultation attempt:
        # adapter invocation, bridge-envelope validation, and response
        # grammar validation.  Record all of those failures so a newly
        # introduced bridge code cannot silently fall through to a retryable
        # state.  The verifier and asset stages are deliberately outside this
        # block and therefore never masquerade as consultation failures.
        forensic = getattr(exc, "forensic", None)
        if not isinstance(forensic, Mapping):
            forensic = _forensic_for_exception(
                exc,
                phase="RESPONSE_VALIDATION",
                request_count=1 if transport is not None else None,
            )
        response_telemetry = _merge_path_sanitization_telemetry(
            response_telemetry,
            getattr(exc, "response_telemetry", None),
            forensic,
        )
        _record_failure(
            root,
            project_id=project_id,
            evidence_digest=evidence_digest,
            project_url=project_url,
            code=exc.code,
            semantic_branch=getattr(exc, "semantic_branch", None),
            grammar_branch=getattr(exc, "grammar_branch", None),
            forensic=forensic,
            response_telemetry=response_telemetry,
        )
        raise
    # Verify every repository claim before constructing a completed report.
    checked_candidates: list[dict[str, Any]] = []
    selected_verifier = verifier if verifier is not None else GitRepositoryVerifier()
    with tempfile.TemporaryDirectory(prefix="project-discovery-cache-") as cache_name:
        cache_dir = Path(cache_name)
        for candidate in normalized["candidate_repositories"]:
            try:
                local = _call_verifier(
                    selected_verifier,
                    candidate=candidate,
                    project_root=root,
                    cache_dir=cache_dir,
                    brief=checked_brief,
                )
            except ProjectDiscoveryError:
                raise
            if not _verification_ok(local):
                raise ProjectDiscoveryError("REPOSITORY_VERIFICATION_FAILED", "candidate repository failed local verification")
            _assert_safe(local, path=f"local_verification[{candidate['repo_url']}]")
            candidate_copy = copy.deepcopy(candidate)
            candidate_copy["local_verification"] = {
                **copy.deepcopy(dict(local)),
                "verified_locally": True,
                "claims_source": "LOCAL_VERIFIER",
            }
            checked_candidates.append(candidate_copy)
    try:
        # Only candidates carrying the same local-verifier evidence used by
        # the report become canonical assets.  GPT-only claims remain claims.
        assets_after_verification = record_verified_candidates(root, checked_candidates, brief=checked_brief)
        asset_pack_after_verification = build_asset_pack(root, brief=checked_brief, purpose="discovery")
    except AssetLayerError as exc:
        raise ProjectDiscoveryError(
            "ASSET_UPDATE_FAILED",
            "asset update validation failed",
            details=_asset_error_details(exc),
            cause=exc,
        ) from exc
    # A recommendation is kept as a recommendation claim; local evidence is
    # attached separately and does not select a production architecture.
    result: dict[str, Any] = {
        "schema_version": DISCOVERY_REPORT_SCHEMA_VERSION,
        "project_id": project_id,
        "brief_digest": sha256_json(checked_brief),
        "brief_revision": int(checked_brief.get("revision", 1)),
        "evidence_digest": evidence_digest,
        "status": DISCOVERY_STATUS,
        "real_goal": normalized["real_goal"],
        "search_summary": normalized["search_summary"],
        "candidate_repositories": checked_candidates,
        "relevant_non_repo_methods": normalized["relevant_non_repo_methods"],
        "primary_recommendation": normalized["primary_recommendation"],
        "why_primary": normalized["why_primary"],
        "simpler_alternative": normalized["simpler_alternative"],
        "important_risks": normalized["important_risks"],
        "facts_needing_local_verification": normalized["facts_needing_local_verification"],
        "no_direct_match_found": normalized["no_direct_match_found"],
        "alternatives": normalized["alternatives"],
        "consultation": {
            "mode": CONSULTATION_MODE_FRESH,
            "project_url": project_url,
            "request_count": 1,
            "claims_digest": sha256_json(normalized),
            "evidence_digest": evidence_digest,
        },
        # This is bounded provenance for the adapter representation only; it
        # contains no consultant text or local path values.  Keeping it at the
        # report top level avoids widening the strict receipt schema.
        "response_sanitization": response_telemetry,
        "anti_tunnel_rule_applied": True,
        "production_architecture_selected": False,
        "stage_started": False,
        "business_source_modified": False,
        "consultation_count": 1,
        "gpt_calls": 1,
        "external_calls": 1,
        "path": DISCOVERY_REPORT_RELATIVE_PATH.as_posix(),
        "available_assets_digest": assets_after_verification.get("assets_digest", ""),
        "asset_pack_digest": asset_pack_after_verification.get("pack_digest", ""),
        "asset_pack_id": asset_pack_after_verification.get("pack_id", ""),
        "available_assets_used": copy.deepcopy(asset_pack_after_verification.get("asset_ids", [])),
        "candidate_zero_asset_id": (
            CANDIDATE_USER_PROJECT_ASSET_ID
            if any(item.get("asset_id") == CANDIDATE_USER_PROJECT_ASSET_ID for item in assets_after_verification.get("assets", []))
            else None
        ),
        "asset_layer_marker": ASSET_LAYER_MARKER,
    }
    # The report records provenance for the enriched post-verification
    # inventory and bounded pack, but never self-references its own digest or
    # stores the temporary clone/cache location.
    assets_resource = _canonical_resource_record(
        root,
        AVAILABLE_ASSETS_RELATIVE_PATH.as_posix(),
        AVAILABLE_ASSETS_RELATIVE_PATH.name,
        limit=MAX_REPORT_BYTES,
    )
    pack_resource = {
        "resource_id": str(asset_pack_after_verification.get("pack_id", "")),
        "logical_name": "ASSET_PACK",
        "path": "<bounded-metadata-pack>",
        "digest": str(asset_pack_after_verification.get("pack_digest", "")),
        "bytes": len(canonical_json(asset_pack_after_verification).encode("utf-8")),
        "asset_ids": [str(item) for item in asset_pack_after_verification.get("asset_ids", [])],
    }
    result["resource_ids"] = [assets_resource["resource_id"], pack_resource["resource_id"]]
    result["resource_digests"] = {
        "available_assets": assets_resource["digest"],
        "asset_pack": pack_resource["digest"],
    }
    result["resource_provenance"] = {
        "available_assets": assets_resource,
        "asset_pack": pack_resource,
    }
    if isinstance(transport, Mapping):
        # Keep only receipt-safe transport metadata in the canonical report;
        # the full prompt and response remain transient bridge artifacts.
        result["bridge_receipt"] = {
            "consultation_id": transport.get("consultation_id"),
            "conversation_id": transport.get("conversation_id"),
            "receipt_path": transport.get("receipt_path"),
            "project_url": transport.get("project_url", project_url),
            "mode": transport.get("mode", CONSULTATION_MODE_FRESH),
            "request_count": 1,
            "receipt": copy.deepcopy(transport.get("receipt", {})),
        }
    # No raw response/prompt can reach this object.  The safety check also
    # prevents accidental confidence fields introduced by a future adapter.
    _assert_safe(result)
    report_path = root / DISCOVERY_REPORT_RELATIVE_PATH
    # Update retention before the report is committed so the canonical report
    # can include the manifest binding and a complete persistent inventory.
    manifest = _update_retention(root, checked_brief=checked_brief, context=evidence["project_context"], report_path=report_path)
    result["retention_manifest_path"] = ARTIFACT_RETENTION_MANIFEST_RELATIVE_PATH.as_posix()
    result["retention_manifest_digest"] = manifest["digest"]
    result["marker"] = DISCOVERY_REPORT_MARKER
    inventory = {
        path.relative_to(root).as_posix()
        for path in (root / ".research").rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    inventory.add(DISCOVERY_REPORT_RELATIVE_PATH.as_posix())
    result["persistent_inventory"] = sorted(inventory)
    checked_result = _verify_report(result)
    _atomic_json(report_path, checked_result)
    return checked_result


def run_project_discovery(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return discover_project(*args, **kwargs)


def perform_project_discovery(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return discover_project(*args, **kwargs)


def load_discovery_report(project_root: str | os.PathLike[str] = ".") -> dict[str, Any]:
    root = resolve_project_root(project_root)
    report = _read_json(root / DISCOVERY_REPORT_RELATIVE_PATH, "DISCOVERY_REPORT_NOT_FOUND")
    if report is None:
        raise ProjectDiscoveryError("DISCOVERY_REPORT_NOT_FOUND", "no discovery report exists")
    checked = _verify_report(report)
    brief = load_project_brief(root)
    if not isinstance(brief, Mapping):
        raise ProjectDiscoveryError("BRIEF_NOT_FOUND", "an approved project brief is required")
    try:
        approved = validate_project_brief(brief, root)
    except ProjectIntakeError as exc:
        raise ProjectDiscoveryError("BRIEF_INVALID", "canonical project brief failed validation") from exc
    if approved.get("state") != "APPROVED" or approved.get("status") != "APPROVED":
        raise ProjectDiscoveryError("BRIEF_NOT_APPROVED", "Project Discovery requires state=APPROVED")
    if checked.get("project_id") != str(approved.get("project_id")):
        raise ProjectDiscoveryError("DISCOVERY_PROJECT_MISMATCH", "discovery report is not bound to this project")
    return checked


def verify_discovery_report(project_root: str | os.PathLike[str] = ".") -> dict[str, Any]:
    report = load_discovery_report(project_root)
    return {
        "passed": True,
        "marker": DISCOVERY_MARKER,
        "path": DISCOVERY_REPORT_RELATIVE_PATH.as_posix(),
        "project_id": report["project_id"],
        "evidence_digest": report["evidence_digest"],
        "consultation_mode": report["consultation"]["mode"],
        "project_url": report["consultation"]["project_url"],
        "candidate_count": len(report["candidate_repositories"]),
        "persistent_inventory": sorted(report.get("persistent_inventory", [])),
    }


class ProjectDiscovery:
    """Object facade for callers that prefer ``service.run()``."""

    def __init__(self, project_root: str | os.PathLike[str] = ".", *, consultant: Any = None, verifier: Any = None) -> None:
        self.root = resolve_project_root(project_root)
        self.consultant = consultant
        self.verifier = verifier

    def run(self, **kwargs: Any) -> dict[str, Any]:
        return discover_project(self.root, consultant=kwargs.pop("consultant", self.consultant), verifier=kwargs.pop("verifier", self.verifier), **kwargs)

    def discover(self, **kwargs: Any) -> dict[str, Any]:
        return self.run(**kwargs)

    def load(self) -> dict[str, Any]:
        return load_discovery_report(self.root)


# Friendly aliases.
discover = discover_project
project_discovery = discover_project
load_report = load_discovery_report
verify_report = verify_discovery_report


__all__ = [
    "ANTI_TUNNEL_RESEARCH_RULE",
    "CONSULTATION_MODE_FRESH",
    "DISCOVERY_MARKER",
    "DISCOVERY_REPORT_MARKER",
    "DISCOVERY_REPORT_FILENAME",
    "DISCOVERY_REPORT_PATH",
    "DISCOVERY_REPORT_RELATIVE_PATH",
    "DISCOVERY_REPORT_SCHEMA_VERSION",
    "DISCOVERY_SEMANTIC_BRANCHES",
    "DISCOVERY_STATUS",
    "DiscoveryConsultant",
    "DiscoveryError",
    "FRESH",
    "GitRepositoryVerifier",
    "MAX_ALTERNATIVES",
    "MAX_CANDIDATE_REPOSITORIES",
    "MAX_RESPONSE_BYTES",
    "MAX_RESPONSE_PROSE_LENGTH",
    "MAX_RESPONSE_TEXT_LENGTH",
    "ProjectDiscovery",
    "ProjectDiscoveryError",
    "ProjectDiscoveryFailure",
    "RepositoryVerifier",
    "build_discovery_evidence",
    "build_approved_discovery_packet",
    "build_approved_packet",
    "build_discovery_prompt",
    "canonicalize_repo_url",
    "discover",
    "discover_project",
    "load_discovery_report",
    "load_report",
    "normalize_discovery_response",
    "perform_project_discovery",
    "project_discovery",
    "prepare_discovery_packet",
    "run_project_discovery",
    "sanitize_project_url",
    "verify_discovery_report",
    "verify_report",
    "validate_discovery_report",
]
