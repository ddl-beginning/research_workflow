"""Bounded workflow-level project Blueprint / Stage design review data.

This is a workflow checkpoint concern, not a StageController concern.  The
design is reviewed before Step 15 creates a Stage and remains only a proposed
project architecture until a human explicitly accepts it.
"""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Mapping

from .contracts import canonical_json


DESIGN_REVIEW_STATES = frozenset({"NOT_REQUIRED", "AWAITING_DESIGN_REVIEW", "ACCEPTED"})
DESIGN_REVIEW_TRIGGERS = frozenset({"INITIAL_ARCHITECTURE", "MAJOR_REPLAN"})
DESIGN_REVIEW_FEEDBACK_STATES = frozenset({"NONE", "FEEDBACK_REQUIRED"})

# These are the project-level fields that make the review a Blueprint rather
# than a single Stage contract.  ``stages`` is deliberately a bounded array;
# each item describes a complete Stage boundary and its route provenance.
PROJECT_DESIGN_SUMMARY_FIELDS = (
    "project_final_goal",
    "stage_count",
    "stages",
    "major_risks",
    "recommended_overall_route",
    "review_question",
    "confirmation_needed",
)
STAGE_DESIGN_FIELDS = (
    "stage_id",
    "name",
    "goal",
    "why_exists",
    "inputs",
    "outputs",
    "acceptance",
    "dependencies",
    "route_class",
)
# Legacy fields remain accepted and rendered as supplemental metadata, but
# they are never sufficient for ``require_core=True`` project review.
LEGACY_DESIGN_SUMMARY_FIELDS = (
    "stage_goal",
    "user_visible_goal",
    "hypothesis",
    "experiment",
    "falsifier",
    "stop_rules",
    "allowed_paths",
    "protected_paths",
    "required_checks",
    "review_artifacts",
    "baseline",
    "architecture_decision",
)
DESIGN_SUMMARY_FIELDS = PROJECT_DESIGN_SUMMARY_FIELDS + LEGACY_DESIGN_SUMMARY_FIELDS

ROUTE_CLASSES = frozenset({"CANDIDATE_0", "ALTERNATIVE", "SHARED", "NEW", "REPLACEMENT"})
_PROJECT_TEXT_FIELDS = frozenset({"project_final_goal", "recommended_overall_route", "review_question"})
_STAGE_TEXT_FIELDS = frozenset({"stage_id", "name", "goal", "why_exists", "route_class"})
_LEGACY_TEXT_FIELDS = frozenset(
    {"stage_goal", "user_visible_goal", "hypothesis", "experiment", "falsifier", "architecture_decision"}
)
_LEGACY_LIST_FIELDS = frozenset(
    {"stop_rules", "allowed_paths", "protected_paths", "required_checks", "review_artifacts"}
)
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "auth_token",
        "cookie",
        "cookies",
        "dom",
        "html",
        "password",
        "prompt",
        "raw_response",
        "response",
        "secret",
        "session",
        "token",
        "tokens",
        "transcript",
    }
)
_SECRET_RE = re.compile(r"(?:sk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._~+/=-]{16,})", re.IGNORECASE)


