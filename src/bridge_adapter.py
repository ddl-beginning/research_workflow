"""Bounded supervisor-to-browser-bridge adaptation.

The browser bridge deliberately owns browser, authentication, navigation, and
receipt creation.  This module only adapts the bridge's small result envelope
to the supervisor's Step 13/14 consultant protocol.  It is intentionally
one-shot: a malformed envelope is rejected locally and is never retried.

Both spellings emitted by the existing bridge are accepted at this boundary:
``responseText``/``response_text``, ``consultationId``/``consultation_id`` and
``requestCount``/``request_count``.  Raw structured consultant responses are
not treated as bridge envelopes; callers can continue to use deterministic
offline consultants in the existing tests.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit


MAX_ENVELOPE_TEXT = 250_000
MAX_METADATA_DEPTH = 8
MAX_METADATA_LIST = 64
MAX_METADATA_STRING = 12_000

_MISSING = object()
_ENVELOPE_KEYS = frozenset(
    {
        "responseText",
        "response_text",
        "response",
        "consultationId",
        "consultation_id",
        "requestCount",
        "request_count",
        "receipt",
        "receiptPath",
        "receipt_path",
    }
)
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
        "conversation_history",
        "chat_history",
    }
)

# Canonical workflow files are authoritative local records and therefore may
# contain fields that are intentionally not suitable for a GPT-visible
# attachment (for example ``repository_root``).  The Node bridge correctly
# rejects those values; this adapter must give it a bounded *representation*
# rather than weakening that guard or uploading the local record verbatim.
_CANONICAL_BRIEF_FIELDS = frozenset(
    {
        "schema_version",
        "project_id",
        "project_identity",
        "repository_root",
        "intake_mode",
        "mode",
        "state",
        "status",
        "brief_status",
        "next_action",
        "rough_requirement",
        "brief",
        "revision",
        "gpt_calls",
        "external_calls",
    }
)
# These are workflow-owned canonical records.  They are deliberately an
# explicit allow-list: the bridge adapter must never discover arbitrary files
# by walking the user's repository.  The two Step 13/14 records are included
# only when they already exist, so Discovery remains a pre-report packet while
# the subsequent Blueprint packet can carry the completed report and compact
# feasibility record.
_CANONICAL_RESOURCE_SPECS = (
    (".research/PROJECT_BRIEF.json", "PROJECT_BRIEF.json"),
    (".research/PROJECT_CONTEXT.md", "PROJECT_CONTEXT.md"),
    (".research/AVAILABLE_ASSETS.json", "AVAILABLE_ASSETS.json"),
    (".research/LOCAL_PROJECT_PROFILE.md", "LOCAL_PROJECT_PROFILE.md"),
    (".research/discovery/DISCOVERY_REPORT.json", "DISCOVERY_REPORT.json"),
    (".research/blueprint/CODEX_FEASIBILITY.json", "CODEX_FEASIBILITY.json"),
)
_REPOSITORY_PATH_KEYS = frozenset(
    {
        "repository_root",
        "root_dir",
        "profile_dir",
        "workspace_root",
        "working_directory",
        "cwd",
        # Transport receipts are local provenance records.  A completed
        # Discovery report may embed the receipt location for auditability;
        # never expose that absolute path in a subsequent Blueprint packet.
        "receipt_path",
    }
)
_REDACTED = "<REDACTED>"
_LOCAL_REPOSITORY_ROOT = "<LOCAL_REPOSITORY_ROOT>"
_LOCAL_PATH = "<LOCAL_PATH>"
_SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|$)", re.IGNORECASE | re.DOTALL),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
_SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?:^|[\"']?)(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd|secret|cookie|cookies|token|session(?:[_-]?id)?)"
    r"(?:[\"']?\s*[:=])",
    re.IGNORECASE,
)
# Keep these patterns deliberately aligned with the bridge's absolute-path
# detector.  They only redact the *content* that is about to be attached;
# bridge path validation remains unchanged and still runs afterwards.
_ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s\"'`),;}\]]*"),
    re.compile(r"\\\\[^\\/\s]+[\\/][^\\/\s\"'`),;}\]]*"),
    re.compile(r"\bfile:///[^\s\"'`)]*", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9_:/])/(?![/\s])[^\\/\s\"'`),;}\]]*"),
)
_FAILURE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_.-]{0,127}$")
_EXCEPTION_CLASS_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")
_FAILURE_PHASES = frozenset(
    {
        "CONTEXT_PACK_BUILD",
        "BRIDGE_RUNNER",
        "BRIDGE_SUBPROCESS",
        "BRIDGE_RESULT",
    }
)


class BridgeEnvelopeError(RuntimeError):
    """A stable, fail-closed project bridge envelope error."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = str(code)
        self.details = dict(details or {})
        super().__init__(message)


def _stable_failure_code(value: Any, *, default: str = "BRIDGE_RUNNER_FAILED") -> str:
    """Keep only a bounded bridge error token, never an exception message."""

    if not isinstance(value, str):
        return default
    candidate = value.strip().upper()
    if not _FAILURE_CODE_PATTERN.fullmatch(candidate):
        return default
    if candidate.casefold().replace("-", "_") in _SENSITIVE_KEYS:
        return default
    return candidate


