"""Small, privacy-preserving lifecycle writer for the three human artifacts.

The Stage controller owns lifecycle state and the consultation adapter owns
transport.  This module is deliberately independent of both.  A caller gives
it a bounded Stage/consultation snapshot and the lifecycle event that made the
snapshot useful; the module renders the canonical, human-readable files at the
repository root.  The execution plan and decision log follow every supported
lifecycle event, while the human review file is reserved for reviewable
outcomes.

Only selected metadata is rendered.  In particular, prompts, responses,
transcripts, DOM data, credentials, and arbitrary nested payloads never cross
the rendering boundary.  The files have fixed names and are replaced through
a temporary file in the repository root so a caller cannot redirect writes to
an arbitrary path.
"""

from __future__ import annotations

import copy

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .design_review import DesignReviewError, normalize_design_summary
from .human_summary import build_human_presentation, render_human_presentation


STAGE_EXECUTION_PLAN_FILENAME = "STAGE_EXECUTION_PLAN.md"
RESEARCH_DECISION_LOG_FILENAME = "RESEARCH_DECISION_LOG.md"
HUMAN_REVIEW_FILENAME = "HUMAN_REVIEW.md"

STAGE_EXECUTION_PLAN_PATH = Path(STAGE_EXECUTION_PLAN_FILENAME)
RESEARCH_DECISION_LOG_PATH = Path(RESEARCH_DECISION_LOG_FILENAME)
HUMAN_REVIEW_PATH = Path(HUMAN_REVIEW_FILENAME)
CANONICAL_HUMAN_ARTIFACTS = (
    STAGE_EXECUTION_PLAN_FILENAME,
    RESEARCH_DECISION_LOG_FILENAME,
    HUMAN_REVIEW_FILENAME,
)

MAX_ARTIFACT_BYTES = 80_000
MAX_FIELD_LENGTH = 600
MAX_ID_LENGTH = 256
MAX_LIST_ITEMS = 32
MAX_HISTORY_ITEMS = 64
MAX_RENDERED_DECISION_HISTORY_ITEMS = 16
MAX_RENDERED_DECISION_FIELD_LENGTH = 240

# ``HUMAN_REVIEW.md`` is a review surface, rather than a general lifecycle
# journal.  These are the only lifecycle outcomes that make the current
# result something a person can act on.  Ordinary planning/start events and a
# normal CONTINUE consultation therefore leave an existing review untouched.
HUMAN_REVIEW_EVENT_KINDS = frozenset(
    {
        "stage_ready",
        "human_gate",
        "human_gate_requested",
        "human_gate_resolved",
        "approve_stage",
        "reject_stage",
        "closeout",
        "closeout_review",
        "fresh_review",
        "design_review_requested",
        "design_review_feedback",
        "design_review_accepted",
    }
)
HUMAN_REVIEW_DECISIONS = frozenset(
    {
        "STAGE_READY",
        "HUMAN_GATE",
        "APPROVE",
        "APPROVED",
        "REJECT",
        "REJECTED",
        "CLOSEOUT",
    }
)