class DesignReviewError(ValueError):
    """Raised when a design review envelope is not bounded or coherent."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


def _text(value: Any, field: str, *, required: bool = False, maximum: int = 1200) -> str:
    if not isinstance(value, str):
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", f"{field} must be a string")
    checked = value.replace("\x00", "").strip()
    if required and not checked:
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", f"{field} must be non-empty")
    if len(checked) > maximum or _SECRET_RE.search(checked):
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", f"{field} is oversized or sensitive")
    return checked


def _list(value: Any, field: str, *, required: bool = False, path_values: bool = False) -> list[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    elif value is None:
        raw = []
    else:
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", f"{field} must be a string or array")
    if len(raw) > 32:
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", f"{field} contains too many entries")
    result: list[str] = []
    for item in raw:
        text = _text(item, field, maximum=600)
        if path_values:
            normalized = text.replace("\\", "/")
            if (
                normalized.startswith(("/", "//"))
                or re.match(r"^[A-Za-z]:/", normalized)
                or normalized.lower().startswith("file:")
                or ".." in normalized.split("/")
            ):
                raise DesignReviewError("DESIGN_PATH_INVALID", f"{field} must contain safe relative paths")
            text = normalized
        if text and text not in result:
            result.append(text)
    if required and not result:
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", f"{field} must contain at least one item")
    return result


def _bounded_json(value: Any, *, field: str, depth: int = 0) -> Any:
    if depth > 4:
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", f"{field} is too deeply nested")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        if len(value) > 32:
            raise DesignReviewError("DESIGN_SUMMARY_INVALID", f"{field} has too many fields")
        for key, item in value.items():
            name = str(key).strip()
            if not name or name.lower().replace("-", "_") in _SENSITIVE_KEYS:
                raise DesignReviewError("DESIGN_SUMMARY_INVALID", f"{field} contains a sensitive field")
            result[name] = _bounded_json(item, field=f"{field}.{name}", depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 32:
            raise DesignReviewError("DESIGN_SUMMARY_INVALID", f"{field} contains too many items")
        return [_bounded_json(item, field=f"{field}[]", depth=depth + 1) for item in value]
    if isinstance(value, str):
        checked = _text(value, field, maximum=600)
        field_name = field.rsplit(".", 1)[-1].lower().replace("-", "_")
        if "path" in field_name or field_name in {"file", "filename", "repository_root"}:
            normalized = checked.replace("\\", "/")
            if (
                normalized.startswith(("/", "//"))
                or re.match(r"^[A-Za-z]:/", normalized)
                or normalized.lower().startswith("file:")
                or ".." in normalized.split("/")
            ):
                raise DesignReviewError("DESIGN_PATH_INVALID", f"{field} must be a safe relative path")
            return normalized
        return checked
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise DesignReviewError("DESIGN_SUMMARY_INVALID", f"{field} must be JSON-safe")


def _normalize_stage(value: Any, *, index: int, require_core: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", f"stages[{index}] must be an object")
    normalized: dict[str, Any] = {}
    for field in STAGE_DESIGN_FIELDS:
        raw = value.get(field)
        if field in _STAGE_TEXT_FIELDS:
            checked = _text(raw or "", f"stages[{index}].{field}", required=require_core)
            if field == "route_class":
                checked = checked.upper()
                if require_core and checked not in ROUTE_CLASSES:
                    raise DesignReviewError("DESIGN_SUMMARY_INVALID", f"stages[{index}].route_class is not allowed")
            normalized[field] = checked
        else:
            normalized[field] = _list(
                raw,
                f"stages[{index}].{field}",
                required=require_core and field == "acceptance",
            )
    return normalized


def normalize_design_summary(value: Mapping[str, Any], *, require_core: bool = True) -> dict[str, Any]:
    """Normalize a project-level Blueprint and its bounded Stage list.

    ``require_core=False`` is used only by the human-artifact compatibility
    reader; runtime submission always requires the complete project shape.
    """

    if not isinstance(value, Mapping):
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", "design_summary must be an object")

    normalized: dict[str, Any] = {}
    for field in _PROJECT_TEXT_FIELDS:
        normalized[field] = _text(value.get(field) or "", field, required=require_core)

    raw_stage_count = value.get("stage_count", 0)
    if isinstance(raw_stage_count, bool) or not isinstance(raw_stage_count, int):
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", "stage_count must be an integer")
    if raw_stage_count < 0 or raw_stage_count > 16:
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", "stage_count is outside the bounded range")
    raw_stages = value.get("stages", [])
    if not isinstance(raw_stages, (list, tuple)):
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", "stages must be an array")
    if len(raw_stages) > 16:
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", "stages contains too many entries")
    stages = [_normalize_stage(item, index=index, require_core=require_core) for index, item in enumerate(raw_stages)]
    if require_core and not stages:
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", "stages must contain at least one Stage")
    if require_core and raw_stage_count != len(stages):
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", "stage_count must equal len(stages)")
    if not require_core and raw_stage_count != len(stages):
        raw_stage_count = len(stages)
    normalized["stage_count"] = raw_stage_count
    normalized["stages"] = stages
    normalized["major_risks"] = _list(value.get("major_risks"), "major_risks", required=False)
    confirmation = value.get("confirmation_needed", False)
    if not isinstance(confirmation, bool):
        raise DesignReviewError("DESIGN_SUMMARY_INVALID", "confirmation_needed must be a boolean")
    normalized["confirmation_needed"] = confirmation

    route_classes = {item["route_class"] for item in stages if item.get("route_class")}
    if require_core:
        if "CANDIDATE_0" not in route_classes:
            raise DesignReviewError("DESIGN_SUMMARY_INVALID", "stages must include a CANDIDATE_0 route")
        if not route_classes.intersection({"ALTERNATIVE", "SHARED"}):
            raise DesignReviewError("DESIGN_SUMMARY_INVALID", "stages must include a meaningful ALTERNATIVE or SHARED route")

    # Keep old Stage-contract fields as optional supplemental metadata.  They
    # cannot satisfy the project-level requirements above by themselves.
    for field in LEGACY_DESIGN_SUMMARY_FIELDS:
        raw = value.get(field)
        if field in _LEGACY_TEXT_FIELDS:
            normalized[field] = _text(raw or "", field, required=False)
        elif field in _LEGACY_LIST_FIELDS:
            normalized[field] = _list(raw, field, path_values=field in {"allowed_paths", "protected_paths"})
        else:
            normalized[field] = _bounded_json(raw if raw is not None else {}, field=field)
    return normalized


def design_summary_digest(summary: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(summary)).encode("utf-8")).hexdigest()


def design_review_trigger_required(trigger: Any) -> bool:
    """Only initial architecture and major REPLAN enter this review gate."""

    if not isinstance(trigger, str):
        return False
    return trigger.strip().upper() in DESIGN_REVIEW_TRIGGERS


def copy_design_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(value))


__all__ = [
    "DESIGN_REVIEW_FEEDBACK_STATES",
    "DESIGN_REVIEW_STATES",
    "DESIGN_REVIEW_TRIGGERS",
    "DESIGN_SUMMARY_FIELDS",
    "LEGACY_DESIGN_SUMMARY_FIELDS",
    "PROJECT_DESIGN_SUMMARY_FIELDS",
    "ROUTE_CLASSES",
    "STAGE_DESIGN_FIELDS",
    "DesignReviewError",
    "copy_design_summary",
    "design_review_trigger_required",
    "design_summary_digest",
    "normalize_design_summary",
]