def _stable_exception_class(value: Any) -> str:
    candidate = value if isinstance(value, str) else ""
    return candidate if _EXCEPTION_CLASS_PATTERN.fullmatch(candidate) else "UnknownException"


def _known_request_count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value in {0, 1} else None


def _bridge_forensic_details(
    error: BaseException,
    *,
    phase: str,
    fallback_code: str,
) -> dict[str, Any]:
    """Extract a tiny safe diagnostic from a runner/pack exception."""

    raw_details = getattr(error, "details", None)
    details = raw_details if isinstance(raw_details, Mapping) else {}
    code = _stable_failure_code(getattr(error, "code", None), default=fallback_code)
    request_count = _known_request_count(details.get("request_count", details.get("requestCount")))
    detail_phase = details.get("failure_phase")
    if not isinstance(detail_phase, str) or detail_phase not in _FAILURE_PHASES:
        detail_phase = phase
    return {
        "failure_phase": detail_phase,
        "exception_class": _stable_exception_class(type(error).__name__),
        "error_code": code,
        "request_count": request_count,
        "attempt_count": 1,
    }


def is_bridge_envelope(value: Any) -> bool:
    """Return whether *value* looks like a transport envelope.

    Discovery and blueprint still accept raw offline response objects.  The
    distinction is therefore explicit instead of assuming every mapping is a
    bridge result.
    """

    return isinstance(value, Mapping) and any(key in value for key in _ENVELOPE_KEYS)


def _value(mapping: Mapping[str, Any], *names: str, default: Any = _MISSING) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    if default is _MISSING:
        return None
    return default


def _text(value: Any, field: str, *, required: bool = True, maximum: int = MAX_METADATA_STRING) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", f"{field} must be text")
    result = value.strip()
    if required and not result:
        raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", f"{field} must be non-empty")
    if len(result) > maximum or "\x00" in result:
        raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", f"{field} exceeds its bound")
    return result