# Route decisions are intentionally broader than the HUMAN_REVIEW trigger.
# CONTINUE is a valid, bounded consultation decision and belongs in the
# decision log, even though it does not by itself create or rewrite the human
# review surface.
ROUTE_DECISION_KINDS = frozenset(
    {
        "stage_ready",
        "human_gate",
        "human_gate_requested",
        "human_gate_resolved",
        "approve_stage",
        "reject_stage",
        "stop_stage",
        "closeout",
        "closeout_review",
        "fresh_review",
        "design_review_requested",
        "design_review_feedback",
        "design_review_accepted",
        "planner_decision",
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
        "html",
        "local_storage",
        "password",
        "passwd",
        "prompt",
        "prompt_text",
        "raw_dom",
        "raw_prompt",
        "raw_response",
        "response",
        "response_text",
        "secret",
        "session",
        "session_id",
        "session_storage",
        "storage_state",
        "token",
        "tokens",
        "transcript",
        "turns",
        "chat_history",
        "messages",
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
_HTTP_URL_PATTERN = re.compile(r"\bhttps?://[^\s<>()\"']+", re.IGNORECASE)
_FILE_URL_PATTERN = re.compile(r"\bfile://[^\s<>()\"']+", re.IGNORECASE)
_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![\w])(?:"
    r"[A-Za-z]:[\\/](?:[^\\/\s<>()\"']+(?:[\\/][^\\/\s<>()\"']*)*)?"
    r"|\\\\[^\\/\s<>()\"']+(?:[\\/][^\\/\s<>()\"']*)+"
    r")"
)
_POSIX_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![\w])/(?:[^\\/\s<>()\"']+(?:/[^\\/\s<>()\"']*)*)?"
)


class HumanArtifactError(RuntimeError):
    """Raised when an artifact event or canonical write is invalid."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


# Event names are intentionally a small compatibility set.  The controller's
# event names are included, together with descriptive names useful to a thin
# CLI/integration caller.  No new Stage state is introduced here.
ARTIFACT_EVENT_KINDS = frozenset(
    {
        "stage_planning",
        "stage_plan",
        "stage_planned",
        "stage_registered",
        "prepare_stage",
        "start_stage",
        "stage_start",
        "stage_started",
        "stage_planning_complete",
        "stage_ready",
        "stop_stage",
        "approve_stage",
        "reject_stage",
        "consultation_completed",
        "consultation_complete",
        "consultation_completion",
        "consultation_done",
        "consultation_result",
        "consult_completed",
        "review_completed",
        "human_gate",
        "human_gate_requested",
        "human_gate_resolved",
        "planner_decision",
        "closeout",
        "closeout_review",
        "fresh_review",
        "design_review_requested",
        "design_review_feedback",
        "design_review_accepted",
    }
)


def _key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _clip(value: Any, *, limit: int = MAX_FIELD_LENGTH) -> str | None:
    """Return a bounded safe string, omitting credentials and control data."""

    if not isinstance(value, str):
        return None
    text = value.replace("\x00", "").strip()
    if not text:
        return None
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        return None
    # Markdown control characters are harmless when escaped by the renderer,
    # but keeping a single line for metadata makes every output deterministic.
    text = re.sub(r"[\r\n\t]+", " ", text)
    return text[:limit]


def sanitize_bounded_evidence(value: Any, *, limit: int = MAX_FIELD_LENGTH) -> str | None:
    """Sanitize one bootstrap evidence string without touching relative paths.

    The projection may contain user-authored prose, so absolute references are
    replaced rather than rejected wholesale.  Relative workspace references
    such as ``src/add.py`` and ``.research/...`` do not match these patterns
    and remain available to a reviewer.  Secret detection stays fail-closed.
    """

    text = _clip(value, limit=limit)
    if text is None:
        return None
    text = _FILE_URL_PATTERN.sub("<LOCAL_PATH>", text)
    text = _HTTP_URL_PATTERN.sub("<EXTERNAL_URL>", text)
    text = _WINDOWS_ABSOLUTE_PATH_PATTERN.sub("<LOCAL_PATH>", text)
    text = _POSIX_ABSOLUTE_PATH_PATTERN.sub("<LOCAL_PATH>", text)
    return text[:limit] if text else None


def _optional_text(mapping: Mapping[str, Any], *names: str, limit: int = MAX_FIELD_LENGTH) -> str | None:
    for name in names:
        if name in mapping:
            value = _clip(mapping[name], limit=limit)
            if value is not None:
                return value
    return None


def _safe_id(value: Any) -> str | None:
    return _clip(value, limit=MAX_ID_LENGTH)


def _safe_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 1_000_000 else None


def _safe_string_list(value: Any, *, limit: int = MAX_LIST_ITEMS) -> list[str]:
    values: list[str] = []
    if isinstance(value, Mapping):
        raw_items = list(value.keys())
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        return values
    for item in raw_items[:limit]:
        text = _clip(item, limit=MAX_FIELD_LENGTH)
        if text is not None and text not in values:
            values.append(text)
    return values


def _find_mapping(value: Mapping[str, Any], *names: str) -> Mapping[str, Any] | None:
    for name in names:
        candidate = value.get(name)
        if isinstance(candidate, Mapping):
            return candidate
    return None


def _normalise_event(event: Any = None, event_type: Any = None) -> dict[str, Any]:
    source = event if isinstance(event, Mapping) else {}
    raw_type = event_type if event_type is not None else (
        source.get("event", source.get("type", source.get("kind", source.get("name"))))
        if isinstance(event, Mapping)
        else event
    )
    event_name = _clip(raw_type, limit=100)
    details_source = source.get("details")
    details = details_source if isinstance(details_source, Mapping) else {}
    decision = (
        _optional_text(source, "decision", "workflow_decision")
        or _optional_text(details, "decision", "workflow_decision")
    )
    normalized_decision = decision.strip().upper() if decision else None
    if normalized_decision == "HUMAN_GATE":
        kind = "human_gate"
    else:
        kind = _key(event_name or "snapshot")
    if normalized_decision is None:
        normalized_decision = {
            "stage_ready": "STAGE_READY",
            "human_gate_requested": "HUMAN_GATE",
            "human_gate_resolved": "HUMAN_GATE",
            "human_gate": "HUMAN_GATE",
            "approve_stage": "APPROVED",
            "reject_stage": "REJECTED",
            "stop_stage": "STOPPED",
            "closeout": "CLOSEOUT",
            "closeout_review": "CLOSEOUT",
            "fresh_review": "CLOSEOUT",
        }.get(kind)
    created_at = (
        _optional_text(source, "created_at", "timestamp", "time", "occurred_at")
        or _optional_text(details, "created_at", "timestamp", "time", "occurred_at")
    )
    resulting_action = (
        _optional_text(source, "resulting_action", "result_action", "next_action", "action")
        or _optional_text(details, "resulting_action", "result_action", "next_action", "action")
    )
    conclusion = (
        _optional_text(
            source,
            "gpt_conclusion_summary",
            "gpt_conclusion",
            "conclusion_summary",
            "decision_summary",
            "summary",
        )
        or _optional_text(
            details,
            "gpt_conclusion_summary",
            "gpt_conclusion",
            "conclusion_summary",
            "decision_summary",
            "summary",
        )
    )
    return {
        "kind": kind,
        "event": event_name or "snapshot",
        "stage_id": _safe_id(source.get("stage_id")),
        "from_status": _clip(source.get("from_status"), limit=100),
        "to_status": _clip(source.get("to_status"), limit=100),
        "event_id": _safe_id(source.get("event_id")),
        "revision": _safe_int(source.get("revision")),
        "decision": normalized_decision,
        "actor": _optional_text(source, "actor") or _optional_text(details, "actor"),
        "rationale": _optional_text(source, "rationale") or _optional_text(details, "rationale"),
        "created_at": created_at,
        "resulting_action": resulting_action,
        "gpt_conclusion_summary": conclusion,
    }


def _event_is_supported(event: Mapping[str, Any]) -> bool:
    kind = _key(event.get("kind", ""))
    if kind in ARTIFACT_EVENT_KINDS:
        return True
    if kind == "planner_decision" and event.get("decision") == "HUMAN_GATE":
        return True
    return False


def _normalise_stage(value: Any, event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        value = {}
    wrapper = _find_mapping(value, "stage")
    if wrapper is not None:
        value = wrapper
    contract = _find_mapping(value, "contract") or {}
    plan = _find_mapping(value, "plan", "stage_plan") or {}

    def first(*names: str) -> Any:
        for mapping in (value, contract, plan):
            for name in names:
                if name in mapping:
                    return mapping[name]
        return None

    stage: dict[str, Any] = {
        "stage_id": _safe_id(first("stage_id")) or event.get("stage_id"),
        "project_id": _safe_id(first("project_id")),
        "plan_id": _safe_id(first("plan_id")),
        "stage_name": _optional_text(value, "stage_name") or _optional_text(contract, "stage_name"),
        "project_goal": _optional_text(value, "project_goal") or _optional_text(contract, "project_goal"),
        "stage_goal": _optional_text(value, "stage_goal") or _optional_text(contract, "stage_goal"),
        "user_visible_goal": _optional_text(value, "user_visible_goal") or _optional_text(contract, "user_visible_goal"),
        "status": _clip(first("status", "stage_status"), limit=100) or event.get("to_status"),
        "next_action": _clip(first("next_action"), limit=160),
        "baseline_digest": _safe_id(first("baseline_digest")),
        "iteration_index": _safe_int(first("iteration_index")),
        "lane_mode": _clip(first("lane_mode"), limit=100),
        "required_checks": _safe_string_list(first("required_checks")),
        "review_artifact_requirements": _safe_string_list(first("review_artifact_requirements")),
    }

    pending = first("pending_human_gate")
    if isinstance(pending, Mapping):
        stage["pending_human_gate"] = {
            "decision": _optional_text(pending, "decision", limit=100),
            "rationale": _optional_text(pending, "rationale"),
            "actor": _optional_text(pending, "actor"),
            "approved": _safe_bool(pending.get("approved")),
        }
    else:
        stage["pending_human_gate"] = None

    stage["events"] = _normalise_history(first("events"), _normalise_event)
    stage["consultations"] = _normalise_consultations(
        first("consultations", "consultation_requests", "reviews")
    )
    stage["decision_history"] = _normalise_decision_history(
        first("decision_history", "decisions", "route_decisions")
    )
    latest_result = first("latest_result")
    stage["latest_result"] = _normalise_result(latest_result)
    # Drop empty fields to keep generated files compact and stable.
    return {key: item for key, item in stage.items() if item not in (None, [], {})}


def _normalise_history(value: Any, normalizer: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in list(value)[-MAX_HISTORY_ITEMS:]:
        if isinstance(item, Mapping):
            normalized = normalizer(item)
            # Event history can contain arbitrary details; keep only the
            # already allow-listed fields returned by _normalise_event.
            result.append(normalized)
    return result


def _normalise_decision_entry(value: Any) -> dict[str, Any]:
    """Normalize one bounded consultation or lifecycle decision entry."""

    if not isinstance(value, Mapping):
        return {}
    # Controller events carry stable event ids/status transitions.  A review
    # index consultation may also have a ``type`` field, so use the event
    # shape rather than that ambiguous field to choose the normalizer.
    if any(key in value for key in ("event", "event_id", "from_status", "to_status", "revision")):
        return _normalise_event(value)
    normalized = _normalise_consultation(value)
    return normalized


def _normalise_decision_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in list(value)[-MAX_HISTORY_ITEMS:]:
        normalized = _normalise_decision_entry(item)
        if normalized:
            result.append(normalized)
    return result


def _normalise_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    allowed = {
        "status": ("status",),
        "stage_ready": ("stage_ready",),
        "decision": ("decision",),
        "iteration_index": ("iteration_index",),
        "baseline_digest": ("baseline_digest",),
        "summary": ("summary", "result_summary", "outcome"),
    }
    result: dict[str, Any] = {}
    for output_key, names in allowed.items():
        raw = next((value[name] for name in names if name in value), None)
        if output_key in {"stage_ready"}:
            safe = _safe_bool(raw)
        elif output_key == "iteration_index":
            safe = _safe_int(raw)
        elif output_key == "baseline_digest":
            safe = _safe_id(raw)
        else:
            safe = _clip(raw, limit=MAX_FIELD_LENGTH)
        if safe is not None:
            result[output_key] = safe
    return result or None


def _normalise_design_review(value: Any) -> dict[str, Any] | None:
    """Keep only the bounded workflow-level design review summary."""

    if not isinstance(value, Mapping):
        return None
    try:
        summary = normalize_design_summary(value.get("summary", value), require_core=False)
    except (DesignReviewError, TypeError, ValueError):
        return None
    result: dict[str, Any] = {
        "state": _clip(value.get("state"), limit=64),
        "trigger": _clip(value.get("trigger"), limit=64),
        "revision": _safe_int(value.get("revision")),
        "design_digest": _safe_id(value.get("design_digest")),
        "summary": summary,
        "project_final_goal": summary.get("project_final_goal", ""),
        "stage_count": summary.get("stage_count", 0),
        "stages": summary.get("stages", []),
        "major_risks": summary.get("major_risks", []),
        "recommended_overall_route": summary.get("recommended_overall_route", ""),
        "review_question": summary.get("review_question", ""),
        "confirmation_needed": summary.get("confirmation_needed", False),
        "feedback_required": _safe_bool(value.get("feedback_required")),
        "feedback_digest": _safe_id(value.get("feedback_digest")),
        "gpt_reconsultation_ref": _safe_id(value.get("gpt_reconsultation_ref")),
        "stage_created": _safe_bool(value.get("stage_created")),
        "stage_started": _safe_bool(value.get("stage_started")),
        "step15_status": _clip(value.get("step15_status"), limit=64),
    }
    feedback = value.get("feedback")
    if isinstance(feedback, str):
        result["feedback"] = _clip(feedback, limit=MAX_FIELD_LENGTH)
    elif isinstance(feedback, Mapping):
        # Feedback is an explicit human note, not a transcript.  Render only
        # its bounded string-valued fields and never arbitrary nested data.
        result["feedback"] = {
            _clip(key, limit=80) or "field": _clip(item, limit=MAX_FIELD_LENGTH) or ""
            for key, item in list(feedback.items())[:MAX_LIST_ITEMS]
            if _clip(key, limit=80) is not None and _clip(item, limit=MAX_FIELD_LENGTH) is not None
        }
    decision = value.get("human_decision")
    if isinstance(decision, Mapping):
        result["human_decision"] = {
            "decision": _clip(decision.get("decision"), limit=64),
            "actor": _clip(decision.get("actor"), limit=MAX_ID_LENGTH),
        }
    return {key: item for key, item in result.items() if item not in (None, [], {})}


def _normalise_bootstrap_evidence(value: Any) -> dict[str, Any] | None:
    """Render only the bounded, allow-listed Bootstrap evidence projection.

    Discovery and Blueprint documents contain transport receipts, local paths,
    and other fields that are useful to the machine workflow but are not a
    human-artifact surface.  The runtime supplies a small projection; this
    second allow-list is deliberately kept here so a future caller cannot
    accidentally turn that projection into a raw document dump.
    """

    if not isinstance(value, Mapping):
        return None

    def text(mapping: Mapping[str, Any], name: str, *, limit: int = MAX_FIELD_LENGTH) -> str | None:
        return sanitize_bounded_evidence(mapping.get(name), limit=limit)

    def strings(mapping: Mapping[str, Any], name: str, *, limit: int = MAX_LIST_ITEMS) -> list[str]:
        result: list[str] = []
        for item in _safe_string_list(mapping.get(name), limit=limit):
            sanitized = sanitize_bounded_evidence(item)
            if sanitized is not None and sanitized not in result:
                result.append(sanitized)
        return result

    def route(mapping: Any) -> dict[str, Any] | None:
        if not isinstance(mapping, Mapping):
            return None
        result = {
            "title": sanitize_bounded_evidence(mapping.get("title") or mapping.get("name") or mapping.get("route_id")),
            "summary": sanitize_bounded_evidence(mapping.get("summary") or mapping.get("description")),
            "steps": [
                sanitized
                for item in _safe_string_list(mapping.get("steps"))
                if (sanitized := sanitize_bounded_evidence(item)) is not None
            ],
        }
        return {key: item for key, item in result.items() if item not in (None, [], {})} or None

    brief = value.get("brief") if isinstance(value.get("brief"), Mapping) else {}
    discovery = value.get("discovery") if isinstance(value.get("discovery"), Mapping) else {}
    blueprint = value.get("blueprint") if isinstance(value.get("blueprint"), Mapping) else {}
    contract = value.get("stage_contract") if isinstance(value.get("stage_contract"), Mapping) else {}
    artifacts = value.get("artifacts") if isinstance(value.get("artifacts"), Mapping) else {}

    normalized_brief = {
        "project_goal": text(brief, "project_goal"),
        "observable_outcome": text(brief, "observable_outcome"),
        "scope": strings(brief, "scope"),
        "non_goals": strings(brief, "non_goals"),
        "constraints": strings(brief, "constraints"),
        "available_assets": strings(brief, "available_assets"),
        "acceptance": strings(brief, "acceptance"),
        "human_preferences": strings(brief, "human_preferences"),
    }
    normalized_discovery = {
        "status": text(discovery, "status", limit=64),
            "evidence_digest": sanitize_bounded_evidence(discovery.get("evidence_digest"), limit=MAX_ID_LENGTH),
        "candidate_zero": text(discovery, "candidate_zero"),
        "primary_recommendation": text(discovery, "primary_recommendation"),
        "alternatives": strings(discovery, "alternatives"),
        "external_evidence": strings(discovery, "external_evidence"),
        "project_inferences": strings(discovery, "project_inferences"),
        "evidence_gaps": strings(discovery, "evidence_gaps"),
    }
    normalized_blueprint = {
        "status": text(blueprint, "status", limit=64),
        "evidence_digest": sanitize_bounded_evidence(blueprint.get("evidence_digest"), limit=MAX_ID_LENGTH),
        "candidate_zero": text(blueprint, "candidate_zero"),
        "primary_route": route(blueprint.get("primary_route")),
        "recommended_route": text(blueprint, "recommended_route"),
        "alternatives": strings(blueprint, "alternatives"),
        "external_evidence": strings(blueprint, "external_evidence"),
        "project_inferences": strings(blueprint, "project_inferences"),
        "evidence_gaps": strings(blueprint, "evidence_gaps"),
        "validation_plan": strings(blueprint, "validation_plan"),
    }
    normalized_contract = {
        "created": _safe_bool(contract.get("created")),
        "status": text(contract, "status", limit=64),
        "brief_scope": strings(contract, "brief_scope"),
        "allowed_paths": strings(contract, "allowed_paths"),
        "protected_paths": strings(contract, "protected_paths"),
    }
    normalized_artifacts = {
        key: sanitize_bounded_evidence(artifacts.get(key), limit=MAX_ID_LENGTH)
        for key in (
            "brief_path",
            "discovery_report_path",
            "blueprint_manifest_path",
            "brief_digest",
            "discovery_report_digest",
            "blueprint_manifest_digest",
        )
        if _safe_id(artifacts.get(key)) is not None
    }
    result = {
        "status": text(value, "status", limit=64) or "UNVERIFIED",
        "validated": _safe_bool(value.get("validated")),
        "missing": strings(value, "missing"),
        "brief": normalized_brief,
        "discovery": normalized_discovery,
        "blueprint": normalized_blueprint,
        "stage_contract": normalized_contract,
        "artifacts": normalized_artifacts,
    }
    return {
        key: item
        for key, item in result.items()
        if item not in (None, [], {})
    }


def _normalise_consultations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in list(value)[-MAX_HISTORY_ITEMS:]:
        if not isinstance(item, Mapping):
            continue
        receipt = _find_mapping(item, "receipt") or {}
        entry: dict[str, Any] = {}
        fields = {
            "stage_id": ("stage_id",),
            "created_at": ("created_at", "timestamp", "time", "occurred_at"),
            "receipt_path": ("receipt_path", "receipt_reference", "receipt_ref", "receipt"),
            "receipt_id": ("receipt_id",),
            "consultation_id": ("consultation_id", "id"),
            "request_id": ("request_id",),
            "type": ("type", "consultation_type", "request_type"),
            "mode": ("mode",),
            "evidence_digest": ("evidence_digest",),
            "evidence_summary": ("evidence_summary", "evidence"),
            "reason": ("reason",),
            "question_summary": ("question_summary", "question", "question_text"),
            "blocker_summary": ("blocker_summary", "blocker"),
            "gpt_conclusion_summary": (
                "gpt_conclusion_summary",
                "gpt_conclusion",
                "conclusion_summary",
                "decision_summary",
                "result_summary",
                "outcome",
                "summary",
            ),
            "gpt_recommendation_summary": (
                "gpt_recommendation_summary",
                "gpt_recommendation",
                "recommendation_summary",
                "recommendation",
            ),
            "workflow_decision": ("workflow_decision", "decision"),
            "codex_disposition": ("codex_disposition",),
            "resulting_action": ("resulting_action", "result_action", "next_action", "action"),
            "status": ("status",),
            "conversation_id": ("conversation_id",),
        }
        for output_key, names in fields.items():
            raw = next((item[name] for name in names if name in item), None)
            if raw is None and output_key in {
                "created_at",
                "receipt_path",
                "receipt_id",
                "consultation_id",
                "conversation_id",
                "type",
                "mode",
                "status",
            }:
                raw = next((receipt[name] for name in names if name in receipt), None)
            if raw is None and output_key == "conversation_id":
                raw = receipt.get("conversation_id")
            if raw is None and output_key == "consultation_id":
                raw = receipt.get("consultation_id")
            if raw is None and output_key == "receipt_path":
                raw = receipt.get("path")
            safe = _safe_id(raw) if output_key.endswith(("_id", "_digest")) else _clip(raw, limit=MAX_FIELD_LENGTH)
            if output_key in {"mode", "workflow_decision", "codex_disposition", "status"} and safe is not None:
                safe = safe.upper()
            if safe is not None:
                entry[output_key] = safe
        if entry:
            result.append(entry)
    return result


def _normalise_consultation(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    # A CLI result commonly wraps the bounded consultation metadata in a
    # ``result`` or ``consultation`` object.  Unwrap only those objects.
    nested = _find_mapping(value, "consultation", "result")
    if nested is not None:
        base = dict(value)
        base.update(nested)
        value = base
    normalized = _normalise_consultations([value])
    return normalized[0] if normalized else {}


def _snapshot(
    *,
    event: Any = None,
    event_type: Any = None,
    stage_metadata: Any = None,
    consultation_metadata: Any = None,
    design_review_metadata: Any = None,
    metadata: Any = None,
) -> dict[str, Any]:
    # ``metadata`` is a convenience for callers passing one event envelope.
    envelope = metadata if isinstance(metadata, Mapping) else {}
    selected_event = event if event is not None else envelope.get("event")
    selected_event_type = event_type if event_type is not None else envelope.get("event_type")
    normalized_event = _normalise_event(selected_event, selected_event_type)
    selected_stage = stage_metadata if stage_metadata is not None else envelope.get("stage")
    if selected_stage is None:
        selected_stage = envelope
    selected_consultation = (
        consultation_metadata
        if consultation_metadata is not None
        else envelope.get("consultation", envelope.get("result"))
    )
    selected_design_review = (
        design_review_metadata
        if design_review_metadata is not None
        else envelope.get("design_review")
    )
    selected_bootstrap_evidence = envelope.get("bootstrap_evidence")
    requirement_snapshot = envelope.get("requirement_snapshot", envelope.get("requirements"))
    stage = _normalise_stage(selected_stage, normalized_event)
    consultation = _normalise_consultation(selected_consultation)
    design_review = _normalise_design_review(selected_design_review)
    bootstrap_evidence = _normalise_bootstrap_evidence(selected_bootstrap_evidence)
    # Some event envelopes carry the completed consultation directly in their
    # details.  Pull only known metadata from there.
    if not consultation and isinstance(selected_event, Mapping):
        consultation = _normalise_consultation(selected_event.get("details"))
    if consultation:
        consultations = stage.setdefault("consultations", [])
        if consultation not in consultations:
            consultations.append(consultation)
        stage["consultations"] = consultations[-MAX_HISTORY_ITEMS:]
    return {
        "event": normalized_event,
        "stage": stage,
        "consultation": consultation,
        "design_review": design_review or {},
        "bootstrap_evidence": bootstrap_evidence or {},
        "requirement_snapshot": copy.deepcopy(requirement_snapshot) if isinstance(requirement_snapshot, Mapping) else {},
    }


def _value(snapshot: Mapping[str, Any], section: str, name: str, default: str = "未提供") -> str:
    section_value = snapshot.get(section)
    if isinstance(section_value, Mapping):
        item = section_value.get(name)
        if isinstance(item, bool):
            return "是" if item else "否"
        if isinstance(item, int):
            return str(item)
        if isinstance(item, str) and item:
            return item
    return default


def _bullet_values(values: Any, *, empty: str = "未声明") -> str:
    if not isinstance(values, (list, tuple)) or not values:
        return f"- {empty}"
    return "\n".join(f"- {item}" for item in values if isinstance(item, str)) or f"- {empty}"


def _latest_consultation(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    direct = snapshot.get("consultation")
    if isinstance(direct, Mapping) and direct:
        return direct
    stage = snapshot.get("stage")
    if isinstance(stage, Mapping):
        consultations = stage.get("consultations")
        if isinstance(consultations, list) and consultations:
            item = consultations[-1]
            if isinstance(item, Mapping):
                return item
    return {}


def _route_decision_entry_is_relevant(item: Mapping[str, Any]) -> bool:
    workflow_decision = item.get("workflow_decision")
    if isinstance(workflow_decision, str) and workflow_decision.strip():
        return True
    decision = item.get("decision")
    if isinstance(decision, str) and decision.strip():
        kind = _key(item.get("kind") or item.get("event") or "")
        return kind in ROUTE_DECISION_KINDS
    kind = _key(item.get("kind") or item.get("event") or "")
    return kind in ROUTE_DECISION_KINDS


def _decision_identity(item: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return a stable identity used to avoid replaying one decision twice."""

    for key in ("event_id", "consultation_id", "request_id", "receipt_id", "receipt_path"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return (key, value)
    return (
        "fallback",
        item.get("stage_id"),
        item.get("event") or item.get("kind") or item.get("type"),
        item.get("revision"),
        item.get("created_at"),
        item.get("workflow_decision") or item.get("decision"),
        item.get("reason") or item.get("rationale"),
    )


def _route_decision_history(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Collect bounded, route-relevant consultation and lifecycle decisions.

    The consultation history normally comes from the CLI review index, while
    lifecycle decisions come from the Stage controller event history.  Both
    sources are intentionally allow-listed and deduplicated before rendering
    so re-rendering an artifact cannot multiply entries.
    """

    stage = snapshot.get("stage")
    stage_mapping = stage if isinstance(stage, Mapping) else {}
    candidates: list[Mapping[str, Any]] = []
    for field in ("consultations", "decision_history", "events"):
        value = stage_mapping.get(field)
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, Mapping))
    direct_event = snapshot.get("event")
    if isinstance(direct_event, Mapping):
        candidates.append(direct_event)

    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in candidates:
        if not _route_decision_entry_is_relevant(item):
            continue
        identity = _decision_identity(item)
        if identity in seen:
            continue
        seen.add(identity)
        result.append(dict(item))
    return result[-MAX_HISTORY_ITEMS:]


def _decision_field(item: Mapping[str, Any], name: str) -> str | None:
    aliases: dict[str, tuple[str, ...]] = {
        "time": ("created_at", "timestamp", "time", "occurred_at"),
        "receipt": ("receipt_path", "receipt_reference", "receipt_ref", "receipt_id"),
        "consultation": ("consultation_id", "id"),
        "conversation": ("conversation_id",),
        "type": ("type", "consultation_type", "request_type", "mode", "kind", "event"),
        "reason": ("reason", "rationale"),
        "question": ("question_summary", "question"),
        "evidence": ("evidence_summary", "evidence_digest", "evidence"),
        "gpt_conclusion": (
            "gpt_conclusion_summary",
            "gpt_conclusion",
            "conclusion_summary",
            "decision_summary",
            "result_summary",
            "outcome",
            "summary",
        ),
        "workflow": ("workflow_decision", "decision"),
        "codex": ("codex_disposition",),
        "action": ("resulting_action", "result_action", "next_action", "action"),
    }
    for key in aliases.get(name, (name,)):
        value = item.get(key)
        safe = _clip(value, limit=MAX_RENDERED_DECISION_FIELD_LENGTH)
        if safe is not None:
            return safe
    return None


def _derived_resulting_action(item: Mapping[str, Any]) -> str:
    explicit = _decision_field(item, "action")
    if explicit:
        return explicit
    decision = _decision_field(item, "workflow")
    return {
        "CONTINUE": "继续当前 Stage",
        "REPLAN": "重新规划当前 Stage",
        "STAGE_READY": "等待人工审阅",
        "HUMAN_GATE": "等待人工闸门处理",
        "BLOCKED": "Stage 进入阻塞",
        "APPROVED": "Stage 已批准",
        "REJECTED": "返回 ACTIVE 并保留人工反馈",
        "STOPPED": "停止 Stage",
        "CLOSEOUT": "执行 closeout 审阅",
    }.get((decision or "").upper(), "未提供")


def _human_review_update_required(snapshot: Mapping[str, Any]) -> bool:
    """Whether the event represents a reviewable result for a human."""

    event = snapshot.get("event")
    event_mapping = event if isinstance(event, Mapping) else {}
    stage = snapshot.get("stage")
    stage_mapping = stage if isinstance(stage, Mapping) else {}
    kind = _key(event_mapping.get("kind") or event_mapping.get("event") or "")
    if kind in HUMAN_REVIEW_EVENT_KINDS:
        return True
    decision = event_mapping.get("decision")
    if isinstance(decision, str) and decision.strip().upper() in HUMAN_REVIEW_DECISIONS:
        return True
    # Do not inspect an old consultation merely because a later planning or
    # start event carries the review-index history.  A consultation outcome
    # can trigger this file only when it is the current event or is supplied
    # directly to this write.
    consultation = snapshot.get("consultation")
    if not isinstance(consultation, Mapping):
        consultation = {}
    if kind in {
        "consultation_complete",
        "consultation_completed",
        "consultation_completion",
        "consultation_done",
        "consultation_result",
        "consult_completed",
    }:
        consultation = _latest_consultation(snapshot)
    workflow = consultation.get("workflow_decision")
    if isinstance(workflow, str) and workflow.strip().upper() in HUMAN_REVIEW_DECISIONS:
        return True
    consultation_type = _decision_field(consultation, "type")
    if isinstance(consultation_type, str) and _key(consultation_type) in {
        "closeout",
        "closeout_review",
        "fresh_review",
        "fresh_closeout",
    }:
        return True
    # Stage CLI uses mode=FRESH for the independent closeout consultation.
    return str(consultation.get("mode", "")).strip().upper() == "FRESH"


def _gate(snapshot: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    stage = snapshot.get("stage")
    stage_mapping = stage if isinstance(stage, Mapping) else {}
    pending = stage_mapping.get("pending_human_gate")
    event = snapshot.get("event")
    event_mapping = event if isinstance(event, Mapping) else {}
    event_decision = event_mapping.get("decision")
    latest = _latest_consultation(snapshot)
    workflow_decision = latest.get("workflow_decision")
    if isinstance(pending, Mapping):
        approved = pending.get("approved")
        status = "已通过" if approved is True else "已拒绝" if approved is False else "待人工处理"
        return status, pending
    # A GPT/consultation ``WORKFLOW_DECISION: HUMAN_GATE`` is only bounded
    # decision data.  It must not synthesize a pending Human Gate in the
    # human-facing artifact: the Stage controller is the source of truth and
    # only an explicit ``request_human_gate``/``apply_decision`` transition
    # populates ``pending_human_gate`` above.  Keep the decision visible in
    # the decision log/reference section, but render the gate as untriggered
    # until the machine snapshot carries the pending record.
    event_kind = _key(event_mapping.get("kind") or event_mapping.get("event") or "")
    if event_kind == "approve_stage" or event_decision in {"APPROVE", "APPROVED"}:
        return "已通过", {
            "decision": "APPROVED",
            **{
                key: event_mapping.get(key)
                for key in ("actor", "rationale")
                if event_mapping.get(key) is not None
            },
        }
    if event_kind == "reject_stage" or event_decision in {"REJECT", "REJECTED"}:
        return "已拒绝", {
            "decision": "REJECTED",
            **{
                key: event_mapping.get(key)
                for key in ("actor", "rationale")
                if event_mapping.get(key) is not None
            },
        }
    stage_status = stage_mapping.get("status")
    if isinstance(stage_status, str) and stage_status.strip().upper() == "STAGE_READY":
        return "待人工审阅", {
            "decision": "STAGE_READY",
            "rationale": _optional_text(stage_mapping, "stage_goal", "user_visible_goal") or "",
        }
    if event_decision == "STAGE_READY" or workflow_decision == "STAGE_READY":
        return "待人工审阅", {"decision": "STAGE_READY"}
    return "未触发", {}


def build_stage_execution_plan(
    metadata: Mapping[str, Any] | None = None,
    *,
    event: Any = None,
    event_type: Any = None,
    stage_metadata: Any = None,
    consultation_metadata: Any = None,
    design_review_metadata: Any = None,
) -> str:
    """Render ``STAGE_EXECUTION_PLAN.md`` from bounded lifecycle metadata."""

    snapshot = _snapshot(
        event=event,
        event_type=event_type,
        stage_metadata=stage_metadata,
        consultation_metadata=consultation_metadata,
        design_review_metadata=design_review_metadata,
        metadata=metadata,
    )
    stage = snapshot["stage"]
    event_view = snapshot["event"]
    lines = [
        "# STAGE_EXECUTION_PLAN",
        "",
        "本文件由 Stage 生命周期事件自动维护，仅记录有界的阶段元数据。",
        "",
        "## 当前阶段",
        "",
        f"- 事件：{_value(snapshot, 'event', 'event')}（{_value(snapshot, 'event', 'kind')}）",
        f"- Stage ID：{_value(snapshot, 'stage', 'stage_id')}",
        f"- 项目 ID：{_value(snapshot, 'stage', 'project_id')}",
        f"- 计划 ID：{_value(snapshot, 'stage', 'plan_id')}",
        f"- 状态：{_value(snapshot, 'stage', 'status')}",
        f"- 下一动作：{_value(snapshot, 'stage', 'next_action')}",
        f"- 迭代：{_value(snapshot, 'stage', 'iteration_index')}",
        "",
        "## 目标",
        "",
        f"- 项目目标：{_value(snapshot, 'stage', 'project_goal')}",
        f"- 阶段目标：{_value(snapshot, 'stage', 'stage_goal')}",
        f"- 用户可见目标：{_value(snapshot, 'stage', 'user_visible_goal')}",
        "",
        "## 有界检查",
        "",
        "### 必需检查",
        _bullet_values(stage.get("required_checks")),
        "",
        "### 审查 artifact",
        _bullet_values(stage.get("review_artifact_requirements")),
        "",
        "## 事件记录",
        "",
    ]
    if event_view.get("actor"):
        lines.append(f"- 参与者：{event_view['actor']}")
    if event_view.get("rationale"):
        lines.append(f"- 理由：{event_view['rationale']}")
    if event_view.get("revision") is not None:
        lines.append(f"- Revision：{event_view['revision']}")
    lines.extend(
        [
            f"- 基线摘要：{_value(snapshot, 'stage', 'baseline_digest')}",
            "",
            "## 边界",
            "",
            "- 该文件不保存提示词、模型响应、对话转录、DOM 或凭据。",
            "- 阶段状态和人工闸门仍由 Stage controller 负责；本文件只提供可读摘要。",
            "",
        ]
    )
    return "\n".join(lines)


def build_research_decision_log(
    metadata: Mapping[str, Any] | None = None,
    *,
    event: Any = None,
    event_type: Any = None,
    stage_metadata: Any = None,
    consultation_metadata: Any = None,
    design_review_metadata: Any = None,
) -> str:
    """Render ``RESEARCH_DECISION_LOG.md`` from bounded consultation data."""

    snapshot = _snapshot(
        event=event,
        event_type=event_type,
        stage_metadata=stage_metadata,
        consultation_metadata=consultation_metadata,
        design_review_metadata=design_review_metadata,
        metadata=metadata,
    )
    stage = snapshot["stage"]
    history = _route_decision_history(snapshot)
    consult = history[-1] if history else _latest_consultation(snapshot)

    def rendered(item: Mapping[str, Any], name: str) -> str:
        if name == "action":
            value = _derived_resulting_action(item)
        else:
            value = _decision_field(item, name)
        return value or "未提供"

    lines = [
        "# RESEARCH_DECISION_LOG",
        "",
        "本文件由 Stage planning、咨询完成和路线决策事件自动维护。",
        "",
        "## 当前决策",
        "",
        f"- 事件：{_value(snapshot, 'event', 'event')}",
        f"- Stage ID：{_value(snapshot, 'stage', 'stage_id')}",
        f"- Stage 状态：{_value(snapshot, 'stage', 'status')}",
        f"- WORKFLOW_DECISION：{rendered(consult, 'workflow')}",
        f"- Codex disposition：{rendered(consult, 'codex')}",
        f"- Resulting action：{rendered(consult, 'action')}",
        "",
        "## 咨询摘要",
        "",
        f"- 时间：{rendered(consult, 'time')}",
        f"- Receipt：{rendered(consult, 'receipt')}",
        f"- Consultation ID：{rendered(consult, 'consultation')}",
        f"- 请求 ID：{rendered(consult, 'request_id')}",
        f"- 类型：{rendered(consult, 'type')}",
        f"- Conversation ID：{rendered(consult, 'conversation')}",
        f"- 原因：{rendered(consult, 'reason')}",
        f"- 问题摘要：{rendered(consult, 'question')}",
        f"- Evidence 摘要：{rendered(consult, 'evidence')}",
        f"- GPT 结论摘要：{rendered(consult, 'gpt_conclusion')}",
        f"- WORKFLOW_DECISION：{rendered(consult, 'workflow')}",
        f"- Codex disposition：{rendered(consult, 'codex')}",
        f"- Resulting action：{rendered(consult, 'action')}",
        "",
        "## 阶段约束",
        "",
        f"- 阶段目标：{stage.get('stage_goal', '未提供')}",
        f"- 基线摘要：{stage.get('baseline_digest', '未提供')}",
        f"- 当前迭代：{stage.get('iteration_index', '未提供')}",
        "",
        "## 决策历史",
        "",
    ]
    if history:
        rendered_history = history[-MAX_RENDERED_DECISION_HISTORY_ITEMS:]
        if len(history) > len(rendered_history):
            lines.extend(
                [
                    f"- 仅展示最近 {len(rendered_history)} 条路线记录；其余记录保留在机器审计索引中。",
                    "",
                ]
            )
        for index, item in enumerate(rendered_history, start=max(1, len(history) - len(rendered_history) + 1)):
            if not isinstance(item, Mapping):
                continue
            lines.extend(
                [
                    f"### 第 {index} 条路线记录",
                    f"- 时间：{rendered(item, 'time')}",
                    f"- Receipt：{rendered(item, 'receipt')}",
                    f"- Consultation ID：{rendered(item, 'consultation')}",
                    f"- 请求 ID：{rendered(item, 'request_id')}",
                    f"- 类型：{rendered(item, 'type')}",
                    f"- Conversation ID：{rendered(item, 'conversation')}",
                    f"- 原因：{rendered(item, 'reason')}",
                    f"- 问题摘要：{rendered(item, 'question')}",
                    f"- Evidence 摘要：{rendered(item, 'evidence')}",
                    f"- GPT 结论摘要：{rendered(item, 'gpt_conclusion')}",
                    f"- WORKFLOW_DECISION：{rendered(item, 'workflow')}",
                    f"- Codex disposition：{rendered(item, 'codex')}",
                    f"- Resulting action：{rendered(item, 'action')}",
                    "",
                ]
            )
    else:
        lines.extend(["- 暂无已完成路线决策记录", ""])
    lines.extend(
        [
            "## 记录边界",
            "",
            "只保存结构化摘要和引用标识；提示词、模型响应和对话转录不会写入本文件。",
            "",
        ]
    )
    return "\n".join(lines)


def build_human_review(
    metadata: Mapping[str, Any] | None = None,
    *,
    event: Any = None,
    event_type: Any = None,
    stage_metadata: Any = None,
    consultation_metadata: Any = None,
    design_review_metadata: Any = None,
    artifact_root: str | os.PathLike[str] | None = None,
) -> str:
    """Render ``HUMAN_REVIEW.md`` from bounded gate and review metadata."""

    snapshot = _snapshot(
        event=event,
        event_type=event_type,
        stage_metadata=stage_metadata,
        consultation_metadata=consultation_metadata,
        design_review_metadata=design_review_metadata,
        metadata=metadata,
    )
    presentation_metadata = dict(metadata or {})
    if isinstance(stage_metadata, Mapping):
        presentation_metadata.setdefault("stage", stage_metadata.get("stage", stage_metadata))
    if isinstance(consultation_metadata, Mapping):
        presentation_metadata.setdefault("consultation", consultation_metadata)
    if isinstance(design_review_metadata, Mapping):
        presentation_metadata.setdefault("design_review", design_review_metadata)
    presentation = build_human_presentation(
        metadata=presentation_metadata,
        canonical_state=snapshot,
        artifact_root=artifact_root,
    )
    presentation_text = render_human_presentation(presentation) + "\n\n"
    gate_status, gate = _gate(snapshot)
    consult = _latest_consultation(snapshot)
    stage = snapshot.get("stage") if isinstance(snapshot.get("stage"), Mapping) else {}
    latest_result = stage.get("latest_result") if isinstance(stage, Mapping) else None
    if not isinstance(latest_result, Mapping):
        latest_result = {}
    design_review = snapshot.get("design_review") if isinstance(snapshot.get("design_review"), Mapping) else {}
    # Research-to-Design Closure review is a decision surface: render the
    # proposed design directly instead of falling back to the legacy Stage
    # lifecycle template.
    closure = design_review.get("summary") if isinstance(design_review, Mapping) else None
    if isinstance(closure, Mapping) and closure.get("recommended_overall_route"):
        def vals(v):
            if isinstance(v, (list, tuple)): return "、".join(str(x) for x in v) if v else "（无）"
            return str(v) if v not in (None, "") else "（无）"
        lines = ["# 研究 → 设计闭环：人工设计评审", "", "## 30 秒摘要", "", f"- 要解决的问题：{closure.get('project_final_goal','（未提供）')}", f"- 研究推荐：{closure.get('recommended_overall_route')}", f"- 推荐理由：{closure.get('architecture_decision') or closure.get('stage_goal','')}", f"- 重要备选：{vals([s.get('name') or s.get('stage_id') for s in closure.get('stages',[]) if s.get('route_class') in {'ALTERNATIVE','SHARED'}])}", f"- 最大风险：{vals(closure.get('major_risks'))}", f"- 下一阶段：{vals([s.get('goal') for s in closure.get('stages',[])])}", "- 你现在需要决定：是否接受这个设计并允许进入后续实施规划。", "", "## 当前设计", "", f"推荐方案：**{closure.get('recommended_overall_route')}**", f"推荐原因：{closure.get('review_question','请审阅研究证据、风险与验证方法。')}", ""]
        for i,s in enumerate(closure.get('stages',[]),1): lines += [f"### 方案阶段 {i}：{s.get('name',s.get('stage_id','未命名'))}", f"目标：{s.get('goal','')}", f"验收：{vals(s.get('acceptance'))}", ""]
        lines += ["## 风险、未知与验证", f"- 风险：{vals(closure.get('major_risks'))}", f"- 未知：{vals(closure.get('unknowns'))}", f"- 验证方式：{vals(closure.get('validation_method'))}", "", "## 阶段设计审阅", "", f"- 状态：{design_review.get('state', '未提供')}", f"- 触发：{design_review.get('trigger', '未提供')}", f"- Revision：{design_review.get('revision', '未提供')}", f"- Design digest：{design_review.get('design_digest', '未提供')}", f"- Step 15 状态：{design_review.get('step15_status', '未提供')}", f"- Stage created：{'是' if design_review.get('stage_created') is True else '否'}", f"- Stage started：{'是' if design_review.get('stage_started') is True else '否'}", "", "### 项目级 Blueprint", "", f"- 项目最终目标：{closure.get('project_final_goal', '未提供')}", f"- 阶段数量：{closure.get('stage_count', len(closure.get('stages', [])) if isinstance(closure.get('stages'), (list, tuple)) else 0)}", "- 主要风险：", vals(closure.get('major_risks')), f"- 推荐总体路线：{closure.get('recommended_overall_route', '未提供')}", f"- 审阅问题：{closure.get('review_question', '未提供')}", f"- 需要确认：{'是' if closure.get('confirmation_needed') is True else '否'}", "", "### 各 Stage 边界", ""]
        for index, stage_summary in enumerate(closure.get('stages', []), start=1):
            if not isinstance(stage_summary, Mapping):
                continue
            lines += [
                f"#### Stage {index}：{stage_summary.get('stage_id', '未提供')} / {stage_summary.get('name', '未提供')}",
                f"- 目标：{stage_summary.get('goal', '未提供')}",
                f"- 存在原因：{stage_summary.get('why_exists', '未提供')}",
                "- 输入：",
                _bullet_values(stage_summary.get('inputs'), empty='证据不足：Blueprint 未提供'),
                "- 输出：",
                _bullet_values(stage_summary.get('outputs'), empty='证据不足：Blueprint 未提供'),
                "- 验收：",
                _bullet_values(stage_summary.get('acceptance'), empty='证据不足：Blueprint 未提供'),
                "- 依赖：",
                _bullet_values(stage_summary.get('dependencies'), empty='证据不足：Blueprint 未提供'),
                f"- 路线类别：{stage_summary.get('route_class', '未提供')}",
                "",
            ]
        lines += [f"- 阶段目标：{closure.get('stage_goal', '未提供')}", f"- 用户可见目标：{closure.get('user_visible_goal', '未提供')}", f"- 假设：{closure.get('hypothesis', '未提供')}", f"- 实验：{closure.get('experiment', '未提供')}", f"- 证伪条件：{closure.get('falsifier', '未提供')}", "- 验收条件：", _bullet_values(closure.get('acceptance')), "- 停止规则：", _bullet_values(closure.get('stop_rules')), "- Required checks：", _bullet_values(closure.get('required_checks')), "- Review artifacts：", _bullet_values(closure.get('review_artifacts')), f"- Baseline：{closure.get('baseline', '未提供')}", f"- Architecture decision：{closure.get('architecture_decision', '未提供')}", "", "## 你现在需要决定什么", "", "请在当前任务中直接回复以下三种决定之一；不要修改 JSON、checkpoint 或 StageController：", "", "- **ACCEPT**：接受当前 Design Package，Workflow 将写入 machine state；后续才可规划实施 Stage。", "- **REQUEST_CHANGES**：指出需要修改的方案、风险或验证项，Workflow 将记录反馈并等待修订。", "- **REJECT**：拒绝当前设计，Workflow 将记录拒绝原因并停止在设计评审阶段。", "", "ACCEPT 前不会创建或启动正式实施 Stage。"]
        return presentation_text + "\n".join(lines) + "\n"
    workflow_decision = consult.get("workflow_decision") or (
        "STAGE_READY" if str(stage.get("status", "")).upper() == "STAGE_READY" else None
    )
    # Requirement review is a recall of the intake baseline, not a second
    # intake.  Keep the human-facing section concise and omit machine-only
    # classifications and receipts.
    requirement = snapshot.get("requirement_snapshot")
    if not isinstance(requirement, Mapping):
        requirement = snapshot.get("requirements")
    if not isinstance(requirement, Mapping):
        requirement = snapshot.get("brief")
    requirement = requirement if isinstance(requirement, Mapping) else {}
    drift = design_review.get("requirement_drift") if isinstance(design_review, Mapping) else None
    if not isinstance(drift, Mapping):
        drift = {}
    if not drift and isinstance(design_review.get("current_requirements"), Mapping):
        current = design_review["current_requirements"]
        deltas = []
        for key in ("goal", "input", "expected_output", "success_criteria", "constraints", "non_goals"):
            before, after = requirement.get(key), current.get(key)
            if before != after:
                kind = "新增" if before in (None, "", []) else "删除" if after in (None, "", []) else "变化"
                deltas.append(f"{key}：{kind}（基线={before!r}；当前={after!r}）")
        drift = {"deltas": deltas}
    deltas = drift.get("deltas") if isinstance(drift.get("deltas"), list) else []
    lines_requirement = [
        "## 已确认需求回顾",
        "",
        f"- 项目目标：{requirement.get('goal', requirement.get('desired_outcome', '未提供'))}",
        "- Input：",
        _bullet_values(requirement.get("input")),
        "- Expected output：",
        _bullet_values(requirement.get("expected_output")),
        "- Success criteria：",
        _bullet_values(requirement.get("success_criteria")),
        "- Constraints：",
        _bullet_values(requirement.get("constraints")),
        "- Non-goals：",
        _bullet_values(requirement.get("non_goals")),
        "",
        "### Requirement Drift Check",
        "",
    ]
    if deltas:
        lines_requirement.append("- Requirement change: DRIFT")
        lines_requirement.extend(f"- Delta：{item}" for item in deltas)
        lines_requirement.append("- 人工判断：REQUIREMENT_DRIFT_OR_CORRECTION_NEEDED")
    else:
        lines_requirement.extend([
            "- Requirement change: NONE",
            "- 人工判断：REQUIREMENT_STILL_CORRECT",
        ])
    lines = [
        "# HUMAN_REVIEW",
        "",
        "本文件由 Stage 生命周期事件自动维护，用于呈现人工闸门的有界摘要。",
        "",
        "## 人工闸门",
        "",
        f"- 状态：{gate_status}",
        f"- 决策：{gate.get('decision', '未触发') if isinstance(gate, Mapping) else '未触发'}",
        f"- 参与者：{gate.get('actor', '未提供') if isinstance(gate, Mapping) else '未提供'}",
        f"- 理由：{gate.get('rationale', '未提供') if isinstance(gate, Mapping) else '未提供'}",
        "",
        "## 阶段引用",
        "",
        f"- Stage ID：{_value(snapshot, 'stage', 'stage_id')}",
        f"- Stage 状态：{_value(snapshot, 'stage', 'status')}",
        f"- 阶段目标：{_value(snapshot, 'stage', 'stage_goal')}",
        f"- 时间：{_decision_field(consult, 'time') or '未提供'}",
        f"- Receipt：{_decision_field(consult, 'receipt') or '未提供'}",
        f"- Consultation ID：{_decision_field(consult, 'consultation') or '未提供'}",
        f"- Conversation ID：{_decision_field(consult, 'conversation') or '未提供'}",
        f"- Evidence digest：{consult.get('evidence_digest', '未提供')}",
        f"- WORKFLOW_DECISION：{workflow_decision or '未提供'}",
        f"- GPT 结论摘要：{_decision_field(consult, 'gpt_conclusion') or _clip(latest_result.get('summary')) or '未提供'}",
        f"- Resulting action：{_derived_resulting_action({**consult, 'workflow_decision': workflow_decision}) if workflow_decision else '未提供'}",
        "",
    ]
    lines.extend(lines_requirement)
    if design_review:
        summary = design_review.get("summary") if isinstance(design_review.get("summary"), Mapping) else {}
        stages = summary.get("stages") if isinstance(summary.get("stages"), (list, tuple)) else []
        lines.extend(
            [
                "## 阶段设计审阅",
                "",
                f"- 状态：{design_review.get('state', '未提供')}",
                f"- 触发：{design_review.get('trigger', '未提供')}",
                f"- Revision：{design_review.get('revision', '未提供')}",
                f"- Design digest：{design_review.get('design_digest', '未提供')}",
                f"- Step 15 状态：{design_review.get('step15_status', '未提供')}",
                f"- Stage created：{'是' if design_review.get('stage_created') is True else '否'}",
                f"- Stage started：{'是' if design_review.get('stage_started') is True else '否'}",
                "",
                "### 项目级 Blueprint",
                "",
                f"- 项目最终目标：{summary.get('project_final_goal', '未提供')}",
                f"- 阶段数量：{summary.get('stage_count', len(stages))}",
                "- 主要风险：",
                _bullet_values(summary.get("major_risks")),
                f"- 推荐总体路线：{summary.get('recommended_overall_route', '未提供')}",
                f"- 审阅问题：{summary.get('review_question', '未提供')}",
                f"- 需要确认：{'是' if summary.get('confirmation_needed') is True else '否'}",
                "",
                "### 各 Stage 边界",
                "",
            ]
        )
        if stages:
            for index, stage_summary in enumerate(stages, start=1):
                if not isinstance(stage_summary, Mapping):
                    continue
                lines.extend(
                    [
                        f"#### Stage {index}：{stage_summary.get('stage_id', '未提供')} / {stage_summary.get('name', '未提供')}",
                        f"- 目标：{stage_summary.get('goal', '未提供')}",
                        f"- 存在原因：{stage_summary.get('why_exists', '未提供')}",
                        "- 输入：",
                        _bullet_values(stage_summary.get("inputs"), empty="证据不足：Blueprint 未提供"),
                        "- 输出：",
                        _bullet_values(stage_summary.get("outputs"), empty="证据不足：Blueprint 未提供"),
                        "- 验收：",
                        _bullet_values(stage_summary.get("acceptance"), empty="证据不足：Blueprint 未提供"),
                        "- 依赖：",
                        _bullet_values(stage_summary.get("dependencies"), empty="证据不足：Blueprint 未提供"),
                        f"- 路线类别：{stage_summary.get('route_class', '未提供')}",
                        "",
                    ]
                )
        else:
            lines.extend(["- 未提供项目级 Stage 列表", ""])
        lines.extend(
            [
                f"- 阶段目标：{summary.get('stage_goal', '未提供')}",
                f"- 用户可见目标：{summary.get('user_visible_goal', '未提供')}",
                f"- 假设：{summary.get('hypothesis', '未提供')}",
                f"- 实验：{summary.get('experiment', '未提供')}",
                f"- 证伪条件：{summary.get('falsifier', '未提供')}",
                "- 验收条件：",
                _bullet_values(summary.get("acceptance")),
                "- 停止规则：",
                _bullet_values(summary.get("stop_rules")),
                "- Allowed paths：",
                _bullet_values(summary.get("allowed_paths")),
                "- Protected paths：",
                _bullet_values(summary.get("protected_paths")),
                "- Required checks：",
                _bullet_values(summary.get("required_checks")),
                "- Review artifacts：",
                _bullet_values(summary.get("review_artifacts")),
                f"- Baseline：{summary.get('baseline', '未提供')}",
                f"- Architecture decision：{summary.get('architecture_decision', '未提供')}",
                f"- Feedback required：{'是' if design_review.get('feedback_required') is True else '否'}",
                f"- GPT re-consultation ref：{design_review.get('gpt_reconsultation_ref', '未提供')}",
                f"- Human decision：{design_review.get('human_decision', '未提供')}",
                f"- User feedback：{design_review.get('feedback', '未提供')}",
                "",
            ]
        )
    bootstrap = snapshot.get("bootstrap_evidence") if isinstance(snapshot.get("bootstrap_evidence"), Mapping) else {}
    if bootstrap:
        brief_evidence = bootstrap.get("brief") if isinstance(bootstrap.get("brief"), Mapping) else {}
        discovery_evidence = bootstrap.get("discovery") if isinstance(bootstrap.get("discovery"), Mapping) else {}
        blueprint_evidence = bootstrap.get("blueprint") if isinstance(bootstrap.get("blueprint"), Mapping) else {}
        contract_evidence = bootstrap.get("stage_contract") if isinstance(bootstrap.get("stage_contract"), Mapping) else {}
        artifacts_evidence = bootstrap.get("artifacts") if isinstance(bootstrap.get("artifacts"), Mapping) else {}
        external_evidence = list(
            dict.fromkeys(
                [
                    *(_safe_string_list(discovery_evidence.get("external_evidence"))),
                    *(_safe_string_list(blueprint_evidence.get("external_evidence"))),
                ]
            )
        )
        project_inferences = list(
            dict.fromkeys(
                [
                    *(_safe_string_list(discovery_evidence.get("project_inferences"))),
                    *(_safe_string_list(blueprint_evidence.get("project_inferences"))),
                ]
            )
        )
        evidence_gaps = list(
            dict.fromkeys(
                [
                    *(_safe_string_list(discovery_evidence.get("evidence_gaps"))),
                    *(_safe_string_list(blueprint_evidence.get("evidence_gaps"))),
                ]
            )
        )
        if not external_evidence:
            external_evidence = ["证据不足：未提供可验证的论文、成熟 OSS 或工程案例证据。"]
        if not project_inferences:
            project_inferences = ["证据不足：未提供独立的项目推断记录。"]
        if not evidence_gaps:
            evidence_gaps = ["证据不足：Bootstrap evidence 未提供缺口列表。"]
        primary_route = blueprint_evidence.get("primary_route")
        primary_route = primary_route if isinstance(primary_route, Mapping) else {}
        contract_created = contract_evidence.get("created") is True
        brief_scope = _safe_string_list(contract_evidence.get("brief_scope")) or _safe_string_list(brief_evidence.get("scope"))
        allowed_paths = _safe_string_list(contract_evidence.get("allowed_paths"))
        protected_paths = _safe_string_list(contract_evidence.get("protected_paths"))
        lines.extend(
            [
                "## 已验证 Bootstrap evidence（有界）",
                "",
                f"- 状态：{bootstrap.get('status', 'UNVERIFIED')}",
                f"- 证据已验证：{'是' if bootstrap.get('validated') is True else '否'}",
                f"- 项目目标：{brief_evidence.get('project_goal') or '未提供'}",
                f"- 可观察结果：{brief_evidence.get('observable_outcome') or '未提供'}",
                "",
                "### Approved brief",
                "",
                "- Scope：",
                _bullet_values(brief_evidence.get("scope")),
                "- Non-goals：",
                _bullet_values(brief_evidence.get("non_goals")),
                "- Constraints：",
                _bullet_values(brief_evidence.get("constraints")),
                "- Acceptance：",
                _bullet_values(brief_evidence.get("acceptance")),
                "",
                "### Discovery / Candidate 0 与替代路线",
                "",
                f"- Discovery 状态：{discovery_evidence.get('status') or '未提供'}",
                f"- Discovery evidence digest：{discovery_evidence.get('evidence_digest') or '未提供'}",
                f"- Candidate 0 推荐：{discovery_evidence.get('candidate_zero') or discovery_evidence.get('primary_recommendation') or '未提供'}",
                "- Discovery alternatives：",
                _bullet_values(discovery_evidence.get("alternatives")),
                "",
                "### Blueprint / GPT 推荐路线",
                "",
                f"- Blueprint 状态：{blueprint_evidence.get('status') or '未提供'}",
                f"- Blueprint evidence digest：{blueprint_evidence.get('evidence_digest') or '未提供'}",
                f"- Candidate 0 路线：{primary_route.get('title') or primary_route.get('summary') or '未提供'}",
                f"- GPT 推荐路线：{blueprint_evidence.get('recommended_route') or '未提供'}",
                "- Candidate 0 路线步骤：",
                _bullet_values(primary_route.get("steps")),
                "- Blueprint alternatives：",
                _bullet_values(blueprint_evidence.get("alternatives")),
                "",
                "### 外部证据（论文 / 成熟 OSS / 工程案例）",
                "",
                _bullet_values(external_evidence, empty="证据不足：未提供可验证外部证据。"),
                "",
                "### 项目推断",
                "",
                _bullet_values(project_inferences, empty="证据不足：未提供项目推断。"),
                "",
                "### 证据不足",
                "",
                _bullet_values(evidence_gaps, empty="证据不足：未提供缺口列表。"),
                "",
                "### Stage Contract 边界",
                "",
                f"- Stage Contract：{'已创建' if contract_created else '尚未创建'}",
                "- Brief scope：",
                _bullet_values(brief_scope, empty="未提供"),
                "- Allowed paths：",
                _bullet_values(allowed_paths, empty="尚未声明"),
                "- Protected paths：",
                _bullet_values(protected_paths, empty="尚未声明"),
                "",
                "### Bootstrap artifact references",
                "",
                f"- PROJECT_BRIEF：{artifacts_evidence.get('brief_path', '未提供')}",
                f"- DISCOVERY_REPORT：{artifacts_evidence.get('discovery_report_path', '未提供')}",
                f"- BLUEPRINT_MANIFEST：{artifacts_evidence.get('blueprint_manifest_path', '未提供')}",
                "",
            ]
        )
    lines.extend(
        [
        "## 人工动作",
        "",
        "- 人工闸门需要明确记录批准、拒绝或继续等待。",
        "- 本摘要不代表自动批准，也不改变 Stage controller 的状态。",
        "",
        "## 隐私边界",
        "",
        "本文件不保存提示词、模型响应、对话转录、DOM、cookie 或凭据。",
        "",
        ]
    )
    return presentation_text + "\n".join(lines)


def generate_human_artifacts(
    metadata: Mapping[str, Any] | None = None,
    *,
    event: Any = None,
    event_type: Any = None,
    stage_metadata: Any = None,
    consultation_metadata: Any = None,
    design_review_metadata: Any = None,
    artifact_root: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    """Return all three canonical documents without touching the filesystem."""

    common = {
        "metadata": metadata,
        "event": event,
        "event_type": event_type,
        "stage_metadata": stage_metadata,
        "consultation_metadata": consultation_metadata,
        "design_review_metadata": design_review_metadata,
    }
    return {
        STAGE_EXECUTION_PLAN_FILENAME: build_stage_execution_plan(**common),
        RESEARCH_DECISION_LOG_FILENAME: build_research_decision_log(**common),
        HUMAN_REVIEW_FILENAME: build_human_review(**common, artifact_root=artifact_root),
    }


def _repository_root(value: str | os.PathLike[str]) -> Path:
    try:
        candidate = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise HumanArtifactError("REPOSITORY_ROOT_INVALID", "repository root could not be resolved") from exc
    if not candidate.is_dir():
        raise HumanArtifactError("REPOSITORY_ROOT_INVALID", "repository root must be a directory")
    return candidate


def _canonical_target(root: Path, filename: str) -> Path:
    target = root / filename
    # The target is constructed from a fixed filename.  Reject an existing
    # link or directory so os.replace cannot silently redirect or destroy it.
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise HumanArtifactError("CANONICAL_ARTIFACT_INVALID", f"{filename} is not a regular file")
    try:
        target.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HumanArtifactError("CANONICAL_ARTIFACT_PATH_INVALID", f"{filename} is outside repository root") from exc
    return target


def _atomic_text(path: Path, content: str) -> None:
    encoded_size = len(content.encode("utf-8"))
    if encoded_size > MAX_ARTIFACT_BYTES:
        raise HumanArtifactError("CANONICAL_ARTIFACT_TOO_LARGE", f"{path.name} exceeds its bounded size")
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise HumanArtifactError("CANONICAL_ARTIFACT_WRITE_FAILED", f"could not write {path.name}") from exc
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def write_human_artifacts(
    project_root: str | os.PathLike[str],
    metadata: Mapping[str, Any] | None = None,
    *,
    event: Any = None,
    event_type: Any = None,
    stage_metadata: Any = None,
    consultation_metadata: Any = None,
    design_review_metadata: Any = None,
    artifact_root: str | os.PathLike[str] | None = None,
) -> dict[str, Path]:
    """Create/update the lifecycle artifacts for one supported event.

    ``event`` may be the controller event mapping or a short event name.  A
    caller may use ``event_type`` when its event details are held separately.
    ``STAGE_EXECUTION_PLAN.md`` and ``RESEARCH_DECISION_LOG.md`` are updated
    for every supported lifecycle event.  ``HUMAN_REVIEW.md`` is written only
    for a reviewable result (STAGE_READY, HUMAN_GATE, closeout, approve, or
    reject); a planning/start/CONTINUE event leaves an existing review file
    byte-for-byte untouched and does not create one.
    """

    root = _repository_root(project_root)
    event_view = _normalise_event(event, event_type)
    if event is not None or event_type is not None:
        if not _event_is_supported(event_view):
            raise HumanArtifactError("ARTIFACT_EVENT_UNSUPPORTED", f"event {event_view['event']} does not update human artifacts")
    snapshot = _snapshot(
        event=event,
        event_type=event_type,
        stage_metadata=stage_metadata,
        consultation_metadata=consultation_metadata,
        design_review_metadata=design_review_metadata,
        metadata=metadata,
    )
    should_update_review = (
        event is None
        and event_type is None
    ) or _human_review_update_required(snapshot)
    documents = generate_human_artifacts(
        metadata,
        event=event,
        event_type=event_type,
        stage_metadata=stage_metadata,
        consultation_metadata=consultation_metadata,
        design_review_metadata=design_review_metadata,
        artifact_root=root if should_update_review else None,
    )
    names = [STAGE_EXECUTION_PLAN_FILENAME, RESEARCH_DECISION_LOG_FILENAME]
    if should_update_review:
        names.append(HUMAN_REVIEW_FILENAME)
    targets = {name: _canonical_target(root, name) for name in names}
    for name in names:
        _atomic_text(targets[name], documents[name])
    return targets


# Integration callers can choose the descriptive name without taking a
# dependency on the implementation detail that this function writes all three
# files together.
update_human_artifacts = write_human_artifacts
update_human_artifacts_for_event = write_human_artifacts
render_stage_execution_plan = build_stage_execution_plan
render_research_decision_log = build_research_decision_log
render_human_review = build_human_review


__all__ = [
    "ARTIFACT_EVENT_KINDS",
    "CANONICAL_HUMAN_ARTIFACTS",
    "HUMAN_REVIEW_FILENAME",
    "HUMAN_REVIEW_PATH",
    "HumanArtifactError",
    "MAX_ARTIFACT_BYTES",
    "RESEARCH_DECISION_LOG_FILENAME",
    "RESEARCH_DECISION_LOG_PATH",
    "STAGE_EXECUTION_PLAN_FILENAME",
    "STAGE_EXECUTION_PLAN_PATH",
    "build_human_review",
    "build_research_decision_log",
    "build_stage_execution_plan",
    "generate_human_artifacts",
    "render_human_review",
    "render_research_decision_log",
    "render_stage_execution_plan",
    "sanitize_bounded_evidence",
    "update_human_artifacts",
    "update_human_artifacts_for_event",
    "write_human_artifacts",
]