def normalize_project_url(value: Any) -> str:
    """Validate the minimum safe shape for a configured ChatGPT target.

    The browser bridge owns the product-specific page check.  This boundary
    only rejects unsafe origins and ambiguous URL spellings; it deliberately
    does not infer a Project identifier or hard-code a Project route shape.
    """

    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise BridgeEnvelopeError("PROJECT_URL_INVALID", "project URL is not a safe URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise BridgeEnvelopeError("PROJECT_URL_INVALID", "project URL syntax is invalid") from exc
    if (
        parsed.scheme.casefold() != "https"
        or hostname is None
        or hostname.casefold() != "chatgpt.com"
        or (port is not None and port != 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
        or not parsed.path
        or parsed.path == "/"
        or "\\" in parsed.path
        or any(part in {".", ".."} for part in parsed.path.split("/"))
        or any(ord(character) < 0x20 or ord(character) == 0x7f for character in parsed.path)
    ):
        raise BridgeEnvelopeError("PROJECT_URL_INVALID", "project URL must use https://chatgpt.com and identify a non-root target")
    raw_authority_end = value.find("/", value.find("://") + 3)
    raw_path = value[raw_authority_end:] if raw_authority_end >= 0 else ""
    raw_without_trailing = raw_path[:-1] if raw_path.endswith("/") else raw_path
    parsed_without_trailing = parsed.path[:-1] if parsed.path.endswith("/") else parsed.path
    if not raw_path or raw_without_trailing != parsed_without_trailing:
        raise BridgeEnvelopeError("PROJECT_URL_INVALID", "project URL path is ambiguous")
    return f"https://chatgpt.com{parsed_without_trailing}"


def _safe_metadata(value: Any, field: str = "metadata", *, depth: int = MAX_METADATA_DEPTH) -> Any:
    """Copy bounded receipt metadata while rejecting sensitive material."""

    if depth < 0:
        raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", f"{field} is too deeply nested")
    if isinstance(value, Mapping):
        if len(value) > MAX_METADATA_LIST:
            raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", f"{field} has too many fields")
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if len(key) > 256 or "\x00" in key:
                raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", f"{field} has an invalid field name")
            if key.casefold().replace("-", "_") in _SENSITIVE_KEYS:
                raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", f"{field}.{key} is not allowed")
            result[key] = _safe_metadata(child, f"{field}.{key}", depth=depth - 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_METADATA_LIST:
            raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", f"{field} has too many values")
        return [_safe_metadata(item, f"{field}[]", depth=depth - 1) for item in value]
    if isinstance(value, str):
        if len(value) > MAX_METADATA_STRING or "\x00" in value:
            raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", f"{field} exceeds its bound")
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", f"{field} has an unsupported value")


def normalize_bridge_envelope(
    value: Mapping[str, Any],
    *,
    expected_project_url: str | None = None,
    expected_mode: str | None = None,
    require_receipt: bool = False,
) -> dict[str, Any]:
    """Normalize and verify one real bridge result.

    The returned object contains only canonical aliases and bounded receipt
    metadata.  It never includes the original mapping, prompt, or raw response
    under another key.
    """

    if not isinstance(value, Mapping):
        raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", "bridge result must be an object")

    # A few bridge wrappers return ``{result: {...}}`` or
    # ``{data: {...}, receipt: {...}}``.  Prefer the nested transport fields
    # when the outer object has no response/id/count of its own, while keeping
    # outer receipt/scope metadata as supplemental fields.  This handles the
    # real bridge's occasional wrapper without allowing a forged outer
    # response to override the nested transport payload.
    source: Mapping[str, Any] = value
    nested = value.get("result", value.get("data"))
    direct_transport_keys = {
        "responseText",
        "response_text",
        "response",
        "consultationId",
        "consultation_id",
        "requestCount",
        "request_count",
    }
    if (
        isinstance(nested, Mapping)
        and is_bridge_envelope(nested)
        and not any(key in value for key in direct_transport_keys)
    ):
        merged = dict(nested)
        for key, child in value.items():
            if key not in {"result", "data"} and key not in merged:
                merged[key] = child
        source = merged
    response_text = _value(source, "response_text", "responseText", "response")
    if not isinstance(response_text, str) or not response_text.strip():
        raise BridgeEnvelopeError("BRIDGE_RESPONSE_EMPTY", "bridge returned no response text")
    if len(response_text) > MAX_ENVELOPE_TEXT or "\x00" in response_text:
        raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", "bridge response exceeds its bound")
    consultation_id = _text(_value(source, "consultation_id", "consultationId"), "consultation_id")
    request_count = _value(source, "request_count", "requestCount")
    if isinstance(request_count, bool) or not isinstance(request_count, int) or request_count != 1:
        raise BridgeEnvelopeError(
            "BRIDGE_REQUEST_BUDGET_INVALID",
            "a project consultation must consume exactly one bridge request",
            details={"request_count": request_count},
        )

    receipt_value = _value(source, "receipt", default={})
    if receipt_value is None:
        receipt_value = {}
    if not isinstance(receipt_value, Mapping):
        raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", "bridge receipt must be an object")
    if require_receipt and not receipt_value:
        raise BridgeEnvelopeError("BRIDGE_RECEIPT_MISSING", "a completed bridge receipt is required")
    receipt = _safe_metadata(dict(receipt_value), "bridge receipt")
    if not isinstance(receipt, dict):  # pragma: no cover - guarded above
        raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", "bridge receipt must be an object")

    status = _value(source, "status", default=None)
    receipt_status = receipt.get("status")
    if status is not None and not isinstance(status, str):
        raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", "bridge status must be text")
    if receipt_status is not None and not isinstance(receipt_status, str):
        raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", "bridge receipt status must be text")
    for candidate in (status, receipt_status):
        if isinstance(candidate, str) and candidate.casefold() != "complete":
            raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", "bridge receipt is not complete")

    receipt_id = _text(_value(receipt, "consultation_id", "consultationId", default=None), "receipt.consultation_id", required=False)
    if receipt_id is not None and receipt_id != consultation_id:
        raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", "bridge receipt consultation id does not match result")
    receipt_count = _value(receipt, "request_count", "requestCount", default=None)
    if receipt_count is not None and (isinstance(receipt_count, bool) or receipt_count != 1):
        raise BridgeEnvelopeError("BRIDGE_REQUEST_BUDGET_INVALID", "bridge receipt request count is not one")

    source_project = _value(source, "project_url", "projectUrl", default=None)
    receipt_project = _value(receipt, "project_url", "projectUrl", default=None)
    normalized_source_project = normalize_project_url(source_project) if source_project is not None else None
    normalized_receipt_project = normalize_project_url(receipt_project) if receipt_project is not None else None
    if normalized_source_project and normalized_receipt_project and normalized_source_project != normalized_receipt_project:
        raise BridgeEnvelopeError("PROJECT_SCOPE_MISMATCH", "bridge result and receipt project bindings differ")
    normalized_project = normalized_source_project or normalized_receipt_project
    expected_project = normalize_project_url(expected_project_url) if expected_project_url is not None else None
    if expected_project is not None:
        if require_receipt and normalized_receipt_project is None:
            raise BridgeEnvelopeError("PROJECT_SCOPE_MISSING", "bridge receipt has no verified project binding")
        if normalized_project is None:
            raise BridgeEnvelopeError("PROJECT_SCOPE_MISSING", "bridge result has no verified project binding")
        if normalized_project != expected_project:
            raise BridgeEnvelopeError("PROJECT_SCOPE_MISMATCH", "bridge result belongs to another ChatGPT Project")

    target_fields = {
        "chatgpt_target_mode",
        "chatgpt_target_url_digest",
        "chatgpt_target_origin",
        "chatgpt_project_target_verified",
        "fresh_project_chat_created",
    }
    if any(field in receipt for field in target_fields):
        if not target_fields.issubset(receipt):
            raise BridgeEnvelopeError("PROJECT_TARGET_METADATA_INVALID", "bridge receipt target metadata is incomplete")
        target_mode = receipt.get("chatgpt_target_mode")
        if target_mode not in {"PROJECT", "DEFAULT"}:
            raise BridgeEnvelopeError("PROJECT_TARGET_METADATA_INVALID", "bridge receipt target mode is invalid")
        if receipt.get("chatgpt_target_origin") != "https://chatgpt.com":
            raise BridgeEnvelopeError("PROJECT_TARGET_METADATA_INVALID", "bridge receipt target origin is invalid")
        target_verified = receipt.get("chatgpt_project_target_verified")
        if target_verified not in {"YES", "NO"}:
            raise BridgeEnvelopeError("PROJECT_TARGET_METADATA_INVALID", "bridge receipt target verification is invalid")
        fresh_created = receipt.get("fresh_project_chat_created")
        if fresh_created not in {"YES", "NO"}:
            raise BridgeEnvelopeError("PROJECT_TARGET_METADATA_INVALID", "bridge receipt fresh-chat marker is invalid")
        digest = receipt.get("chatgpt_target_url_digest")
        if target_mode == "PROJECT":
            if normalized_receipt_project is None or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise BridgeEnvelopeError("PROJECT_TARGET_METADATA_INVALID", "Project receipt target digest is invalid")
            if digest != hashlib.sha256(normalized_receipt_project.encode("utf-8")).hexdigest():
                raise BridgeEnvelopeError("PROJECT_TARGET_METADATA_INVALID", "Project receipt target digest does not match its URL")
            if target_verified != "YES":
                raise BridgeEnvelopeError("PROJECT_TARGET_METADATA_INVALID", "Project receipt target was not verified")
            if fresh_created == "YES" and (
                receipt.get("mode") != "fresh"
                or receipt.get("status") != "complete"
                or receipt.get("conversation_validated") is not True
                or not isinstance(receipt.get("conversation_id"), str)
                or not receipt.get("conversation_id")
            ):
                raise BridgeEnvelopeError("PROJECT_TARGET_METADATA_INVALID", "Project fresh-chat marker is not bound to a completed conversation")
        elif digest is not None or normalized_receipt_project is not None or target_verified != "NO":
            raise BridgeEnvelopeError("PROJECT_TARGET_METADATA_INVALID", "default receipt carries Project target state")
        if target_mode == "DEFAULT" and fresh_created != "NO":
            raise BridgeEnvelopeError("PROJECT_TARGET_METADATA_INVALID", "default receipt carries a Project fresh-chat marker")

    # Receipts produced for a Project-scoped invocation must carry the same
    # verified scope proof that the bridge uses for continuation.  A receipt
    # without any Project fields remains a legacy receipt and is intentionally
    # accepted for existing unscoped Stage 9 callers.
    scope_fields_present = (
        normalized_source_project is not None
        or normalized_receipt_project is not None
        or any(
            key in receipt
            for key in (
                "project_scope_requested",
                "projectScopeRequested",
                "project_scope_verified",
                "projectScopeVerified",
                "project_scope_evidence",
                "projectScopeEvidence",
            )
        )
    )
    scope_requested = _value(
        receipt,
        "project_scope_requested",
        "projectScopeRequested",
        default=_MISSING,
    )
    scope_verified = _value(
        receipt,
        "project_scope_verified",
        "projectScopeVerified",
        default=_MISSING,
    )
    scope_evidence = _value(
        receipt,
        "project_scope_evidence",
        "projectScopeEvidence",
        default=_MISSING,
    )
    if scope_fields_present:
        if normalized_receipt_project is None:
            raise BridgeEnvelopeError("PROJECT_SCOPE_MISSING", "scoped receipt has no project URL")
        if scope_requested is not True:
            raise BridgeEnvelopeError("PROJECT_SCOPE_INVALID", "scoped receipt was not marked as requested")
        if scope_verified is not True:
            raise BridgeEnvelopeError("PROJECT_SCOPE_INVALID", "scoped receipt was not verified")
        if not isinstance(scope_evidence, Mapping) or not scope_evidence:
            raise BridgeEnvelopeError("PROJECT_SCOPE_INVALID", "scoped receipt has no bounded scope evidence")
        initial_navigation = _value(scope_evidence, "initial_navigation", "initialNavigation", default=None)
        if not isinstance(initial_navigation, Mapping) or not initial_navigation:
            raise BridgeEnvelopeError("PROJECT_SCOPE_INVALID", "scoped receipt lacks initial navigation evidence")
        if (
            _value(initial_navigation, "matched", default=None) is not True
            or _value(initial_navigation, "verified", default=None) is not True
        ):
            raise BridgeEnvelopeError("PROJECT_SCOPE_INVALID", "scoped receipt navigation evidence is not verified")
        evidence_requested_url = _value(
            initial_navigation,
            "requested_url",
            "requestedUrl",
            default=None,
        )
        evidence_landed_url = _value(
            initial_navigation,
            "landed_url",
            "landedUrl",
            default=None,
        )
        try:
            normalized_evidence_requested = normalize_project_url(evidence_requested_url)
            normalized_evidence_landed = normalize_project_url(evidence_landed_url)
        except BridgeEnvelopeError as exc:
            raise BridgeEnvelopeError("PROJECT_SCOPE_INVALID", "scoped receipt navigation evidence has an invalid URL") from exc
        if (
            normalized_evidence_requested != normalized_receipt_project
            or normalized_evidence_landed != normalized_receipt_project
        ):
            raise BridgeEnvelopeError("PROJECT_SCOPE_MISMATCH", "scoped receipt navigation evidence does not match its project")

    source_mode = _value(source, "mode", default=None)
    receipt_mode = receipt.get("mode")
    effective_mode = source_mode or receipt_mode
    if effective_mode is not None:
        if not isinstance(effective_mode, str):
            raise BridgeEnvelopeError("BRIDGE_RESULT_INVALID", "bridge mode must be text")
        effective_mode = effective_mode.casefold()
    if expected_mode is not None:
        expected_mode_value = _text(expected_mode, "expected_mode", maximum=32)
        assert expected_mode_value is not None
        expected_mode_value = expected_mode_value.casefold()
        if effective_mode != expected_mode_value:
            raise BridgeEnvelopeError("BRIDGE_MODE_MISMATCH", "bridge mode does not match the requested consultation")

    receipt_path = _text(
        _value(
            source,
            "receipt_path",
            "receiptPath",
            default=_value(receipt, "receipt_path", "receiptPath", default=None),
        ),
        "receipt_path",
        required=False,
        maximum=4096,
    )
    conversation_id = _text(
        _value(source, "conversation_id", "conversationId", default=receipt.get("conversation_id", receipt.get("conversationId"))),
        "conversation_id",
        required=False,
        maximum=512,
    )
    normalized: dict[str, Any] = {
        "consultation_id": consultation_id,
        "request_count": 1,
        "response_text": response_text,
        "receipt": receipt,
    }
    if receipt_path is not None:
        normalized["receipt_path"] = receipt_path
    if conversation_id is not None:
        normalized["conversation_id"] = conversation_id
    if normalized_project is not None:
        normalized["project_url"] = normalized_project
    if effective_mode is not None:
        normalized["mode"] = effective_mode
    if scope_fields_present:
        normalized["project_scope_requested"] = True
        normalized["project_scope_verified"] = True
        normalized["project_scope_evidence"] = copy.deepcopy(dict(scope_evidence))
    return normalized


def _clip_value(value: Any, *, depth: int = 5, max_string: int = 6_000) -> Any:
    """Make structured Step 13/14 evidence safe for a generated pack."""

    if depth < 0:
        return "<truncated>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if key_text.casefold().replace("-", "_") in _SENSITIVE_KEYS:
                continue
            result[key_text] = _clip_value(child, depth=depth - 1, max_string=max_string)
        return result
    if isinstance(value, (list, tuple)):
        return [_clip_value(item, depth=depth - 1, max_string=max_string) for item in list(value)[:32]]
    if isinstance(value, str):
        return value[:max_string]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:max_string]


def _sanitized_attachment_text(root: Path, source_path: str, logical_name: str, raw: str) -> str:
    """Return a deterministic GPT-visible view of one canonical file.

    The on-disk canonical files remain untouched: they are local provenance
    records and may legitimately carry the resolved repository identity.  An
    inline ``content`` descriptor is used for the bridge instead of a
    ``sourcePath`` descriptor so that the browser bridge never reads or stages
    those local-only fields verbatim.  This is intentionally conservative:
    sensitive JSON fields are omitted, sensitive Markdown assignment lines are
    omitted, and path-like values are replaced with controlled placeholders.
    """

    path_key = source_path.casefold().replace("\\", "/")

    def key_name(value: Any) -> str:
        return str(value).strip().casefold().replace("-", "_")

    sensitive_keys = _SENSITIVE_KEYS | frozenset(
        {
            "authorization",
            "credentials",
            "github_token",
            "private_key",
            "private_key_data",
            "refresh_token",
            "slack_token",
        }
    )

    def sanitize_text(value: str) -> str:
        text = str(value)
        # Replace the known repository spelling first.  This avoids leaking a
        # path prefix when a later generic pattern sees a path component.
        root_variants = {
            root.as_posix(),
            str(root),
            str(root).replace("\\", "/"),
            str(root).replace("/", "\\"),
        }
        for variant in sorted((item for item in root_variants if item), key=len, reverse=True):
            text = re.sub(re.escape(variant), _LOCAL_REPOSITORY_ROOT, text, flags=re.IGNORECASE)
        for pattern in _SECRET_VALUE_PATTERNS:
            text = pattern.sub(_REDACTED, text)
        # A secret assignment is not useful as GPT evidence and keeping the
        # key would still trigger the bridge's assignment detector even when
        # its value is replaced.  Drop the whole line instead.
        lines = [line for line in text.splitlines() if not _SENSITIVE_ASSIGNMENT_PATTERN.search(line)]
        text = "\n".join(lines)
        for pattern in _ABSOLUTE_PATH_PATTERNS:
            text = pattern.sub(_LOCAL_PATH, text)
        return text

    def sanitize_json(value: Any, *, field: str = "") -> Any:
        normalized_field = key_name(field)
        if normalized_field in sensitive_keys:
            return _MISSING
        if normalized_field in _REPOSITORY_PATH_KEYS:
            return _LOCAL_REPOSITORY_ROOT
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for raw_key, child in value.items():
                key = str(raw_key)
                child_value = sanitize_json(child, field=key)
                if child_value is not _MISSING:
                    result[key] = child_value
            return result
        if isinstance(value, (list, tuple)):
            return [
                child
                for child in (sanitize_json(item, field=field) for item in value)
                if child is not _MISSING
            ]
        if isinstance(value, str):
            return sanitize_text(value)
        return value

    if path_key.endswith("project_brief.json"):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, Mapping):
            # The brief's timestamps, approval prose, and repository fact
            # cache are local bookkeeping.  Keep the approved requirement and
            # stable identity/state fields needed by Discovery/Blueprint.
            parsed = {
                key: value
                for key, value in parsed.items()
                if key_name(key) in _CANONICAL_BRIEF_FIELDS
            }
            safe = sanitize_json(parsed)
            if safe is not _MISSING:
                return json.dumps(safe, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    elif path_key.endswith(".json"):
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            safe = sanitize_json(parsed)
            if safe is not _MISSING:
                return json.dumps(safe, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    return sanitize_text(raw)


def _canonical_file_content(root: Path, source_path: str, logical_name: str) -> str:
    path = root / Path(source_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise BridgeEnvelopeError(
            "CONTEXT_PACK_CANONICAL_READ_FAILED",
            f"canonical attachment {logical_name} could not be read safely",
        ) from exc
    content = _sanitized_attachment_text(root, source_path, logical_name, raw)
    # Keep this adapter fail-closed if a future sanitizer change misses a
    # pattern.  The bridge's own guard is still authoritative and is not
    # relaxed by this preflight.
    if any(pattern.search(content) for pattern in _SECRET_VALUE_PATTERNS):
        raise BridgeEnvelopeError(
            "CONTEXT_PACK_CANONICAL_SECRET_REJECTED",
            f"canonical attachment {logical_name} still contains secret-like material",
        )
    if any(pattern.search(content) for pattern in _ABSOLUTE_PATH_PATTERNS):
        raise BridgeEnvelopeError(
            "CONTEXT_PACK_CANONICAL_PATH_REJECTED",
            f"canonical attachment {logical_name} still contains an absolute local path",
        )
    return content


def _resource_id(logical_name: str) -> str:
    """Return a stable, path-independent id for one canonical resource."""

    # The id is intentionally derived from the logical name, never from the
    # user's absolute checkout path.  This makes receipts/reviews comparable
    # across disposable worktrees without exposing local filesystem identity.
    slug = re.sub(r"[^A-Za-z0-9]+", "-", logical_name).strip("-").casefold()
    return f"resource-{slug or 'canonical'}"


def _canonical_file_descriptors(root: Path) -> list[dict[str, Any]]:
    """Select bounded, sanitized views of canonical project assets.

    ``sourcePath`` is deliberately not sent for these files.  The local
    canonical record is allowed to contain repository identity/provenance,
    while the bridge attachment must contain only its sanitized, relative-path
    representation.
    """

    descriptors: list[dict[str, Any]] = []
    for source_path, logical_name in _CANONICAL_RESOURCE_SPECS:
        path = root / Path(source_path)
        if path.is_file() and not path.is_symlink():
            content = _canonical_file_content(root, source_path, logical_name)
            descriptors.append(
                {
                    "content": content,
                    "logicalName": logical_name,
                    "sourceRelativePath": source_path,
                    "role": "source",
                    # These metadata fields are inline provenance only.  The
                    # Node bridge ignores unknown descriptor fields while
                    # staging the content, and no absolute path is included.
                    "resourceId": _resource_id(logical_name),
                    "resourceDigest": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "byteSize": len(content.encode("utf-8")),
                }
            )
    return descriptors


def build_project_context_pack(
    root_dir: str | os.PathLike[str],
    *,
    evidence: Mapping[str, Any] | None = None,
    packet: Mapping[str, Any] | None = None,
    project_goal: str = "",
    current_stage_goal: str = "",
    user_visible_goal: str = "",
    hard_constraints: Sequence[str] | None = None,
    current_blocker: str = "",
) -> dict[str, Any]:
    """Build a bounded Node ``buildContextPack`` specification.

    The source list is deliberately canonical and small.  It never walks the
    repository and never adds arbitrary source files just because they exist.
    """

    root = Path(root_dir).expanduser().resolve()
    source = evidence if isinstance(evidence, Mapping) else packet if isinstance(packet, Mapping) else {}
    goal = project_goal or str(source.get("real_goal", ""))
    brief = source.get("brief") if isinstance(source.get("brief"), Mapping) else {}
    visible_goal = user_visible_goal or str(brief.get("desired_outcome", brief.get("goal", "")))
    facts: list[str] = []
    if source.get("project_id"):
        facts.append(f"Project id: {source['project_id']}")
    if source.get("brief_digest"):
        facts.append(f"Approved brief digest: {source['brief_digest']}")
    if source.get("evidence_digest"):
        facts.append(f"Local evidence digest: {source['evidence_digest']}")
    latest = _clip_value(dict(source)) if source else {}
    canonical_descriptors = _canonical_file_descriptors(root)
    resource_provenance = [
        {
            "resource_id": str(item["resourceId"]),
            "logical_name": str(item["logicalName"]),
            "source_relative_path": str(item["sourceRelativePath"]),
            "digest": str(item["resourceDigest"]),
            "bytes": int(item["byteSize"]),
        }
        for item in canonical_descriptors
    ]
    pack: dict[str, Any] = {
        "mode": "fresh",
        "projectGoal": goal[:12_000],
        "currentStageGoal": (current_stage_goal or goal)[:12_000],
        "userVisibleGoal": visible_goal[:12_000],
        "establishedFacts": facts,
        "currentMethod": "bounded project asset discovery and blueprint review",
        "currentBlocker": current_blocker[:12_000],
        "protectedForbiddenScope": [".git", ".auth", ".consultations"],
        "hardConstraints": [str(item)[:4_000] for item in (hard_constraints or []) if str(item).strip()][:16],
        "previousRelevantDecisions": [],
        "latestResult": latest,
        "evidence": canonical_descriptors,
        "resource_ids": [item["resource_id"] for item in resource_provenance],
        "resource_digests": {item["resource_id"]: item["digest"] for item in resource_provenance},
        "resource_provenance": resource_provenance,
    }
    return pack


class ProjectScopedBridgeConsultant:
    """One-shot consultant adapter for Step 13/14 project-scoped calls."""

    def __init__(
        self,
        root_dir: str | os.PathLike[str],
        *,
        bridge_runner: Callable[..., Mapping[str, Any]],
        profile_dir: str | os.PathLike[str] | None = None,
        bridge_root: str | os.PathLike[str] | None = None,
        timeout_ms: int = 300_000,
        context_pack: Mapping[str, Any] | None = None,
    ) -> None:
        if not callable(bridge_runner):
            raise BridgeEnvelopeError("BRIDGE_RUNNER_INVALID", "bridge_runner must be callable")
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.bridge_runner = bridge_runner
        self.profile_dir = None if profile_dir is None else str(Path(profile_dir).expanduser().resolve())
        self.bridge_root = None if bridge_root is None else str(Path(bridge_root).expanduser().resolve())
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms < 1 or timeout_ms > 600_000:
            raise BridgeEnvelopeError("TIMEOUT_INVALID", "timeout_ms is outside its bounded range")
        self.timeout_ms = timeout_ms
        self.context_pack = copy.deepcopy(dict(context_pack)) if isinstance(context_pack, Mapping) else None
        self.calls = 0

    def consult(self, **kwargs: Any) -> Mapping[str, Any]:
        project_url_value = kwargs.get("project_url", kwargs.get("url"))
        project_url = normalize_project_url(project_url_value)
        mode = str(kwargs.get("mode", kwargs.get("consultation_mode", "fresh"))).casefold()
        if mode not in {"fresh", "normal", "continue"}:
            raise BridgeEnvelopeError("CONVERSATION_MODE_INVALID", "consultation mode must be fresh or normal continuation")
        continue_from = kwargs.get("continue_from")
        if mode == "fresh" and continue_from is not None:
            raise BridgeEnvelopeError("FRESH_CONTINUATION_INVALID", "fresh consultation cannot include continue_from")
        if mode in {"normal", "continue"} and (not isinstance(continue_from, str) or not continue_from.strip()):
            raise BridgeEnvelopeError("CONTINUATION_REQUIRED", "normal continuation requires continue_from")
        prompt = _text(kwargs.get("prompt"), "prompt", maximum=100_000)
        assert prompt is not None
        evidence = kwargs.get("evidence") if isinstance(kwargs.get("evidence"), Mapping) else None
        packet = kwargs.get("packet") if isinstance(kwargs.get("packet"), Mapping) else None
        if self.context_pack is not None:
            pack = copy.deepcopy(self.context_pack)
        else:
            try:
                pack = build_project_context_pack(
                    self.root_dir,
                    evidence=evidence,
                    packet=packet,
                    project_goal=str((evidence or packet or {}).get("real_goal", "")),
                )
                # Rebuild current canonical assets for each call. Conversation
                # lineage comes from the receipt, never a previous staging pack.
                pack["mode"] = "fresh" if mode == "fresh" else "normal"
            except BridgeEnvelopeError as exc:
                details = dict(exc.details)
                details.update(
                    _bridge_forensic_details(
                        exc,
                        phase="CONTEXT_PACK_BUILD",
                        fallback_code="CONTEXT_PACK_BUILD_FAILED",
                    )
                )
                raise BridgeEnvelopeError(exc.code, "project context pack could not be built", details=details) from exc
            except Exception as exc:  # pragma: no cover - filesystem/runtime-specific
                raise BridgeEnvelopeError(
                    "CONTEXT_PACK_BUILD_FAILED",
                    "project context pack could not be built",
                    details={
                        "failure_phase": "CONTEXT_PACK_BUILD",
                        "exception_class": _stable_exception_class(type(exc).__name__),
                        "error_code": "CONTEXT_PACK_BUILD_FAILED",
                        "request_count": None,
                        "attempt_count": 1,
                    },
                ) from exc
        call_kwargs: dict[str, Any] = {
            "mode": "fresh" if mode == "fresh" else "continue",
            "continue_from": continue_from,
            "context_pack": pack,
            "root_dir": str(self.root_dir),
            "profile_dir": self.profile_dir,
            "timeout_ms": self.timeout_ms,
            "project_url": project_url,
        }
        if self.bridge_root is not None:
            call_kwargs["bridge_root"] = self.bridge_root
        # Do not catch TypeError and retry: a real request may already have
        # crossed the browser boundary.  Signature inspection only prevents a
        # known-incompatible fake from issuing a request without project scope.
        try:
            signature = inspect.signature(self.bridge_runner)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            parameters = signature.parameters
            if "project_url" not in parameters and not any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
            ):
                raise BridgeEnvelopeError("BRIDGE_RUNNER_SIGNATURE_INVALID", "bridge runner must accept project_url")
        self.calls += 1
        try:
            # This is the sole bridge invocation.  In particular, do not catch
            # TypeError and call again: a wrapper may already have crossed the
            # browser boundary before reporting a duplicate keyword or other
            # argument error.
            raw = self.bridge_runner(prompt, **call_kwargs)
        except BridgeEnvelopeError as exc:
            details = dict(exc.details)
            details.update(
                _bridge_forensic_details(
                    exc,
                    phase="BRIDGE_RUNNER",
                    fallback_code="BRIDGE_RUNNER_FAILED",
                )
            )
            raise BridgeEnvelopeError(exc.code, "bridge runner failed; no retry was attempted", details=details) from exc
        except Exception as exc:  # pragma: no cover - transport-specific errors
            # Preserve a stable inner bridge code when one exists (for example
            # subprocess_bridge_runner's marker code), while exposing only a
            # bounded class/phase/count diagnostic to outer failure handling.
            code = _stable_failure_code(getattr(exc, "code", None), default="BRIDGE_RUNNER_FAILED")
            raw_details = getattr(exc, "details", None)
            count = _known_request_count(raw_details.get("request_count")) if isinstance(raw_details, Mapping) else None
            raise BridgeEnvelopeError(
                code,
                "bridge runner failed; no retry was attempted",
                details={
                    "failure_phase": (
                        raw_details.get("failure_phase")
                        if isinstance(raw_details, Mapping)
                        and raw_details.get("failure_phase") in _FAILURE_PHASES
                        else "BRIDGE_RUNNER"
                    ),
                    "exception_class": _stable_exception_class(type(exc).__name__),
                    "error_code": code,
                    "request_count": count,
                    "attempt_count": 1,
                },
            ) from exc
        normalized = normalize_bridge_envelope(
            raw,
            expected_project_url=project_url,
            expected_mode="fresh" if mode == "fresh" else "continue",
            require_receipt=True,
        )
        receipt_path = normalized.get("receipt_path")
        if receipt_path is not None:
            candidate = Path(receipt_path).expanduser()
            if not candidate.is_absolute():
                candidate = self.root_dir / candidate
            try:
                candidate.resolve().relative_to(self.root_dir)
            except ValueError as exc:
                raise BridgeEnvelopeError(
                    "BRIDGE_RECEIPT_INVALID",
                    "bridge receipt escaped the project root",
                ) from exc
        return normalized


# Compatibility aliases for thin callers and acceptance harnesses.
ProjectBridgeConsultant = ProjectScopedBridgeConsultant
ProjectScopedConsultant = ProjectScopedBridgeConsultant
BridgeConsultantAdapter = ProjectScopedBridgeConsultant
create_project_scoped_consultant = ProjectScopedBridgeConsultant
make_project_scoped_consultant = ProjectScopedBridgeConsultant
normalize_bridge_result = normalize_bridge_envelope
build_project_asset_pack = build_project_context_pack


__all__ = [
    "BridgeConsultantAdapter",
    "BridgeEnvelopeError",
    "ProjectBridgeConsultant",
    "ProjectScopedBridgeConsultant",
    "ProjectScopedConsultant",
    "build_project_context_pack",
    "build_project_asset_pack",
    "is_bridge_envelope",
    "create_project_scoped_consultant",
    "make_project_scoped_consultant",
    "normalize_bridge_envelope",
    "normalize_bridge_result",
    "normalize_project_url",
]
